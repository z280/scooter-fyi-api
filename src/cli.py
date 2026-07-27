"""Subcommand entry points for the cron scheduler.

Invoked from the crontab inside the `scheduler` container as
`python -m src.cli <command>`. Each command is idempotent and safe
to run repeatedly — the cron daemon doesn't track state.

Available commands:
    ingest_cycle      Fetch GBFS + DuckDB compute + write to Postgres.
    archive_if_due    Run the R2 archive only if >= archive_hours have
                      passed since the last successful archive.
    daily_sla         Compute today's 6-9 AM Denver SLA window.
    daily_trips       Roll up yesterday's full-day trip/popularity stats.
    cleanup_receipts  Delete receipt images past the 18-month retention
                      (API_REQUIREMENTS.md §3.2 / privacy policy).
    fetch_map_pbf     Sync the routing .pbf + canopy sidecar from R2 into the
                      Valhalla volume. Runs as a one-shot sidecar before the
                      valhalla service starts.
    refresh_routing_graph
                      Re-sync the routing assets and report whether the .pbf
                      changed, i.e. whether Valhalla needs a tile rebuild.
    extract_battery_trips
                      Mine device_history for trips matching the anchor filter,
                      route them through Valhalla, store the observations.
    train_battery_model
                      Refit the battery-burn regression on stored observations.
    migrate           Apply pending SQL migrations.
    admin             Manage the admin allowlist:
                      `admin (list | add <email> | remove <email>)`.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

from .archive import run_archive
from .battery_model import extract_trips, train as train_battery
from .config import load
from .cycle import run_once
from .r2_map import sync_map_assets
from .daily_sla import run_daily
from .daily_trips import run_daily as run_daily_trips
from .pg import connection, run_migrations
from .sentry import capture_exception, init as sentry_init, monitor

log = logging.getLogger("veo.cli")


# Sentry Cron Monitors — Sentry auto-creates these on first check-in using
# the schedule embedded below. Match the cron expressions in /app/crontab.
# checkin_margin = how late a check-in can arrive before counting as missed
# max_runtime = how long a run can take before Sentry alerts on a stuck job
_MONITOR_INGEST = {
    "schedule": {"type": "crontab", "value": "*/2 * * * *"},
    "timezone": "America/Denver",
    "checkin_margin": 1,    # minutes; must stay under the 2-min interval
    "max_runtime": 2,       # cycles run ~5s; a 2-min run is already stuck
    # 5 consecutive failed cycles = one full 10-minute SLA interval with no
    # data. Polling (2 min) is now decoupled from the SLA interval (10 min);
    # a single missed 2-min cycle is a blip, not an SLA-scale event.
    "failure_issue_threshold": 5,
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
_MONITOR_DAILY_TRIPS = {
    "schedule": {"type": "crontab", "value": "0 9 * * *"},
    "timezone": "America/Denver",
    "checkin_margin": 10,
    "max_runtime": 10,
    "failure_issue_threshold": 1,
    "recovery_threshold": 1,
}


@monitor(slug="ingest_cycle", monitor_config=_MONITOR_INGEST)
def _cli_ingest_cycle():
    return run_once()


@monitor(slug="daily_sla", monitor_config=_MONITOR_DAILY_SLA)
def _cli_daily_sla():
    return run_daily()


@monitor(slug="daily_trips", monitor_config=_MONITOR_DAILY_TRIPS)
def _cli_daily_trips():
    return run_daily_trips()


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


def _months_ago(dt: datetime, months: int) -> datetime:
    """Subtract calendar months, not `months * 30` days — that shortcut
    under-counts by up to ~9 days over 18 months and would delete receipts
    before the documented retention actually elapses."""
    total_months = dt.year * 12 + (dt.month - 1) - months
    year, month = divmod(total_months, 12)
    month += 1
    # Clamp day-of-month for the rare case a shorter target month can't
    # hold it (e.g. Aug 31 - 18mo -> Feb 31 doesn't exist -> Feb 28/29).
    import calendar
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def cleanup_receipts() -> dict:
    """Purge receipt images older than the documented 18-month retention.

    Deletes the R2 object and stamps receipt_deleted_at; the report row
    itself is kept (the evidence record outlives the image). Idempotent —
    rows already stamped are skipped.
    """
    from .receipts import ReceiptError, delete_receipt

    cutoff = _months_ago(datetime.now(timezone.utc), _RECEIPT_RETENTION_MONTHS)
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


def _cli_fetch_map_pbf() -> dict:
    """One-shot sidecar: pull the routing assets into the Valhalla volume.

    Deliberately exits 0 even when R2 credentials are absent — compose gates the
    valhalla service on this completing, and a missing credential should not
    wedge the whole stack. sync_map_assets logs loudly in that case.
    """
    return sync_map_assets()


def _cli_refresh_routing_graph() -> dict:
    """Re-sync the routing assets on a schedule and report whether they moved.

    Valhalla's scripted entrypoint hashes the .pbf and rebuilds tiles when it
    changes, so the rebuild is triggered by recreating the container once this
    reports pbf_changed. Left as an operator/deploy step rather than giving this
    container a Docker socket.
    """
    result = sync_map_assets()
    if result.get("pbf_changed"):
        log.warning("Routing .pbf changed — recreate the valhalla service to "
                    "rebuild tiles: docker compose up -d --force-recreate valhalla")
    return result


def _cli_extract_battery_trips() -> dict:
    return extract_trips()


def _cli_train_battery_model() -> dict:
    return train_battery()


COMMANDS = {
    "ingest_cycle":          _cli_ingest_cycle,
    "archive_if_due":        _cli_archive_if_due,
    "daily_sla":             _cli_daily_sla,
    "daily_trips":           _cli_daily_trips,
    "cleanup_receipts":      cleanup_receipts,
    "fetch_map_pbf":         _cli_fetch_map_pbf,
    "refresh_routing_graph": _cli_refresh_routing_graph,
    "extract_battery_trips": _cli_extract_battery_trips,
    "train_battery_model":   _cli_train_battery_model,
    "migrate":               lambda: run_migrations(),
}


def admin_cli(sub_args: list[str]) -> int:
    """`python -m src.cli admin <list|add|remove> [email]` — manage the
    admin allowlist (the ADMIN_EMAILS replacement, sql/021). Same table the
    /admin/admins portal page edits."""
    from .accounts import add_admin, list_admins, remove_admin

    usage = "usage: python -m src.cli admin (list | add <email> | remove <email>)"
    if not sub_args:
        print(usage, file=sys.stderr)
        return 2
    action, rest = sub_args[0], sub_args[1:]

    if action == "list":
        rows = list_admins()
        if not rows:
            print("(no admins in the allowlist)")
        for r in rows:
            print(f"{r['email']}\t(added_by={r['added_by'] or '-'}, at={r['added_at'] or '-'})")
        return 0

    if action in ("add", "remove"):
        if len(rest) != 1:
            print(usage, file=sys.stderr)
            return 2
        email = rest[0]
        if action == "add":
            try:
                added = add_admin(email, added_by="cli")
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            print(f"{'added' if added else 'already present'}: {email}")
        else:
            removed = remove_admin(email)
            print(f"{'removed' if removed else 'not found'}: {email}")
        return 0

    print(usage, file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sentry_init()

    args = argv if argv is not None else sys.argv[1:]

    # `admin` takes sub-arguments (list/add/remove <email>); the cron
    # commands are strictly zero-arg.
    if args and args[0] == "admin":
        try:
            return admin_cli(args[1:])
        except Exception as e:  # noqa: BLE001
            log.exception("cli command admin failed")
            capture_exception(e)
            return 1

    if len(args) != 1 or args[0] not in COMMANDS:
        choices = " | ".join(sorted(COMMANDS))
        print(f"usage: python -m src.cli ({choices} | admin ...)", file=sys.stderr)
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
