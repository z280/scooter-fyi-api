"""Servicing: battery swaps, depot visits, the settled reading, effective fleet.

docs/SERVICING_PLAN.md Phase 1 (owner, 2026-10-10). The public feed shows two
service processes nobody recorded:

  * IN-FIELD SWAPS — ~2,400 a day, 85% where the vehicle sits, at a median 4%
    charge. A swap is a vehicle reading FULL while parked after reading <= 50%
    parked since it was last full (sql/105; never "any rise between readings":
    the range sags under load during a ride and rebounds for minutes after it,
    by up to ~25% of a full charge). Logged in service_events.
  * THE DEPOT (data/depots.json) — vehicles keep reporting inside it. A stay
    from the first stop inside to the first outside is one depot_visits row.

ONE RULE, TWO CALLERS. `step` is the charge rule; ingest (src/device_state.py)
applies it per reading with device_state as the state, and the archive
backfill applies it to the R2 parquet history with an in-memory state, so the
log cannot mean two things depending on where a row came from.

THE SETTLED READING. For SETTLE_MINUTES after a rental ends, the highest
parked reading since the release is the vehicle's charge: what riders see,
what a report records as its baseline, and what the clear rule compares.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import depots
from .fleet_reports import full_battery_meters, service_from_meters
from .pg import connection

log = logging.getLogger(__name__)

#: How long after a rental ends the reading is still settling. The rebound is
#: visible for a few cycles in the feed (e.g. 7514 -> 11231 -> 17876 m over
#: ~4 minutes, 2026-10-10); 30 minutes covers a slow-reporting vehicle.
SETTLE_MINUTES = 30

#: A swap whose full spot is within this of its low spot happened in place.
IN_PLACE_M = 100

#: The operator hides vehicles at ~4-5% (inferred: absences in place average
#: 6% before, 27% after). At or below this a vehicle is about to be hidden
#: (plan D4) — and an absent one was most likely hidden for a swap (1d).
HIDE_RISK_PERCENT = 8

#: Plan D2/D3: what riders are told.
FRESH_BATTERY_HOURS = 12
FRESH_BATTERY_PERCENT = 80
BACK_FROM_SHOP_MIN_HOURS = 72
BACK_FROM_SHOP_SHOW_DAYS = 7


def hide_risk_meters() -> int:
    """current_range_meters at HIDE_RISK_PERCENT, from the battery table."""
    from .quality import _soc_lut
    lut = _soc_lut()
    return int(lut[round(HIDE_RISK_PERCENT / 100 * (len(lut) - 1))])


def absence_kind_sql() -> str:
    """For the census (plan 1d), a SQL expression over device_state `ds`:
    'at_depot' (last seen inside a depot), 'hidden_low_battery' (last reading
    at or below HIDE_RISK_PERCENT: most likely hidden by the operator until a
    swap), else 'missing'."""
    return (f"CASE WHEN COALESCE({depots.sql_inside('ds.current_lat', 'ds.current_lon')}, FALSE) "
            f"THEN 'at_depot' "
            f"WHEN ds.last_range_meters <= {hide_risk_meters()} THEN 'hidden_low_battery' "
            f"ELSE 'missing' END")


@dataclass(frozen=True)
class ChargeState:
    last_range: int | None = None
    low: int | None = None
    low_at: datetime | None = None
    low_lat: float | None = None
    low_lon: float | None = None
    settled: int | None = None
    settling_until: datetime | None = None


@dataclass(frozen=True)
class Serviced:
    """A servicing found by `step`: the full reading and the low before it."""
    at: datetime
    lat: float
    lon: float
    full: int
    low: int
    low_at: datetime | None
    low_lat: float | None
    low_lon: float | None

    @property
    def moved_m(self) -> float | None:
        if self.low_lat is None or self.low_lon is None:
            return None
        from .geo import distance_meters
        return distance_meters(float(self.low_lat), float(self.low_lon), self.lat, self.lon)


def step(s: ChargeState, *, r: int, t: datetime, lat: float, lon: float,
         reserved: bool, in_rental_before: bool,
         full_m: int | None = None, low_m: int | None = None,
         ) -> tuple[ChargeState, Serviced | None]:
    """One reading through the charge rule. Pure; callers persist the state.

    `reserved` is this reading's is_reserved; `in_rental_before` whether the
    vehicle was in a rental until now (device_state.rental_started_at, or the
    previous reading's is_reserved in the archive). A reading that is either
    is not "parked" and never moves the low or stamps a servicing; the first
    parked reading after a rental starts the settling window.
    """
    full_m = full_battery_meters() if full_m is None else full_m
    low_m = service_from_meters() if low_m is None else low_m
    hit: Serviced | None = None
    n = replace(s, last_range=r)

    if reserved:
        return replace(n, settled=None, settling_until=None), None
    if in_rental_before:
        # The release reading: sagging. Start the settling window; it does not
        # count as a parked low (sql/105).
        return replace(n, settled=r,
                       settling_until=t + timedelta(minutes=SETTLE_MINUTES)), None
    if s.settling_until is not None and t < s.settling_until:
        n = replace(n, settled=max(s.settled or r, r))

    if r >= full_m:
        if s.low is not None and s.low <= low_m:
            hit = Serviced(at=t, lat=lat, lon=lon, full=r, low=s.low, low_at=s.low_at,
                           low_lat=s.low_lat, low_lon=s.low_lon)
        n = replace(n, low=r, low_at=t, low_lat=lat, low_lon=lon)
    elif s.low is None or r < s.low:
        n = replace(n, low=r, low_at=t, low_lat=lat, low_lon=lon)
    return n, hit


def settled_range(current: int | None, settled: int | None,
                  settling_until: datetime | None, now: datetime) -> int | None:
    """The range to show and compare: the settled reading while settling,
    else the current one."""
    if current is None:
        return None
    if settling_until is not None and settled is not None and now < settling_until:
        return max(settled, current)
    return current


def service_event_row(vid: str, hit: Serviced, *, cycle_id: Any, h3_9: Any,
                      model: str | None, after_absence: bool, source: str = "ingest",
                      ) -> dict[str, Any]:
    from .device_state import _equity_area_of
    moved = hit.moved_m
    return {
        "v": vid, "at": hit.at, "cycle": str(cycle_id) if cycle_id else None,
        "lat": hit.lat, "lon": hit.lon, "h3": int(h3_9) if h3_9 is not None else None,
        "eq": (_equity_area_of(float(hit.low_lon), float(hit.low_lat))
               if hit.low_lat is not None and hit.low_lon is not None else "unknown"),
        "low": hit.low, "low_at": hit.low_at, "low_lat": hit.low_lat, "low_lon": hit.low_lon,
        "full": hit.full, "moved": moved,
        "in_place": None if moved is None else moved < IN_PLACE_M,
        "absent": after_absence, "depot": depots.depot_at(hit.lat, hit.lon),
        "model": model, "src": source,
    }


INSERT_SERVICE_EVENT = """
    INSERT INTO service_events (
        vehicle_identifier, observed_at, cycle_id, lat, lon, h3_9_index, equity_area,
        low_range_meters, low_at, low_lat, low_lon, full_range_meters, moved_m,
        in_place, after_absence, depot_id, vehicle_model_name, source
    ) VALUES (%(v)s, %(at)s, %(cycle)s, %(lat)s, %(lon)s, %(h3)s, %(eq)s,
              %(low)s, %(low_at)s, %(low_lat)s, %(low_lon)s, %(full)s, %(moved)s,
              %(in_place)s, %(absent)s, %(depot)s, %(model)s, %(src)s)
    ON CONFLICT (vehicle_identifier, observed_at) DO NOTHING
"""


# ---------------------------------------------------------------------------
# Depot visits (per cycle)
# ---------------------------------------------------------------------------

def update_depot_visits(cycle_id: Any, snapshot_time: datetime,
                        devices: Iterable[Any]) -> dict[str, int]:
    """Open a visit for each vehicle newly inside a depot, close the visit of
    each vehicle seen outside again. Called from src/cycle.py after
    device_state, under the same isolation as ride_watch: a failure here must
    never fail the cycle.

    Cost: a geofence test per vehicle in Python (a bounding check rejects
    nearly all), one indexed read of the open visits, and writes only for
    vehicles that crossed a fence.
    """
    from .device_state import _equity_area_of

    seen: dict[str, tuple[str | None, Any]] = {}
    for d in devices:
        if d.vehicle_identifier:
            seen[d.vehicle_identifier] = (depots.depot_at(d.lat, d.lon), d)
    stats = {"opened": 0, "closed": 0, "inside": 0}
    if not seen:
        return stats
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT vehicle_identifier, id FROM depot_visits "
                "WHERE exited_at IS NULL AND vehicle_identifier = ANY(%s)",
                (list(seen),))
            open_by = {r[0]: r[1] for r in cur.fetchall()}
            to_open = [v for v, (dep, _) in seen.items() if dep and v not in open_by]
            to_close = [(open_by[v], d) for v, (dep, d) in seen.items()
                        if not dep and v in open_by]
            inside = [open_by[v] for v, (dep, _) in seen.items() if dep and v in open_by]
            stats["inside"] = len(inside) + len(to_open)
            if inside:
                cur.execute("UPDATE depot_visits SET last_inside_at = %s WHERE id = ANY(%s)",
                            (snapshot_time, inside))
            for v in to_open:
                dep, d = seen[v]
                # Where it was picked up: its last street stop before now.
                cur.execute(
                    f"""
                    SELECT COALESCE(departed_at, snapshot_time), lat, lon, departure_reason
                      FROM device_history
                     WHERE vehicle_identifier = %s AND snapshot_time < %s
                       AND NOT {depots.sql_inside('lat', 'lon')}
                     ORDER BY snapshot_time DESC LIMIT 1
                    """, (v, snapshot_time))
                p = cur.fetchone()
                cur.execute(
                    """
                    INSERT INTO depot_visits (vehicle_identifier, depot_id, entered_at,
                        last_inside_at, picked_up_at, pickup_lat, pickup_lon,
                        pickup_equity_area, went_dark_first, vehicle_model_name)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (vehicle_identifier, entered_at) DO NOTHING
                    """,
                    (v, dep, snapshot_time, snapshot_time,
                     p[0] if p else None, p[1] if p else None, p[2] if p else None,
                     _equity_area_of(float(p[2]), float(p[1])) if p else None,
                     (p[3] == "absent") if p else None, d.vehicle_model_name))
                stats["opened"] += 1
            for visit_id, d in to_close:
                cur.execute(
                    "UPDATE depot_visits SET exited_at = %s, deploy_lat = %s, deploy_lon = %s, "
                    "deploy_equity_area = %s WHERE id = %s AND exited_at IS NULL",
                    (snapshot_time, d.lat, d.lon, _equity_area_of(d.lon, d.lat), visit_id))
                stats["closed"] += 1
        conn.commit()
    return stats


# ---------------------------------------------------------------------------
# Effective fleet (plan 1e)
# ---------------------------------------------------------------------------

def effective_fleet(cur, now: datetime) -> dict[str, Any]:
    """On the street vs inside a depot, right now."""
    cur.execute(
        f"""
        SELECT
          (SELECT COUNT(*) FROM device_state
            WHERE last_observed_at >= %(now)s - INTERVAL '24 hours'
              AND NOT COALESCE({depots.sql_inside('current_lat', 'current_lon')}, FALSE)),
          COUNT(*) FILTER (WHERE entered_at > %(now)s - INTERVAL '7 days'),
          COUNT(*) FILTER (WHERE entered_at <= %(now)s - INTERVAL '7 days'
                             AND entered_at > %(now)s - INTERVAL '30 days'),
          COUNT(*) FILTER (WHERE entered_at <= %(now)s - INTERVAL '30 days')
          FROM depot_visits WHERE exited_at IS NULL
        """, {"now": now})
    street, d7, d30, older = cur.fetchone()
    return {
        "on_street_24h": int(street),
        "inside_depot": int(d7) + int(d30) + int(older),
        "inside_depot_under_7d": int(d7),
        "inside_depot_7_to_30d": int(d30),
        "inside_depot_30d_plus": int(older),
        "caveat": ("Depots are inferred from where vehicles report from; a vehicle "
                   "inside 30+ days has most likely been retired, but the feed "
                   "cannot say so."),
    }


# ---------------------------------------------------------------------------
# Backfills (plan 1f) — idempotent; run once by hand after deploy
# ---------------------------------------------------------------------------

def backfill_depot_visits(batch_vehicles: int = 500) -> dict[str, int]:
    """Depot visits from device_history (2026-05-31 onward): consecutive
    stops inside a depot are one visit. Keyed by (vehicle, entered_at), so a
    rerun adds nothing and ingest-written visits are kept."""
    from .device_state import _equity_area_of

    stats = {"vehicles": 0, "visits": 0, "replaced": 0}
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""SELECT DISTINCT vehicle_identifier FROM device_history
                             WHERE {depots.sql_inside('lat', 'lon')}""")
            vids = [r[0] for r in cur.fetchall()]
        for i in range(0, len(vids), batch_vehicles):
            chunk = vids[i:i + batch_vehicles]
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT vehicle_identifier, snapshot_time, departed_at, lat, lon,
                              departure_reason, vehicle_model_name
                         FROM device_history WHERE vehicle_identifier = ANY(%s)
                        ORDER BY vehicle_identifier, snapshot_time""", (chunk,))
                rows = cur.fetchall()
                visits = []
                cv, run, prev = None, None, None
                for v, t, dep, lat, lon, why, model in rows:
                    if v != cv:
                        if run:
                            visits.append(run)
                        cv, run, prev = v, None, None
                    inside = depots.depot_at(lat, lon)
                    if inside:
                        if run is None:
                            run = {"v": v, "depot": inside, "in": t, "last": dep or t,
                                   "model": model, "out": None, "dlat": None, "dlon": None,
                                   "pt": (prev[2] or prev[1]) if prev else None,
                                   "plat": prev[3] if prev else None,
                                   "plon": prev[4] if prev else None,
                                   "dark": (prev[5] == "absent") if prev else None}
                        else:
                            run["last"] = dep or t
                    else:
                        if run:
                            run.update(out=t, dlat=lat, dlon=lon)
                            visits.append(run)
                            run = None
                        prev = (v, t, dep, lat, lon, why)
                if run:
                    visits.append(run)
                for r in visits:
                    # A vehicle already inside when ingest started tracking
                    # depots got a visit opened at THAT cycle, not when it
                    # really arrived. The history knows better: the backfilled
                    # visit replaces any ingest visit that began inside it.
                    cur.execute(
                        "DELETE FROM depot_visits WHERE vehicle_identifier = %s "
                        "AND source = 'ingest' AND entered_at > %s "
                        "AND (%s::timestamptz IS NULL OR entered_at < %s)",
                        (r["v"], r["in"], r["out"], r["out"]))
                    stats["replaced"] = stats.get("replaced", 0) + cur.rowcount
                    cur.execute(
                        """
                        INSERT INTO depot_visits (vehicle_identifier, depot_id, entered_at,
                            last_inside_at, exited_at, picked_up_at, pickup_lat, pickup_lon,
                            pickup_equity_area, went_dark_first, deploy_lat, deploy_lon,
                            deploy_equity_area, vehicle_model_name, source)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'backfill')
                        ON CONFLICT (vehicle_identifier, entered_at) DO NOTHING
                        """,
                        (r["v"], r["depot"], r["in"], r["last"], r["out"], r["pt"],
                         r["plat"], r["plon"],
                         _equity_area_of(float(r["plon"]), float(r["plat"])) if r["plat"] is not None else None,
                         r["dark"], r["dlat"], r["dlon"],
                         _equity_area_of(float(r["dlon"]), float(r["dlat"])) if r["dlat"] is not None else None,
                         r["model"]))
                    stats["visits"] += cur.rowcount
            conn.commit()
            stats["vehicles"] += len(chunk)
            log.info("backfill_depot_visits: %d/%d vehicles, %d visits",
                     stats["vehicles"], len(vids), stats["visits"])
    return stats


def replay_charge(rows: Iterable[tuple], states: dict[str, ChargeState],
                  prev_reserved: dict[str, bool], last_seen: dict[str, datetime],
                  absent_after: timedelta) -> list[dict[str, Any]]:
    """The archive replay: rows of (vehicle_identifier, snapshot_time, range,
    is_reserved, lat, lon, model, h3_9) in (vehicle, time) order. State is
    carried across calls (files, days) so a low in one file and its full
    reading in the next still pair up."""
    out = []
    full_m, low_m = full_battery_meters(), service_from_meters()
    for v, t, r, res, lat, lon, model, h3_9 in rows:
        if r is None or lat is None or lon is None:
            continue
        lat, lon = float(lat), float(lon)
        s = states.get(v, ChargeState())
        gap = last_seen.get(v)
        s2, hit = step(s, r=int(r), t=t, lat=lat, lon=lon, reserved=bool(res),
                       in_rental_before=prev_reserved.get(v, False),
                       full_m=full_m, low_m=low_m)
        if hit is not None:
            out.append(service_event_row(
                v, hit, cycle_id=None, h3_9=h3_9, model=model,
                after_absence=gap is not None and t - gap > absent_after,
                source="backfill"))
        states[v] = s2
        prev_reserved[v] = bool(res)
        last_seen[v] = t
    return out


def _utc(text: str) -> datetime:
    t = datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def replay_archive_file(con, local: str, states: dict[str, ChargeState],
                        prev_res: dict[str, bool], last_seen: dict[str, datetime],
                        absent_after: timedelta):
    """Yield (day, service-event rows) for one local parquet archive file,
    day by day (battery_model's MEMORY note), carrying per-vehicle state."""
    from .battery_model import _archive_file_days

    for day in _archive_file_days(con, local):
        res = con.execute(
            # snapshot_time as text: DuckDB hands TIMESTAMPTZ to Python
            # through pytz, which the image does not ship (battery_model reads
            # it the same way).
            f"""SELECT vehicle_identifier, snapshot_time::VARCHAR, current_range_meters,
                       coalesce(is_reserved, FALSE), latitude, longitude,
                       vehicle_model_name, h3_9_index
                  FROM read_parquet('{local}')
                 WHERE vehicle_identifier IS NOT NULL
                   AND snapshot_time >= TIMESTAMPTZ '{day} 00:00:00+00'
                   AND snapshot_time <  TIMESTAMPTZ '{day} 00:00:00+00' + INTERVAL 1 DAY
                 ORDER BY vehicle_identifier, snapshot_time""")
        events: list[dict[str, Any]] = []
        while True:
            batch = res.fetchmany(100_000)
            if not batch:
                break
            events.extend(replay_charge(
                [(b[0], _utc(b[1]), *b[2:]) for b in batch],
                states, prev_res, last_seen, absent_after))
        yield day, events


def backfill_service_events(max_files: int | None = None) -> dict[str, Any]:
    """Service events from the R2 raw-telemetry archive (2-minute era only),
    one file at a time and one day at a time (battery_model's MEMORY note),
    replaying `step` per vehicle in time order. Idempotent on
    (vehicle_identifier, observed_at)."""
    import duckdb
    import boto3
    from botocore.client import Config as BotoConfig

    from .battery_model import ARCHIVE_DUCKDB_MEMORY, _archive_keys
    from .config import load, r2_credentials
    from .device_state import ABSENT_STOP_AFTER

    creds = r2_credentials()
    if creds is None:
        return {"error": "R2 credentials absent"}
    endpoint = load().r2.endpoint_url(creds["account_id"])
    s3 = boto3.client("s3", endpoint_url=endpoint,
                      aws_access_key_id=creds["access_key_id"],
                      aws_secret_access_key=creds["secret_access_key"],
                      config=BotoConfig(signature_version="s3v4"), region_name="auto")
    keys = _archive_keys(s3, creds["bucket"])
    if max_files:
        keys = keys[:max_files]
    scratch = "/tmp/servicing_backfill"
    os.makedirs(scratch, exist_ok=True)
    con = duckdb.connect(":memory:")
    con.execute(f"SET memory_limit='{ARCHIVE_DUCKDB_MEMORY}';")
    con.execute("SET threads=1; SET preserve_insertion_order=false;")
    con.execute("SET TimeZone='UTC';")   # the scheduler runs with TZ=America/Denver
    states: dict[str, ChargeState] = {}
    prev_res: dict[str, bool] = {}
    last_seen: dict[str, datetime] = {}
    stats = {"files": 0, "days": 0, "events": 0, "inserted": 0}
    for key in keys:
        local = os.path.join(scratch, os.path.basename(key))
        s3.download_file(creds["bucket"], key, local)
        try:
            for day, events in replay_archive_file(con, local, states, prev_res,
                                                   last_seen, ABSENT_STOP_AFTER):
                with connection() as conn:
                    with conn.cursor() as cur:
                        for e in events:
                            cur.execute(INSERT_SERVICE_EVENT, e)
                            stats["inserted"] += cur.rowcount
                    conn.commit()
                stats["days"] += 1
                stats["events"] += len(events)
                log.info("backfill_service_events: %s %s: %d events", key, day, len(events))
        finally:
            os.remove(local)
        stats["files"] += 1
    # The archive ends where the hot raw_telemetry_points buffer begins, and
    # ingest only started logging at the sql/107 deploy: replay the buffer
    # between the last archived reading and the first ingest-written event,
    # carrying the same per-vehicle state, so the log has no hole.
    stats["raw"] = _replay_raw_buffer(states, prev_res, last_seen,
                                      max(last_seen.values(), default=None),
                                      ABSENT_STOP_AFTER)
    stats["inserted"] += stats["raw"]["inserted"]
    return stats


def _replay_raw_buffer(states: dict[str, ChargeState], prev_res: dict[str, bool],
                       last_seen: dict[str, datetime], after: datetime | None,
                       absent_after: timedelta, until: datetime | None = None,
                       ) -> dict[str, int]:
    out = {"rows": 0, "events": 0, "inserted": 0}
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT MIN(observed_at) FROM service_events WHERE source = 'ingest'")
            until = cur.fetchone()[0] or until or datetime.now(timezone.utc)
        rc = conn.cursor(name="servicing_raw_replay")
        rc.itersize = 50_000
        rc.execute(
            """SELECT r.vehicle_identifier, m.snapshot_time, r.current_range_meters,
                      COALESCE(r.is_reserved, FALSE), r.latitude, r.longitude,
                      r.vehicle_model_name, r.h3_9_index
                 FROM raw_telemetry_points r JOIN snapshot_metadata_core m USING (cycle_id)
                WHERE r.vehicle_identifier IS NOT NULL
                  AND (%(after)s::timestamptz IS NULL OR m.snapshot_time > %(after)s)
                  AND m.snapshot_time < %(until)s
                ORDER BY r.vehicle_identifier, m.snapshot_time""",
            {"after": after, "until": until})
        events: list[dict[str, Any]] = []
        while True:
            batch = rc.fetchmany(50_000)
            if not batch:
                break
            out["rows"] += len(batch)
            events.extend(replay_charge(batch, states, prev_res, last_seen, absent_after))
        rc.close()
        conn.commit()
        with conn.cursor() as cur:
            for e in events:
                cur.execute(INSERT_SERVICE_EVENT, e)
                out["inserted"] += cur.rowcount
        conn.commit()
    out["events"] = len(events)
    log.info("backfill_service_events: raw buffer %s: %s", after, out)
    return out


# ---------------------------------------------------------------------------
# Depot discovery (report only)
# ---------------------------------------------------------------------------

def discover_depots(days: int = 60, min_count: int = 50) -> list[dict[str, Any]]:
    """Candidate depots: ~200 m cells where vehicles that left the feed
    reappeared more than 1 km from where they vanished, at least `min_count`
    times. Known depots are flagged. A human decides what to add."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH a AS (
                  SELECT h.lat glat, h.lon glon, n.lat blat, n.lon blon
                    FROM device_history h
                    JOIN LATERAL (SELECT lat, lon FROM device_history n
                                   WHERE n.vehicle_identifier = h.vehicle_identifier
                                     AND n.snapshot_time > h.departed_at
                                   ORDER BY snapshot_time LIMIT 1) n ON TRUE
                   WHERE h.departure_reason = 'absent'
                     AND h.departed_at > NOW() - make_interval(days => %s))
                SELECT round(blat::numeric, 3), round(blon::numeric, 3), COUNT(*)
                  FROM a WHERE geo_distance_m(glat, glon, blat, blon) > 1000
                 GROUP BY 1, 2 HAVING COUNT(*) >= %s ORDER BY 3 DESC LIMIT 25
                """, (days, min_count))
            rows = cur.fetchall()
    return [{"lat": float(r[0]), "lon": float(r[1]), "reappearances": int(r[2]),
             "known_depot": depots.depot_at(float(r[0]), float(r[1]))} for r in rows]
