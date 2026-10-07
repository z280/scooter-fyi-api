"""Fleet analytics rollups (sql/094, docs/PLAN_FLEET_ANALYTICS.md).

Rides, failed starts and dwell, summed by hour (or day) x region x model, so
the dashboard can slice months of history by neighbourhood without a
point-in-polygon test per request. Each source row is placed ONCE, here.

INCREMENTAL AND IDEMPOTENT. Sums and the position they have reached commit
in one transaction, so a crash re-processes nothing twice and skips nothing:

  rides           trip_events.id watermark: append-only, one writer (the
                  ingest; overlapping cycles serialize on device_state's row
                  locks before inserting), so ids commit in order.
  failed starts,  the CLOSE QUEUE (sql/094 analytics_stop_closes): a trigger
  dwell           queues every stop close in commit order, a reopen removes a
                  close not yet folded in, and the rollup consumes closes
                  after a 6 h settle. departed_at itself is not a watermark:
                  the absent rule and close_ghost_stops stamp it arbitrarily
                  far in the past. Stops closed before the migration are
                  swept once by departed_at, up to the cutover.
  region devices  snapshot_metadata_core.snapshot_time windows, joined to
                  regional_metrics_narrow on cycle_id (its primary key: a
                  range scan on regional_metrics_narrow itself walks ~1 GB of
                  index per cycle on PG 15).

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


def _fold_stops(cur, rows: list[tuple]) -> None:
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


def _cutover(cur) -> datetime | None:
    cur.execute("SELECT watermark_time FROM analytics_rollup_state WHERE name = 'stops_cutover'")
    row = cur.fetchone()
    return row[0] if row else None


def refresh_stops(cur, now: datetime, step: timedelta = STOP_WINDOW, batch: int = BATCH) -> int:
    """Failed starts and dwell. Two sources, one rule each, so no stop is
    counted by both: stops whose departed_at is <= the migration cutover are
    swept once by departed_at window (the legacy sweep); stops whose
    departed_at is after it come from the close queue."""
    cutover = _cutover(cur)
    done = 0
    # Legacy sweep, by time window (no batch boundary can split equal
    # timestamps), bounded by the cutover and the settle lag so a stop that
    # was still open at the cutover and closed with a backdated departed_at
    # has closed before its window is read.
    _, wtime = _watermark(cur, "stops")
    start = wtime or _EPOCH
    if cutover is not None and start < cutover:
        upto = min(cutover, now - DEPARTURE_LAG, start + step)
        if upto > start:
            cur.execute(
                """
                SELECT snapshot_time, departed_at, vehicle_model_name, lat, lon, dwell_failed_starts
                FROM device_history WHERE departed_at > %s AND departed_at <= %s
                """,
                (start, upto),
            )
            rows = cur.fetchall()
            _fold_stops(cur, rows)
            _set_watermark(cur, "stops", None, upto)
            done += max(len(rows), 1)
    # The close queue: closes that have settled for DEPARTURE_LAG.
    cur.execute(
        """
        SELECT c.seq, h.snapshot_time, h.departed_at, h.vehicle_model_name, h.lat, h.lon,
               h.dwell_failed_starts
        FROM analytics_stop_closes c JOIN device_history h ON h.id = c.stop_id
        WHERE c.closed_at <= %s
        ORDER BY c.seq LIMIT %s
        """,
        (now - DEPARTURE_LAG, batch),
    )
    queued = cur.fetchall()
    if queued:
        rows = [r[1:] for r in queued
                if r[2] is not None and (cutover is None or r[2] > cutover)]
        _fold_stops(cur, rows)
        cur.execute("DELETE FROM analytics_stop_closes WHERE seq = ANY(%s)", ([r[0] for r in queued],))
        done += len(queued)
    return done


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
        SELECT date_trunc('hour', s.snapshot_time), r.region_type, r.region_name,
               SUM(r.count_total), COUNT(*)
        FROM snapshot_metadata_core s
        JOIN regional_metrics_narrow r ON r.cycle_id = s.cycle_id
        WHERE r.region_type = ANY(%s) AND s.snapshot_time > %s AND s.snapshot_time <= %s
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
    # Only if device_state actually processed this cycle: otherwise no
    # vehicle carries this last_cycle_id and the whole week's fleet would be
    # stored as "off-map". Denver-core vehicles only, the same population as
    # the cycle's available / in-use / out-of-service counts.
    cur.execute(
        """
        UPDATE device_status_snapshots SET off_map = (
            SELECT COUNT(*) FROM device_state
            WHERE last_observed_at >= %s AND last_cycle_id IS DISTINCT FROM %s
              AND current_spatial_status = 'denver_core'
        )
        WHERE cycle_id = %s
          AND EXISTS (SELECT 1 FROM device_state_processed_cycles WHERE cycle_id = %s)
        """,
        (snapshot_time - OFF_MAP_WINDOW, cycle_id, cycle_id, cycle_id),
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
                if not backfill:
                    # A cycle's slice is ~0.1 s; anything slower is a bug, and
                    # it must not hold the ingest's process.
                    cur.execute("SET LOCAL statement_timeout = '5s'")
                out["rides"] = refresh_rides(cur, BATCH if backfill else CYCLE_BATCH)
                out["stops"] = refresh_stops(cur, now, STOP_WINDOW if backfill else CYCLE_STOP_WINDOW,
                                             BATCH if backfill else CYCLE_BATCH)
                out["region_devices"] = refresh_region_devices(
                    cur, REGION_WINDOW if backfill else CYCLE_REGION_WINDOW)
            conn.commit()
    except Exception as e:  # noqa: BLE001
        if backfill:
            raise   # a failed pass must not read as "caught up"
        log.exception("analytics rollup refresh failed")
        from .sentry import capture_exception
        capture_exception(e)
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
