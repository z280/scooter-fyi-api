"""Reprocess prior days' Equity Area compliance against the city's
clarified map.

WHY THIS EXISTS ---------------------------------------------------------
The city settled which map the Veo contract means in August 2026 (see
`src/equity_groups.py` OFFICIAL_GROUP / `data/equity.geojson`). Every
snapshot recorded before that deploy carries `*_v1` / `*_v2` / `*_erN`
counts and a NULL in every `*_equity` column, because the group did not
exist yet. Compliance history against the map that actually binds
therefore starts empty and, without this, would only ever fill forward.

The live pipeline cannot help: `compute.py` joins boundaries against the
DuckDB `points` table, which is built from the CURRENT GBFS payload and
thrown away at the end of the cycle. `raw_telemetry_points` keeps the
per-cycle positions but only for `archive_hours` (24 since the 2-minute
cadence cutover) before the archive job flushes it to R2. So for any day
older than yesterday, the positions have to come from somewhere else.

WHERE THE POSITIONS COME FROM -------------------------------------------
`device_history` (sql/004) is the durable answer. It is an append-only
STOP log: one row per (vehicle, place-it-parked), with `snapshot_time`
when the vehicle arrived and `departed_at` when it left (NULL = still
there). That makes it an interval table, and the fleet at any past
instant T is every stop whose interval covers T:

    snapshot_time <= T AND (departed_at IS NULL OR departed_at > T)

The statuses on those rows are the spatially-CORRECTED ones (see
src/cycle.py: device_state is fed `corrected_devices`, post-buffer,
post-polygon-refinement), so filtering to `denver_core` reconstructs the
same population `total_devices_denver` counts — not the rough bbox.

WHAT THE RECONSTRUCTION CANNOT RECOVER ----------------------------------
Two known gaps, both handled explicitly rather than papered over:

1. **`vehicle_use_type` is not on `device_history`.** The sitting/standing
   split (sql/017) cannot be rebuilt, so `total_sitting_equity`,
   `percent_standing_equity` and friends are left NULL for reprocessed
   snapshots. NULL is the truthful value and `AVG()` skips it, so a
   reprocessed day reports no sitting/standing figure rather than a
   confident zero. The form_factor split (bike/scooter) IS on the row and
   is rebuilt.

2. **Ghost stops.** `departed_at` is stamped only on a MOVED transition
   (src/device_state.py). A vehicle that simply leaves the feed — pulled
   for repair, retired — keeps an open stop forever and would be counted
   as parked long after it was gone. A vehicle that comes back somewhere
   else closes its own stop retroactively, so this only bites permanently
   retired ones, but it biases the count upward and there is no column
   that would tell us which.

   The control for (2) is the FIDELITY GATE below, not a heuristic. Every
   snapshot row already records `total_devices_denver` as observed at the
   time — ground truth. The reconstruction is compared against it, and a
   snapshot whose reconstructed fleet differs by more than
   `max_drift` is SKIPPED, not written. A percentage we cannot stand
   behind is worth less than an honest NULL, and the skip count is
   reported so a bad day shows up as a bad day.

   For the same reason both sides of every ratio come from the
   reconstruction: mixing a reconstructed numerator with the recorded
   denominator would fold the entire ghost bias straight into the
   compliance percentage. Within-reconstruction ratios cancel it to the
   extent ghosts are distributed like the live fleet, and the gate bounds
   what is left.

WHAT IT WRITES ----------------------------------------------------------
The `*_equity` columns on `snapshot_metadata_core` (sql/079), then
`daily_sla.compute_for_date()` for the day, which re-averages the window
and stamps `compliance_equity_pass`. Both steps are idempotent, so
re-running a day is free and a half-finished run is not a hazard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import daily_sla
from .config import BoundaryLayer, load
from .duck import session
from .equity_groups import OFFICIAL_GROUP
from .pg import connection
from .sentry import capture_exception

log = logging.getLogger(__name__)

DENVER_TZ = daily_sla.DENVER_TZ

#: How far the reconstructed Denver-core fleet may differ from the count the
#: cycle actually recorded before the snapshot is rejected as unreliable.
#: 0.10 = ±10%. Tightening this trades coverage for confidence; loosening it
#: does the reverse. It is NOT a tuning knob for "make more days green" —
#: skipped snapshots are reported, so widening it to chase coverage shows up
#: in the fidelity numbers.
DEFAULT_MAX_DRIFT = 0.10

#: Default sweep depth for the scheduled job: how many prior Denver-local
#: days to consider in one firing. Bounded so a single run cannot walk the
#: whole history and time out; the sweep is idempotent, so a deep backlog
#: just takes a few nights.
DEFAULT_LOOKBACK_DAYS = 14

#: The columns this module can rebuild. Deliberately NOT
#: `core_metric_columns((OFFICIAL_GROUP,))` — that list includes the
#: sitting/standing split, which device_history cannot reconstruct (see the
#: module docstring). Writing those as anything but NULL would be inventing
#: numbers.
REBUILT_COLUMNS: tuple[str, ...] = (
    "total_devices_equity",
    "total_bike_equity",
    "total_scooter_equity",
    "percent_all_devices_equity",
    "percent_all_bikes_equity",
    "percent_all_scooters_equity",
    "percent_bikes_equity",
    "percent_scooters_equity",
)


# ---------------------------------------------------------------------------
# Boundary geometry
# ---------------------------------------------------------------------------
def official_layer() -> BoundaryLayer:
    """The configured boundary layer for the official Equity Area map."""
    for b in load().boundaries:
        if b.region_type == OFFICIAL_GROUP:
            return b
    raise RuntimeError(
        f"no boundary configured for region_type {OFFICIAL_GROUP!r} — "
        "add it to config.json's `boundaries` list"
    )


# ---------------------------------------------------------------------------
# Fleet reconstruction
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Stop:
    """One `device_history` row, reduced to what the rebuild needs."""
    vehicle_identifier: str
    arrived: datetime
    departed: datetime | None
    lat: float
    lon: float
    form_factor: str | None
    #: True when the parked position falls inside the official Equity Area
    #: map. Resolved once per stop, not once per (stop, cycle).
    in_equity: bool = False

    def covers(self, t: datetime) -> bool:
        return self.arrived <= t and (self.departed is None or self.departed > t)


@dataclass
class DayResult:
    """What one day's reprocessing did. Returned by `reprocess_date` and
    aggregated into the CLI's summary."""
    sla_date: date_cls
    snapshots_considered: int = 0
    snapshots_written: int = 0
    snapshots_skipped_low_fidelity: int = 0
    snapshots_skipped_no_history: int = 0
    #: reconstructed / recorded Denver-core fleet, per written snapshot.
    fidelity: list[float] = field(default_factory=list)
    avg_percent_all_devices_equity: float | None = None
    compliance_equity_pass: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        fid = self.fidelity
        return {
            "sla_date": self.sla_date.isoformat(),
            "snapshots_considered": self.snapshots_considered,
            "snapshots_written": self.snapshots_written,
            "snapshots_skipped_low_fidelity": self.snapshots_skipped_low_fidelity,
            "snapshots_skipped_no_history": self.snapshots_skipped_no_history,
            "fidelity_min": round(min(fid), 4) if fid else None,
            "fidelity_max": round(max(fid), 4) if fid else None,
            "fidelity_mean": round(sum(fid) / len(fid), 4) if fid else None,
            "avg_percent_all_devices_equity": self.avg_percent_all_devices_equity,
            "compliance_equity_pass": self.compliance_equity_pass,
        }


def _bounds(d: date_cls, window_only: bool) -> tuple[datetime, datetime]:
    """UTC [start, end) for one Denver-local day.

    `window_only` gives the 6-9 AM window the contract measures — the only
    snapshots the daily average reads. The full day is available for
    rebuilding the whole series (e.g. to chart it), at ~8x the work.
    """
    if window_only:
        return daily_sla.window_for_date(d)
    start_local = datetime.combine(d, time(0, 0), tzinfo=DENVER_TZ)
    end_local = datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=DENVER_TZ)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _load_snapshots(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Recorded snapshots in [start, end), with the denominators the rebuild
    is checked against."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cycle_id, snapshot_time, total_devices_denver,
                       total_bike_denver, total_scooter_denver
                FROM snapshot_metadata_core
                WHERE snapshot_time >= %s AND snapshot_time < %s
                ORDER BY snapshot_time
                """,
                (start, end),
            )
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def _load_stops(start: datetime, end: datetime) -> list[Stop]:
    """Every Denver-core stop whose interval overlaps [start, end).

    The selective half of this predicate is `departed_at` (served by
    sql/079's index): `snapshot_time < end` alone matches nearly the whole
    table for any day but the newest.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT vehicle_identifier, snapshot_time, departed_at,
                       lat, lon, form_factor
                FROM device_history
                WHERE spatial_status = 'denver_core'
                  AND snapshot_time < %s
                  AND (departed_at IS NULL OR departed_at > %s)
                """,
                (end, start),
            )
            return [
                Stop(
                    vehicle_identifier=r[0],
                    arrived=r[1],
                    departed=r[2],
                    lat=float(r[3]),
                    lon=float(r[4]),
                    form_factor=r[5],
                )
                for r in cur.fetchall()
            ]


def tag_equity_membership(stops: Iterable[Stop]) -> list[Stop]:
    """Return `stops` with `in_equity` resolved against the official map.

    Uses the same DuckDB spatial predicate as the live pipeline
    (`ST_Within` over `ST_Read` of the same file), so a reprocessed
    percentage and a live one mean the same thing. A hand-rolled ray cast
    in Python would disagree on boundary cases, which is precisely where a
    compliance number gets argued about.
    """
    stops = list(stops)
    if not stops:
        return stops

    layer = official_layer()
    path = Path(layer.file)
    if not path.exists():
        raise FileNotFoundError(f"equity boundary file missing: {path}")

    with session() as con:
        con.execute(
            f"CREATE TABLE eq AS SELECT geom FROM ST_Read('{path}');"
        )
        con.execute("CREATE INDEX idx_eq_geom ON eq USING RTREE (geom);")
        con.execute("CREATE TABLE stops (idx INTEGER, geom GEOMETRY);")
        con.executemany(
            "INSERT INTO stops (idx, geom) VALUES (?, ST_Point(?, ?))",
            [(i, s.lon, s.lat) for i, s in enumerate(stops)],
        )
        inside = {
            r[0]
            for r in con.execute(
                """
                SELECT DISTINCT s.idx
                FROM stops s JOIN eq b ON ST_Within(s.geom, b.geom)
                """
            ).fetchall()
        }

    return [
        Stop(
            vehicle_identifier=s.vehicle_identifier,
            arrived=s.arrived,
            departed=s.departed,
            lat=s.lat,
            lon=s.lon,
            form_factor=s.form_factor,
            in_equity=i in inside,
        )
        for i, s in enumerate(stops)
    ]


def fleet_at(stops: Iterable[Stop], t: datetime) -> list[Stop]:
    """The stops covering instant `t`, one per vehicle.

    `device_history` intervals should not overlap for a single vehicle, but
    a MOVED transition that failed to close its predecessor would produce
    two open stops and double-count the vehicle. Keeping only the LATEST
    arrival per vehicle makes that impossible by construction, and the
    latest arrival is the right one anyway — it is where the vehicle
    actually is.
    """
    latest: dict[str, Stop] = {}
    for s in stops:
        if not s.covers(t):
            continue
        prior = latest.get(s.vehicle_identifier)
        if prior is None or s.arrived > prior.arrived:
            latest[s.vehicle_identifier] = s
    return list(latest.values())


def _pct(num: int, den: int) -> float | None:
    """Percentage rounded to 2dp, or None when the denominator is empty.

    Matches compute.py's `ROUND(a::DOUBLE / NULLIF(b,0) * 100, 2)` — an
    empty denominator is unanswerable, not zero.
    """
    if den <= 0:
        return None
    return round(num / den * 100, 2)


def rebuild_metrics(fleet: list[Stop]) -> dict[str, Any]:
    """The `*_equity` columns for one reconstructed fleet.

    Every ratio's denominator comes from this same fleet — see the module
    docstring on why the recorded denominator is deliberately NOT used.
    """
    denver = len(fleet)
    bike_denver = sum(1 for s in fleet if s.form_factor == "bicycle")
    scooter_denver = sum(1 for s in fleet if s.form_factor == "scooter")

    eq = [s for s in fleet if s.in_equity]
    eq_total = len(eq)
    eq_bike = sum(1 for s in eq if s.form_factor == "bicycle")
    eq_scooter = sum(1 for s in eq if s.form_factor == "scooter")

    return {
        "total_devices_equity": eq_total,
        "total_bike_equity": eq_bike,
        "total_scooter_equity": eq_scooter,
        # "% of all X that are in equity areas" — denominator is the fleet.
        "percent_all_devices_equity": _pct(eq_total, denver),
        "percent_all_bikes_equity": _pct(eq_bike, bike_denver),
        "percent_all_scooters_equity": _pct(eq_scooter, scooter_denver),
        # "of the devices in equity areas, what share are X" — denominator
        # is the equity subset.
        "percent_bikes_equity": _pct(eq_bike, eq_total),
        "percent_scooters_equity": _pct(eq_scooter, eq_total),
    }


def _write_metrics(rows: list[tuple[Any, dict[str, Any]]]) -> int:
    """Persist `(cycle_id, metrics)` pairs onto `snapshot_metadata_core`."""
    if not rows:
        return 0
    set_clause = ", ".join(f"{c} = %({c})s" for c in REBUILT_COLUMNS)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                f"UPDATE snapshot_metadata_core SET {set_clause} WHERE cycle_id = %(cycle_id)s",
                [{**m, "cycle_id": cid} for cid, m in rows],
            )
        conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# One day
# ---------------------------------------------------------------------------
def reprocess_date(
    d: date_cls,
    *,
    window_only: bool = True,
    max_drift: float = DEFAULT_MAX_DRIFT,
    recompute_sla: bool = True,
) -> DayResult:
    """Rebuild one Denver-local day's `*_equity` snapshot columns and
    re-average its daily SLA row.

    Idempotent: every write is an UPDATE keyed by cycle_id and the SLA
    recompute is an upsert, so re-running a day converges rather than
    accumulating.
    """
    start, end = _bounds(d, window_only)
    result = DayResult(sla_date=d)

    snapshots = _load_snapshots(start, end)
    result.snapshots_considered = len(snapshots)
    if not snapshots:
        log.info("equity reprocess %s: no snapshots in %s – %s", d, start, end)
        return result

    stops = tag_equity_membership(_load_stops(start, end))
    log.info(
        "equity reprocess %s: %d snapshots, %d overlapping stops (%d in equity areas)",
        d, len(snapshots), len(stops), sum(1 for s in stops if s.in_equity),
    )

    pending: list[tuple[Any, dict[str, Any]]] = []
    for snap in snapshots:
        recorded = snap["total_devices_denver"]
        fleet = fleet_at(stops, snap["snapshot_time"])
        if not fleet:
            result.snapshots_skipped_no_history += 1
            continue
        # No recorded denominator to check against — an old row from before
        # the column existed, or a cycle that wrote nothing. Unverifiable,
        # so not written.
        if not recorded:
            result.snapshots_skipped_no_history += 1
            continue
        fidelity = len(fleet) / recorded
        if abs(fidelity - 1.0) > max_drift:
            result.snapshots_skipped_low_fidelity += 1
            continue
        result.fidelity.append(fidelity)
        pending.append((snap["cycle_id"], rebuild_metrics(fleet)))

    result.snapshots_written = _write_metrics(pending)

    if recompute_sla and result.snapshots_written:
        row = daily_sla.compute_for_date(d)
        pct = row.get("avg_percent_all_devices_equity")
        result.avg_percent_all_devices_equity = None if pct is None else float(pct)
        result.compliance_equity_pass = row.get("compliance_equity_pass")

    log.info("equity reprocess %s: %r", d, result.as_dict())
    return result


# ---------------------------------------------------------------------------
# Backlog sweep (the scheduled entry point)
# ---------------------------------------------------------------------------
def days_needing_reprocess(
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    *,
    today: date_cls | None = None,
) -> list[date_cls]:
    """Prior Denver-local days whose SLA row has no equity figure yet.

    Only days with an existing `daily_sla_compliance` row are candidates:
    a day the daily job never computed is that job's backlog, not this
    one's, and inventing a row here would hide the gap. Today is excluded
    — its window may not have closed.
    """
    today = today or datetime.now(DENVER_TZ).date()
    earliest = today - timedelta(days=lookback_days)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sla_date
                FROM daily_sla_compliance
                WHERE sla_date >= %s AND sla_date < %s
                  AND avg_percent_all_devices_equity IS NULL
                ORDER BY sla_date
                """,
                (earliest, today),
            )
            return [r[0] for r in cur.fetchall()]


def reprocess_range(
    start: date_cls,
    end: date_cls,
    *,
    window_only: bool = True,
    max_drift: float = DEFAULT_MAX_DRIFT,
) -> list[DayResult]:
    """Inclusive [start, end], oldest first."""
    if end < start:
        raise ValueError("end < start")
    out: list[DayResult] = []
    d = start
    while d <= end:
        out.append(reprocess_date(d, window_only=window_only, max_drift=max_drift))
        d += timedelta(days=1)
    return out


def run_backlog(
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    *,
    max_drift: float = DEFAULT_MAX_DRIFT,
) -> dict[str, Any]:
    """Scheduled entry point: fill in whatever equity history is missing.

    Never raises. Like `daily_sla.run_daily`, a scheduled job that dies
    takes the next night's run with it if the scheduler treats the failure
    as fatal; a reported failure is more useful than a stack trace at 3 AM.
    A single day's failure does not abandon the rest of the backlog.
    """
    try:
        days = days_needing_reprocess(lookback_days)
    except Exception as e:  # noqa: BLE001
        log.exception("equity reprocess: could not list the backlog")
        capture_exception(e)
        return {"days_found": 0, "days_reprocessed": 0, "error": f"{type(e).__name__}: {e}"}

    results: list[dict[str, Any]] = []
    failed: list[str] = []
    for d in days:
        try:
            results.append(reprocess_date(d, max_drift=max_drift).as_dict())
        except Exception as e:  # noqa: BLE001
            log.exception("equity reprocess failed for %s", d)
            capture_exception(e)
            failed.append(d.isoformat())

    return {
        "days_found": len(days),
        "days_reprocessed": len(results),
        "days_failed": failed,
        "snapshots_written": sum(r["snapshots_written"] for r in results),
        "snapshots_skipped_low_fidelity": sum(
            r["snapshots_skipped_low_fidelity"] for r in results
        ),
        "days": results,
    }
