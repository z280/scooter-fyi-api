"""Fleet analytics rollups (sql/094, docs/PLAN_FLEET_ANALYTICS.md).

Rides, failed starts and dwell, summed by hour (or day) x region x model, so
the dashboard can slice months of history by neighbourhood without a
point-in-polygon test per request. Each source row is placed ONCE, here.

INCREMENTAL AND IDEMPOTENT. Each rollup keeps a watermark in
analytics_rollup_state and advances it in the same transaction as the sums it
adds, so a crash re-processes nothing twice and skips nothing:

  rides           trip_events.id: append-only, one writer (the ingest), so
                  ids commit in order.
  failed starts,  device_history.departed_at, processed only up to now - 6 h.
  dwell           departed_at is stamped later than the stop it closes, and the
                  absent rule backdates it by up to ABSENT_STOP_AFTER (1 h);
                  the lag keeps a late stamp from landing behind the watermark.

`refresh(cycle_id, snapshot_time)` runs at the end of every ingest cycle
(src/cycle.py), bounded per call, and never raises. `backfill()` (CLI
`analytics_backfill`) loops the same code until caught up.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .pg import connection

log = logging.getLogger(__name__)

#: Official boundary layers the dashboard can slice by. 'city' is added on top
#: (every event, region_name 'Denver').
REGION_LAYERS = ("neighborhood", "council_district", "community_network")
CITY = ("city", "Denver")

DENVER = ZoneInfo("America/Denver")
DEPARTURE_LAG = timedelta(hours=6)
MAX_DWELL = timedelta(days=30)
OFF_MAP_WINDOW = timedelta(days=7)

#: Rows per refresh call. A cycle sees ~100-300 new rides; the cap only bites
#: in the backfill, which simply calls again.
BATCH = 20_000

#: Per-cycle slices: small enough that catching up never slows the ingest.
CYCLE_BATCH = 2_000
CYCLE_STOP_WINDOW = timedelta(minutes=30)
CYCLE_REGION_WINDOW = timedelta(hours=2)

#: Closed stops are taken a time window at a time (see refresh_stops).
STOP_WINDOW = timedelta(hours=6)
#: Region snapshots likewise (regional_metrics_narrow: ~150 rows per cycle
#: for the dashboard's layers).
REGION_WINDOW = timedelta(days=1)

# Before any history existed for a rollup, start here (the feed's history).
_EPOCH = datetime(2026, 5, 1, tzinfo=timezone.utc)


@lru_cache(maxsize=200_000)
def _regions_at(lat4: float, lon4: float) -> tuple[tuple[str, str], ...]:
    """(region_type, region_name) for every layer containing the point, plus
    the city row. Cached on coordinates rounded to 4 decimals (~10 m): the
    fleet parks at the same racks, so the backfill mostly hits the cache."""
    from .geo import region_for_point

    out = [CITY]
    for layer in REGION_LAYERS:
        try:
            name = region_for_point(layer, lon4, lat4)
        except KeyError:  # layer not configured in this deployment
            name = None
        if name:
            out.append((layer, name))
    return tuple(out)


def regions_for(lat: float | None, lon: float | None) -> tuple[tuple[str, str], ...]:
    if lat is None or lon is None:
        return (CITY,)
    return _regions_at(round(float(lat), 4), round(float(lon), 4))


def _hour(t: datetime) -> datetime:
    return t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _model(m: str | None) -> str:
    return (m or "").strip() or "Unknown"


# --- pure aggregation (testable without a database) ---------------------------

def aggregate_rides(rows: Iterable[tuple]) -> dict[tuple, int]:
    """rows: (id, detected_at, model, from_lat, from_lon) -> {(hour, rt, rn, model): n}"""
    acc: dict[tuple, int] = defaultdict(int)
    for _id, detected_at, model, lat, lon in rows:
        h, m = _hour(detected_at), _model(model)
        for rt, rn in regions_for(lat, lon):
            acc[(h, rt, rn, m)] += 1
    return dict(acc)


def aggregate_stops(rows: Iterable[tuple]) -> tuple[dict[tuple, list[int]], dict[tuple, list[int]]]:
    """rows: (arrived, departed_at, model, lat, lon, dwell_failed_starts)
    -> (failed {(hour, rt, rn, model): [failed, stops]},
        dwell  {(day, rt, rn, model): [dwells, seconds]})"""
    failed: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    dwell: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    for arrived, departed, model, lat, lon, n_failed in rows:
        m = _model(model)
        regions = regions_for(lat, lon)
        if n_failed and n_failed > 0:
            h = _hour(departed)
            for rt, rn in regions:
                acc = failed[(h, rt, rn, m)]
                acc[0] += int(n_failed)
                acc[1] += 1
        span = departed - arrived
        if timedelta(0) <= span <= MAX_DWELL:
            day = departed.astimezone(DENVER).date()
            secs = int(span.total_seconds())
            for rt, rn in regions:
                acc = dwell[(day, rt, rn, m)]
                acc[0] += 1
                acc[1] += secs
    return dict(failed), dict(dwell)


# --- database side -------------------------------------------------------------

def _watermark(cur, name: str) -> tuple[int | None, datetime | None]:
    cur.execute("SELECT watermark_id, watermark_time FROM analytics_rollup_state WHERE name = %s "
                "FOR UPDATE", (name,))
    row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def _set_watermark(cur, name: str, wid: int | None, wtime: datetime | None) -> None:
    cur.execute(
        """
        INSERT INTO analytics_rollup_state (name, watermark_id, watermark_time, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (name) DO UPDATE SET watermark_id = EXCLUDED.watermark_id,
            watermark_time = EXCLUDED.watermark_time, updated_at = NOW()
        """,
        (name, wid, wtime),
    )


def refresh_rides(cur, batch: int = BATCH) -> int:
    wid, _ = _watermark(cur, "rides")
    cur.execute(
        "SELECT id, detected_at, vehicle_model_name, from_lat, from_lon FROM trip_events "
        "WHERE id > %s ORDER BY id LIMIT %s",
        (wid or 0, batch),
    )
    rows = cur.fetchall()
    if not rows:
        return 0
    acc = aggregate_rides(rows)
    cur.executemany(
        """
        INSERT INTO analytics_rides_hourly (hour, region_type, region_name, model, rides)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (hour, region_type, region_name, model)
        DO UPDATE SET rides = analytics_rides_hourly.rides + EXCLUDED.rides
        """,
        [(*k, v) for k, v in sorted(acc.items())],
    )
    _set_watermark(cur, "rides", rows[-1][0], None)
    return len(rows)


def refresh_stops(cur, now: datetime, step: timedelta = STOP_WINDOW) -> int:
    """Failed starts and dwell share one pass over newly-closed stops.

    By TIME WINDOW, not row count: everything with departed_at in
    (watermark, watermark + step] is taken whole, so no batch boundary can
    split a run of equal timestamps (a row cap would have to special-case
    that). A window is a few thousand stops; the backfill just takes more of
    them."""
    _, wtime = _watermark(cur, "stops")
    start = wtime or _EPOCH
    upto = min(now - DEPARTURE_LAG, start + step)
    if upto <= start:
        return 0
    cur.execute(
        """
        SELECT snapshot_time, departed_at, vehicle_model_name, lat, lon, dwell_failed_starts
        FROM device_history
        WHERE departed_at > %s AND departed_at <= %s
        """,
        (start, upto),
    )
    rows = cur.fetchall()
    failed, dwell = aggregate_stops(rows)
    if failed:
        cur.executemany(
            """
            INSERT INTO analytics_failed_starts_hourly
                (hour, region_type, region_name, model, failed_starts, stops_with_failures)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (hour, region_type, region_name, model) DO UPDATE SET
                failed_starts = analytics_failed_starts_hourly.failed_starts + EXCLUDED.failed_starts,
                stops_with_failures = analytics_failed_starts_hourly.stops_with_failures
                                      + EXCLUDED.stops_with_failures
            """,
            [(*k, *v) for k, v in sorted(failed.items())],
        )
    if dwell:
        cur.executemany(
            """
            INSERT INTO analytics_dwell_daily (day, region_type, region_name, model, dwells, dwell_seconds)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (day, region_type, region_name, model) DO UPDATE SET
                dwells = analytics_dwell_daily.dwells + EXCLUDED.dwells,
                dwell_seconds = analytics_dwell_daily.dwell_seconds + EXCLUDED.dwell_seconds
            """,
            [(*k, *v) for k, v in sorted(dwell.items())],
        )
    _set_watermark(cur, "stops", None, upto)
    # Report progress even for an empty window, so the backfill keeps going
    # until it reaches the lag line rather than stopping at the first gap.
    return max(len(rows), 1)


def refresh_region_devices(cur, step: timedelta = REGION_WINDOW) -> int:
    """Fold regional_metrics_narrow (per-cycle vehicles per region) into
    hourly sums for the dashboard's layers. By time window like the stops,
    bounded by the newest snapshot so a half-written cycle is never taken."""
    _, wtime = _watermark(cur, "region_devices")
    start = wtime or _EPOCH
    cur.execute("SELECT MAX(snapshot_time) FROM snapshot_metadata_core")
    newest = cur.fetchone()[0]
    if newest is None:
        return 0
    upto = min(newest, start + step)
    if upto <= start:
        return 0
    cur.execute(
        """
        INSERT INTO analytics_region_devices_hourly (hour, region_type, region_name, devices_sum, cycles)
        SELECT date_trunc('hour', snapshot_time), region_type, region_name, SUM(count_total), COUNT(*)
        FROM regional_metrics_narrow
        WHERE region_type = ANY(%s) AND snapshot_time > %s AND snapshot_time <= %s
        GROUP BY 1, 2, 3
        ON CONFLICT (hour, region_type, region_name) DO UPDATE SET
            devices_sum = analytics_region_devices_hourly.devices_sum + EXCLUDED.devices_sum,
            cycles = analytics_region_devices_hourly.cycles + EXCLUDED.cycles
        """,
        (list(REGION_LAYERS), start, upto),
    )
    n = cur.rowcount
    _set_watermark(cur, "region_devices", None, upto)
    return max(n, 1)


def record_off_map(cur, cycle_id: Any, snapshot_time: datetime) -> None:
    """Vehicles seen in the last 7 days but not in this cycle's feed."""
    cur.execute(
        """
        UPDATE device_status_snapshots SET off_map = (
            SELECT COUNT(*) FROM device_state
            WHERE last_observed_at >= %s AND last_cycle_id IS DISTINCT FROM %s
        )
        WHERE cycle_id = %s
        """,
        (snapshot_time - OFF_MAP_WINDOW, cycle_id, cycle_id),
    )


#: Advisory lock shared by the per-cycle refresh and the backfill: the
#: backfill holds it per pass; a cycle that cannot take it skips the rollups
#: (it never waits: the ingest must not stall behind a 10 s backfill batch).
_LOCK_KEY = 0x0A9A_7011


def refresh(cycle_id: Any = None, snapshot_time: datetime | None = None,
            *, backfill: bool = False) -> dict[str, int]:
    """One bounded refresh pass. Never raises: analytics are derived and must
    not fail the ingest.

    Per cycle (backfill=False) the slices are SMALL (2,000 rides, 30 min of
    stops, 2 h of region snapshots: ~1 s at worst) so catching up after a
    gap can never slow the 2-minute ingest; the backfill CLI takes big ones
    (20,000 rides at ~10 s) and holds the lock, which makes the cycles skip."""
    out = {"rides": 0, "stops": 0, "region_devices": 0}
    now = datetime.now(timezone.utc)
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                if cycle_id is not None and snapshot_time is not None:
                    record_off_map(cur, cycle_id, snapshot_time)
                if backfill:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
                else:
                    cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (_LOCK_KEY,))
                    if not cur.fetchone()[0]:
                        conn.commit()          # keep off_map; the backfill has the rollups
                        return out
                out["rides"] = refresh_rides(cur, BATCH if backfill else CYCLE_BATCH)
                out["stops"] = refresh_stops(cur, now, STOP_WINDOW if backfill else CYCLE_STOP_WINDOW)
                out["region_devices"] = refresh_region_devices(
                    cur, REGION_WINDOW if backfill else CYCLE_REGION_WINDOW)
            conn.commit()
    except Exception:  # noqa: BLE001
        log.exception("analytics rollup refresh failed")
    return out


def backfill(max_passes: int = 100_000) -> dict[str, int]:
    """Run refresh until every rollup is caught up. Each pass commits, so it
    can be interrupted and resumed; ingest cycles skip the rollups while a
    pass holds the lock."""
    total = {"rides": 0, "stops": 0, "region_devices": 0}
    for _ in range(max_passes):
        got = refresh(backfill=True)
        for k in total:
            total[k] += got.get(k, 0)
        if not any(got.values()):
            break
        log.info("analytics backfill: +%s (total %s)", got, total)
    return total
