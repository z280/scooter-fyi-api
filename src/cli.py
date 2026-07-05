"""Subcommand entry points for the cron scheduler.

Invoked from the crontab inside the `scheduler` container as
`python -m src.cli <command>`. Each command is idempotent and safe
to run repeatedly — the cron daemon doesn't track state.

Available commands:
    ingest_cycle      Fetch GBFS + DuckDB compute + write to Postgres.
    archive_if_due    Run the R2 archive only if >= archive_hours have
                      passed since the last successful archive.
    daily_sla         Compute today's 6-9 AM Denver SLA window.
    cleanup_receipts  Delete receipt images past the 18-month retention
                      (API_REQUIREMENTS.md §3.2 / privacy policy).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

from .archive import run_archive
from .config import load
from .cycle import run_once
from .daily_sla import run_daily
from .pg import connection, run_migrations
from .sentry import capture_exception, init as sentry_init, monitor

log = logging.getLogger("veo.cli")


# Sentry Cron Monitors — Sentry auto-creates these on first check-in using
# the schedule embedded below. Match the cron expressions in /app/crontab.
# checkin_margin = how late a check-in can arrive before counting as missed
# max_runtime = how long a run can take before Sentry alerts on a stuck job
_MONITOR_INGEST = {
    "schedule": {"type": "crontab", "value": "*/10 * * * *"},
    "timezone": "America/Denver",
    "checkin_margin": 2,    # minutes
    "max_runtime": 5,
    "failure_issue_threshold": 2,   # alert after 2 consecutive failures
    "recovery_threshold": 1,
}
_MONITOR_DAILY_SLA = {
    "schedule": {"type": "crontab", "value": "0 9 * * *"},
    "timezone": "America/Denver",
    "checkin_margin": 10,
    "max_runtime": 5,
    "failure_issue_threshold": 1,
    "recovery_threshold": 1,
}
_MONITOR_ARCHIVE = {
    "schedule": {"type": "crontab", "value": "0 2 * * *"},
    "timezone": "America/Denver",
    "checkin_margin": 30,
    "max_runtime": 30,
    "failure_issue_threshold": 1,
    "recovery_threshold": 1,
}


@monitor(slug="ingest_cycle", monitor_config=_MONITOR_INGEST)
def _cli_ingest_cycle():
    return run_once()


@monitor(slug="daily_sla", monitor_config=_MONITOR_DAILY_SLA)
def _cli_daily_sla():
    return run_daily()


@monitor(slug="archive_if_due", monitor_config=_MONITOR_ARCHIVE)
def _cli_archive_if_due():
    return archive_if_due()


def _last_archive_ts() -> datetime | None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM system_state WHERE key = 'last_archive_ts'")
            r = cur.fetchone()
    if not r or not r[0]:
        return None
    try:
        return datetime.fromisoformat(r[0])
    except ValueError:
        return None


def archive_if_due() -> dict | None:
    """Only run the archive when the last success was more than
    archive_hours ago. Lets us cron this daily without forcing a 48h
    cadence at the cron-config level."""
    cfg = load()
    last = _last_archive_ts()
    if last is None:
        log.info("archive_if_due: no prior archive recorded — running")
        return run_archive()
    age = datetime.now(timezone.utc) - last
    if age < timedelta(hours=cfg.schedule.archive_hours):
        log.info(
            "archive_if_due: last success was %.1fh ago (< %dh window), skipping",
            age.total_seconds() / 3600.0,
            cfg.schedule.archive_hours,
        )
        return None
    log.info(
        "archive_if_due: last success was %.1fh ago, running",
        age.total_seconds() / 3600.0,
    )
    return run_archive()


_RECEIPT_RETENTION_MONTHS = 18


def cleanup_receipts() -> dict:
    """Purge receipt images older than the documented 18-month retention.

    Deletes the R2 object and stamps receipt_deleted_at; the report row
    itself is kept (the evidence record outlives the image). Idempotent —
    rows already stamped are skipped.
    """
    from .receipts import ReceiptError, delete_receipt

    cutoff = datetime.now(timezone.utc) - timedelta(days=_RECEIPT_RETENTION_MONTHS * 30)
    deleted = failed = 0
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, receipt_r2_key FROM discount_reports
                WHERE receipt_r2_key IS NOT NULL
                  AND receipt_deleted_at IS NULL
                  AND created_at < %s
                """,
                (cutoff,),
            )
            rows = cur.fetchall()
            for row_id, key in rows:
                try:
                    delete_receipt(key)
                except ReceiptError:
                    log.exception("cleanup_receipts: R2 not configured — aborting")
                    raise
                except Exception:  # noqa: BLE001 — keep going, retry next run
                    log.exception("cleanup_receipts: delete failed for %s", key)
                    failed += 1
                    continue
                cur.execute(
                    "UPDATE discount_reports SET receipt_deleted_at = NOW() WHERE id = %s",
                    (row_id,),
                )
                deleted += 1
        conn.commit()
    log.info("cleanup_receipts: deleted=%d failed=%d", deleted, failed)
    return {"deleted": deleted, "failed": failed}


COMMANDS = {
    "ingest_cycle":      _cli_ingest_cycle,
    "archive_if_due":    _cli_archive_if_due,
    "daily_sla":         _cli_daily_sla,
    "cleanup_receipts":  cleanup_receipts,
    "migrate":           lambda: run_migrations(),
}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sentry_init()

    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or args[0] not in COMMANDS:
        choices = " | ".join(sorted(COMMANDS))
        print(f"usage: python -m src.cli ({choices})", file=sys.stderr)
        return 2

    cmd = args[0]
    try:
        result = COMMANDS[cmd]()
        log.info("cli command %s done: %r", cmd, result)
        return 0
    except Exception as e:  # noqa: BLE001
        log.exception("cli command %s failed", cmd)
        capture_exception(e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
