#!/usr/bin/env python3
"""Chunked drain of raw_telemetry_points → R2 → DELETE, one UTC day at a time.

Operational one-off for clearing a backlog that the steady-state
archive_if_due cron couldn't handle in a single shot (e.g. when the
scheduler was OOMing and the table grew past what one Parquet export
fits in memory).

Per day:
  1. Export rows where snapshot_time ∈ [day, day+1) to a local Parquet
     via DuckDB's postgres extension.
  2. Upload to R2 at raw/YYYY/MM/DD/raw_<ts>_drain_<hex>.parquet — same
     prefix shape as src.archive.run_archive so readers see one layout.
  3. DELETE the same rows in a single transaction.
  4. Move to the next day.

Resumability: each day's DELETE commits independently. A crash mid-run
re-processes that one day on the next invocation; the re-upload
overwrites the prior partial object (idempotent) and the DELETE picks
up the now-still-present rows.

Safety: only processes UTC days strictly before today, so the live
ingest_cycle appending to today's rows is never raced.

Invoke from inside the scheduler container (it already has the
POSTGRES_* and R2_* env set):

    sudo docker compose exec scheduler bash -c \\
      'cd /app && python scripts/drain_archive.py [--dry-run] [--max-days N]'
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make `from src...` work when invoked as `python scripts/drain_archive.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.archive import _set_system_state, _upload_to_r2  # noqa: E402
from src.duck import session  # noqa: E402
from src.pg import connection  # noqa: E402

log = logging.getLogger("drain")


def _dsn() -> str:
    p = {
        "host": os.environ.get("POSTGRES_HOST", "denver_spatial_db"),
        "port": os.environ.get("POSTGRES_PORT", "5432"),
        "user": os.environ["POSTGRES_USER"],
        "password": os.environ["POSTGRES_PASSWORD"],
        "db": os.environ["POSTGRES_DB"],
    }
    return (
        f"host={p['host']} port={p['port']} user={p['user']} "
        f"password={p['password']} dbname={p['db']}"
    )


def _table_bounds() -> tuple[datetime | None, datetime | None, int]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MIN(snapshot_time), MAX(snapshot_time), COUNT(*) "
                "FROM raw_telemetry_points;"
            )
            row = cur.fetchone()
    if not row or row[2] == 0:
        return None, None, 0
    return row[0], row[1], int(row[2])


def _floor_day_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _export_day(con, day_start: datetime, day_end: datetime, target: Path) -> int:
    # Inline the timestamps as ISO literals — internally generated, no SQL-
    # injection risk, and matches the f-string pattern in src.archive.
    where = (
        f"snapshot_time >= TIMESTAMP '{day_start.isoformat()}' "
        f"AND snapshot_time < TIMESTAMP '{day_end.isoformat()}'"
    )
    count = int(
        con.execute(
            f"SELECT COUNT(*) FROM pgsrc.public.raw_telemetry_points WHERE {where};"
        ).fetchone()[0]
        or 0
    )
    if count == 0:
        return 0
    con.execute(
        f"COPY (SELECT * FROM pgsrc.public.raw_telemetry_points WHERE {where}) "
        f"TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD);"
    )
    return count


def _delete_day(day_start: datetime, day_end: datetime) -> int:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM raw_telemetry_points "
                "WHERE snapshot_time >= %s AND snapshot_time < %s;",
                (day_start, day_end),
            )
            deleted = cur.rowcount
        conn.commit()
    return deleted


def drain(dry_run: bool, max_days: int | None) -> int:
    mn, mx, total = _table_bounds()
    if mn is None:
        log.info("raw_telemetry_points is empty — nothing to drain")
        return 0
    log.info("table has %d rows spanning %s → %s", total, mn, mx)

    cutoff = _floor_day_utc(datetime.now(timezone.utc))
    day = _floor_day_utc(mn)
    span_days = (cutoff - day).days
    log.info("draining UTC days [%s → %s), %d day(s) in scope%s",
             day.date(), cutoff.date(), span_days,
             f" (capped at --max-days={max_days})" if max_days else "")

    days_done = 0
    total_uploaded = 0
    total_deleted = 0

    while day < cutoff:
        if max_days is not None and days_done >= max_days:
            log.info("hit --max-days=%d, stopping", max_days)
            break
        day_end = day + timedelta(days=1)
        log.info("==== day %s ====", day.date())

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / (
                f"raw_{day:%Y%m%dT%H%M%SZ}_drain_{uuid.uuid4().hex[:8]}.parquet"
            )
            with session() as con:
                con.execute("INSTALL postgres; LOAD postgres;")
                con.execute(
                    f"ATTACH '{_dsn()}' AS pgsrc (TYPE POSTGRES, READ_ONLY);"
                )
                count = _export_day(con, day, day_end, local)

            if count == 0:
                log.info("day %s: 0 rows, skipping", day.date())
                day = day_end
                days_done += 1
                continue

            key = f"raw/{day:%Y/%m/%d/}{local.name}"
            log.info("day %s: %d rows → %s (%d bytes parquet)",
                     day.date(), count, key, local.stat().st_size)

            if dry_run:
                log.info("day %s: --dry-run, skipping upload + delete", day.date())
                day = day_end
                days_done += 1
                continue

            if not _upload_to_r2(local, key):
                log.error("day %s: R2 upload failed — aborting", day.date())
                return 1

        deleted = _delete_day(day, day_end)
        log.info("day %s: deleted %d rows from raw_telemetry_points",
                 day.date(), deleted)
        total_uploaded += count
        total_deleted += deleted
        days_done += 1
        day = day_end

    if not dry_run and day >= cutoff and days_done > 0:
        # Reached the safe-cutoff. Stamp last_archive_ts so the steady-state
        # archive_if_due cron treats us as current and resumes the 48h cycle
        # from here.
        _set_system_state(
            "last_archive_ts",
            datetime.now(timezone.utc).isoformat(),
        )
        log.info("drain reached cutoff — stamped last_archive_ts")

    log.info("done: %d day(s) processed, %d rows uploaded, %d rows deleted",
             days_done, total_uploaded, total_deleted)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(
        description="Chunked drain of raw_telemetry_points to R2."
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Export Parquet but skip upload and DELETE.")
    p.add_argument("--max-days", type=int, default=None,
                   help="Stop after this many UTC days (useful for a probe run).")
    args = p.parse_args(argv)
    try:
        return drain(args.dry_run, args.max_days)
    except Exception:
        log.exception("drain failed")
        return 2


if __name__ == "__main__":
    sys.exit(main())
