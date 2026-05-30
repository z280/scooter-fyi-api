"""Daily 6 AM – 9 AM Denver SLA window computation.

Per the License Agreement, Exhibit B:
    Equity Area Deployment — "Deploy 30% of active fleet daily in Equity
    Areas. Daily deployment average during the 6am-9:00am window."

This module computes that window-average from per-cycle snapshots in
`snapshot_metadata_core` and stores one row per day in
`daily_sla_compliance`. Scheduled to run at 9:00 AM Denver time daily
(see src/main.py), but `compute_for_date(date)` is callable for
backfills.

Denver TZ is `America/Denver` — handled with stdlib zoneinfo so DST
transitions are correct.
"""

from __future__ import annotations

import logging
from datetime import date as date_cls, datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from .pg import connection
from .sentry import capture_exception

log = logging.getLogger(__name__)

DENVER_TZ = ZoneInfo("America/Denver")
COMPLIANCE_THRESHOLD = 30.0   # Exhibit B — Equity Area Deployment


# ---------------------------------------------------------------------------
# Window math
# ---------------------------------------------------------------------------
def window_for_date(d: date_cls) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for the 6 AM – 9 AM Denver window on
    the given Denver-local date.

    DST-aware via zoneinfo: on the spring-forward day, the window is
    still 6→9 wall-clock (2 hours of real time); on the fall-back day,
    it's 4 hours of real time. We use wall-clock semantics because the
    contract specifies "6am-9:00am".
    """
    start_local = datetime.combine(d, time(6, 0), tzinfo=DENVER_TZ)
    end_local = datetime.combine(d, time(9, 0), tzinfo=DENVER_TZ)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------
_AVG_FIELDS = (
    "total_devices_denver",
    "total_devices_v1",
    "total_devices_v2",
    "total_bike_denver",
    "total_bike_v1",
    "total_bike_v2",
    "total_scooter_denver",
    "total_scooter_v1",
    "total_scooter_v2",
    "total_not_in_denver",
    "percent_all_devices_v1",
    "percent_all_devices_v2",
    "percent_all_bikes_v1",
    "percent_all_bikes_v2",
    "percent_all_scooters_v1",
    "percent_all_scooters_v2",
    "percent_bikes_denver",
    "percent_scooters_denver",
    "percent_bikes_v1",
    "percent_scooters_v1",
    "percent_bikes_v2",
    "percent_scooters_v2",
)


def _avg_select_list() -> str:
    return ", ".join(f"AVG({f})::NUMERIC AS avg_{f}" for f in _AVG_FIELDS)


def compute_for_date(d: date_cls) -> dict:
    """Compute and upsert the daily SLA row for one Denver-local date.

    Returns the persisted row as a dict. Safe to call repeatedly for
    the same date — uses ON CONFLICT to replace prior computations.
    """
    start_utc, end_utc = window_for_date(d)
    log.info("computing daily SLA for %s (window %s – %s UTC)", d, start_utc, end_utc)

    select_sql = f"""
        SELECT COUNT(*)::INT AS snapshot_count, {_avg_select_list()}
        FROM snapshot_metadata_core
        WHERE snapshot_time >= %s AND snapshot_time < %s
    """

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(select_sql, (start_utc, end_utc))
            row = cur.fetchone()
            cols = [c.name for c in cur.description]
            agg = dict(zip(cols, row))

    snapshot_count = int(agg["snapshot_count"] or 0)
    v1_pct = agg.get("avg_percent_all_devices_v1")
    v2_pct = agg.get("avg_percent_all_devices_v2")
    pass_v1 = None if v1_pct is None else (float(v1_pct) >= COMPLIANCE_THRESHOLD)
    pass_v2 = None if v2_pct is None else (float(v2_pct) >= COMPLIANCE_THRESHOLD)

    record = {
        "sla_date": d,
        "window_start_ts": start_utc,
        "window_end_ts": end_utc,
        "snapshot_count": snapshot_count,
        **{f"avg_{f}": agg.get(f"avg_{f}") for f in _AVG_FIELDS},
        "compliance_v1_pass": pass_v1,
        "compliance_v2_pass": pass_v2,
    }

    insert_cols = list(record.keys())
    placeholders = ", ".join(f"%({c})s" for c in insert_cols)
    set_clause = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in insert_cols if c != "sla_date"
    ) + ", computed_at = NOW()"

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO daily_sla_compliance ({", ".join(insert_cols)})
                VALUES ({placeholders})
                ON CONFLICT (sla_date) DO UPDATE SET {set_clause}
                """,
                record,
            )
        conn.commit()

    log.info(
        "daily SLA %s: n=%d v1=%s%% (pass=%s) v2=%s%% (pass=%s)",
        d, snapshot_count, v1_pct, pass_v1, v2_pct, pass_v2,
    )
    return record


# ---------------------------------------------------------------------------
# Scheduler entry point
# ---------------------------------------------------------------------------
def run_daily() -> dict | None:
    """Called by APScheduler at 9:00 AM Denver time.

    Computes the window for *today* (Denver-local), since the window
    just ended at 9:00 AM. Catches exceptions and reports to Sentry —
    never lets the scheduler die.
    """
    try:
        today_denver = datetime.now(DENVER_TZ).date()
        return compute_for_date(today_denver)
    except Exception as e:  # noqa: BLE001
        log.exception("daily SLA job failed")
        capture_exception(e)
        return None


# ---------------------------------------------------------------------------
# Backfill helper
# ---------------------------------------------------------------------------
def backfill(start: date_cls, end: date_cls) -> list[dict]:
    """Inclusive [start, end] range backfill. Useful after a deploy to
    populate prior days from existing snapshot_metadata_core history."""
    if end < start:
        raise ValueError("end < start")
    results = []
    d = start
    while d <= end:
        results.append(compute_for_date(d))
        d += timedelta(days=1)
    return results
