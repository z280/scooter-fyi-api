"""The job-run ledger behind /admin/scheduler (sql/062).

Every scheduled operation records that it ran, what it returned, and how
long it took. src/cli.py has one dispatch point, so this is wired in once
there rather than per command — a job added later is recorded because it
exists, not because somebody remembered to instrument it.

Two rules are load-bearing enough to state here rather than bury in the
code:

**ingest_cycle is excluded.** It fires every 2 minutes — on its own, more
rows per day than every other command combined — and `observation_cycles`
already records each cycle in far more detail behind /admin/cycles. This
ledger exists to cover the jobs that had NO operator-visible record; the
ingest cycle was never one of them, and letting it in would swamp the
table to duplicate a better page.

**Recording must never break the job.** A scheduled command's purpose is
its work, not its bookkeeping. Every function here swallows its own
exceptions and logs them: a ledger that is down must not turn a healthy
archive run into a failed one. That is also why `start()` returning None
is an ordinary outcome the caller carries through rather than a special
case it has to check.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .pg import connection

log = logging.getLogger(__name__)

#: Commands deliberately absent from the ledger, with the reason each is
#: exempt. Anything not listed here is recorded — the default is "record
#: it", so a new job is covered by existing.
EXCLUDED_COMMANDS: dict[str, str] = {
    # ~720 runs/day, and observation_cycles + /admin/cycles already cover it
    # in more detail than this table could.
    "ingest_cycle": "recorded in observation_cycles; see /admin/cycles",
}


def is_recorded(command: str) -> bool:
    return command not in EXCLUDED_COMMANDS


def _jsonable(value: Any) -> Any:
    """Coerce a command's return value into something JSONB will take.

    Summaries are plain dicts of counts, but several carry a datetime (a
    run timestamp, a window bound) and one carries a UUID. `default=str`
    renders those rather than raising — a summary that cannot serialize
    must not fail the run that produced it, and a stringified timestamp is
    perfectly readable on the page this feeds.
    """
    if value is None:
        return None
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {"repr": repr(value)[:2000]}


def start(command: str) -> int | None:
    """Open a 'running' row and return its id, or None if the command is
    excluded or the insert failed.

    Written BEFORE the command runs, so a job killed mid-flight (OOM, a
    container restart) leaves a row that started and never finished —
    which is precisely the failure a log-only record hides.
    """
    if not is_recorded(command):
        return None
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO job_runs (command, status) VALUES (%s, 'running') RETURNING id",
                    (command,),
                )
                (run_id,) = cur.fetchone()
            conn.commit()
        return int(run_id)
    except Exception:  # noqa: BLE001 — bookkeeping must never break the job
        log.exception("job_runs: could not open a run row for %s", command)
        return None


def finish(
    run_id: int | None,
    *,
    status: str,
    summary: Any = None,
    error: str | None = None,
) -> None:
    """Close a run row. No-op when `run_id` is None (excluded command, or
    `start()` itself failed) so callers need no special case."""
    if run_id is None:
        return
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE job_runs
                       SET finished_at = NOW(),
                           status      = %s,
                           summary     = %s,
                           error       = %s,
                           duration_ms = GREATEST(
                               0, (EXTRACT(EPOCH FROM (NOW() - started_at)) * 1000)::int)
                     WHERE id = %s
                    """,
                    (status, json.dumps(_jsonable(summary)) if summary is not None else None,
                     error[:4000] if error else None, run_id),
                )
            conn.commit()
    except Exception:  # noqa: BLE001
        log.exception("job_runs: could not close run %s", run_id)


def prune(keep_days: int = 30) -> dict[str, Any]:
    """Delete run rows older than `keep_days`. Exposed as the
    `cleanup_job_runs` CLI command and scheduled alongside the other
    retention sweeps — this table is small, but it is append-only and
    nothing else would ever bound it."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM job_runs WHERE started_at < NOW() - make_interval(days => %s)",
                (keep_days,),
            )
            deleted = cur.rowcount
        conn.commit()
    log.info("job_runs.prune: deleted=%s keep_days=%s", deleted, keep_days)
    return {"deleted": deleted, "keep_days": keep_days}


def latest_per_command() -> list[dict[str, Any]]:
    """The newest run of each command, newest first — what /admin/scheduler
    renders. DISTINCT ON is the index-friendly way to ask Postgres for
    this, and `idx_job_runs_command_started` matches its sort exactly."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (command)
                       command, started_at, finished_at, status, duration_ms, summary, error
                FROM job_runs
                ORDER BY command, started_at DESC
                """
            )
            rows = cur.fetchall()
    out = [
        {
            "command": r[0],
            "started_at": r[1],
            "finished_at": r[2],
            "status": r[3],
            "duration_ms": r[4],
            "summary": r[5],
            "error": r[6],
        }
        for r in rows
    ]
    out.sort(key=lambda d: d["started_at"], reverse=True)
    return out


def recent(limit: int = 50) -> list[dict[str, Any]]:
    """The last `limit` runs across all commands, newest first."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT command, started_at, finished_at, status, duration_ms, summary, error
                FROM job_runs
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    return [
        {
            "command": r[0],
            "started_at": r[1],
            "finished_at": r[2],
            "status": r[3],
            "duration_ms": r[4],
            "summary": r[5],
            "error": r[6],
        }
        for r in rows
    ]
