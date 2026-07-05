"""Daily trip/popularity rollup.

A "trip" is a MOVED transition detected in src/device_state.py — the
vehicle's position changed by more than the stationary threshold between
consecutive 10-minute cycles, i.e. someone rode it somewhere. Each one is
logged to `trip_events` as it's detected; this module rolls those events
up once a day into:

    daily_trip_summary          — total trips + distinct vehicles tripped
    daily_vehicle_trip_counts   — per-vehicle trip count + popularity rank

Scheduled to run at 9:00 AM Denver time, the same cron slot as the
compliance SLA job (src/daily_sla.py) — see src/cli.py's `daily_sla`
command and /app/crontab — but scoped to a FULL Denver-local calendar
day, not the narrow 6am-9am SLA window. `compute_for_date(date)` is
callable directly for backfills.

Denver TZ is `America/Denver` — handled with stdlib zoneinfo so DST
transitions are correct.
"""

from __future__ import annotations

import logging
from datetime import date as date_cls, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .pg import connection
from .sentry import capture_exception

log = logging.getLogger(__name__)

DENVER_TZ = ZoneInfo("America/Denver")

# How many top vehicles to include in the log line / returned summary.
# The full ranked list always lands in daily_vehicle_trip_counts
# regardless of this cap.
_LOG_TOP_N = 5


def day_bounds_for_date(d: date_cls) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for the full Denver-local calendar day
    `d` (midnight to midnight). DST-aware via zoneinfo."""
    start_local = datetime.combine(d, time(0, 0), tzinfo=DENVER_TZ)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def compute_for_date(d: date_cls) -> dict[str, Any]:
    """Compute and upsert the daily trip rollup for one Denver-local date.

    Safe to call repeatedly for the same date — the per-vehicle rows are
    fully replaced (DELETE then INSERT) rather than incrementally
    updated, so a backfill or a re-run after a late-arriving trip_events
    write always reflects the current data, not a stale accumulation.
    """
    start_utc, end_utc = day_bounds_for_date(d)
    log.info("computing daily trip rollup for %s (window %s – %s UTC)", d, start_utc, end_utc)

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)::INT, COUNT(DISTINCT vehicle_identifier)::INT
                FROM trip_events
                WHERE detected_at >= %s AND detected_at < %s
                """,
                (start_utc, end_utc),
            )
            total_trips, distinct_vehicles = cur.fetchone()
            total_trips = total_trips or 0
            distinct_vehicles = distinct_vehicles or 0

            cur.execute(
                """
                INSERT INTO daily_trip_summary (trip_date, total_trips, distinct_vehicles_tripped)
                VALUES (%s, %s, %s)
                ON CONFLICT (trip_date) DO UPDATE SET
                    total_trips = EXCLUDED.total_trips,
                    distinct_vehicles_tripped = EXCLUDED.distinct_vehicles_tripped,
                    computed_at = NOW()
                """,
                (d, total_trips, distinct_vehicles),
            )

            cur.execute("DELETE FROM daily_vehicle_trip_counts WHERE trip_date = %s", (d,))

            # Per-vehicle counts + rank in one query. RANK() (not
            # ROW_NUMBER()) so tied trip counts share a position — two
            # vehicles both ridden 5 times are both "#1", matching the
            # convention already used for range_rank_all_by_type
            # (src/ranking.py).
            #
            # vehicle_plate/form_factor/vehicle_use_type/vehicle_model_name
            # are picked from the vehicle's most recent trip that day
            # (DISTINCT ON ... ORDER BY detected_at DESC) — these
            # shouldn't change within a day, but "most recent" is the
            # least surprising tiebreak if one somehow does.
            cur.execute(
                """
                INSERT INTO daily_vehicle_trip_counts (
                    trip_date, vehicle_identifier, vehicle_plate,
                    form_factor, vehicle_use_type, vehicle_model_name,
                    trip_count, popularity_rank
                )
                SELECT
                    %(d)s, vehicle_identifier, vehicle_plate,
                    form_factor, vehicle_use_type, vehicle_model_name,
                    trip_count,
                    RANK() OVER (ORDER BY trip_count DESC) AS popularity_rank
                FROM (
                    SELECT DISTINCT ON (te.vehicle_identifier)
                        te.vehicle_identifier, te.vehicle_plate,
                        te.form_factor, te.vehicle_use_type, te.vehicle_model_name,
                        counts.trip_count
                    FROM trip_events te
                    JOIN (
                        SELECT vehicle_identifier, COUNT(*)::INT AS trip_count
                        FROM trip_events
                        WHERE detected_at >= %(start)s AND detected_at < %(end)s
                        GROUP BY vehicle_identifier
                    ) counts ON counts.vehicle_identifier = te.vehicle_identifier
                    WHERE te.detected_at >= %(start)s AND te.detected_at < %(end)s
                    ORDER BY te.vehicle_identifier, te.detected_at DESC
                ) per_vehicle
                """,
                {"d": d, "start": start_utc, "end": end_utc},
            )

            cur.execute(
                """
                SELECT vehicle_plate, vehicle_model_name, trip_count, popularity_rank
                FROM daily_vehicle_trip_counts
                WHERE trip_date = %s
                ORDER BY popularity_rank ASC, vehicle_plate ASC
                LIMIT %s
                """,
                (d, _LOG_TOP_N),
            )
            top = [
                {"vehicle_plate": r[0], "vehicle_model_name": r[1],
                 "trip_count": r[2], "popularity_rank": r[3]}
                for r in cur.fetchall()
            ]
        conn.commit()

    log.info(
        "daily trips %s: total=%d distinct_vehicles=%d top=%s",
        d, total_trips, distinct_vehicles, top,
    )
    return {
        "trip_date": d,
        "total_trips": total_trips,
        "distinct_vehicles_tripped": distinct_vehicles,
        "top_vehicles": top,
    }


# ---------------------------------------------------------------------------
# Scheduler entry point
# ---------------------------------------------------------------------------
def run_daily() -> dict[str, Any] | None:
    """Called by the 9:00 AM Denver cron slot, alongside daily_sla.run_daily().

    Rolls up *yesterday's* full Denver-local calendar day — by 9am today,
    yesterday is the most recent day that's fully closed out. Catches
    exceptions and reports to Sentry — never lets the scheduler die.
    """
    try:
        yesterday_denver = (datetime.now(DENVER_TZ) - timedelta(days=1)).date()
        return compute_for_date(yesterday_denver)
    except Exception as e:  # noqa: BLE001
        log.exception("daily trip rollup job failed")
        capture_exception(e)
        return None


# ---------------------------------------------------------------------------
# Backfill helper
# ---------------------------------------------------------------------------
def backfill(start: date_cls, end: date_cls) -> list[dict[str, Any]]:
    """Inclusive [start, end] range backfill. Useful after a deploy to
    populate prior days from existing trip_events history."""
    if end < start:
        raise ValueError("end < start")
    results = []
    d = start
    while d <= end:
        results.append(compute_for_date(d))
        d += timedelta(days=1)
    return results
