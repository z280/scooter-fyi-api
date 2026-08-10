"""Empirical battery-burn model: extraction, fit, and serving.

Replaces a static per-type estimate with a regression on observed trips:

    burn% = b0 + b1*distance_m + b2*elevation_gain_m + b3*temperature_C

Three deliberate departures from the obvious implementation, each forced by the
data (see sql/024_battery_model.sql for the long form):

* The target is state-of-charge PERCENT, recovered through the fleet-wide
  lookup table in data/range_soc_lut.json. ``current_range_meters`` is a
  nonlinear vendor re-encoding of an integer percent and is identical across all
  three vehicle models, so regressing on metres would fit the vendor curve.
* A trip is a RESERVATION EPISODE — a run of consecutive observations with
  ``is_reserved`` true, bracketed by the last available sample before it and
  the first available sample after. See THE ANCHOR below; this replaced an
  observation-gap model on 2026-08-10 and is the reason this pipeline produces
  anything at all. Neither ``trip_events`` (no duration) nor ``device_history``
  works: measured over 1.37M stops, device_history's ``departed_at`` equals the
  next stop's ``snapshot_time`` at p50, p90 and mean, because it records the
  detecting cycle rather than the departure.
* Distance and elevation are Valhalla's routed values, routed THROUGH the
  in-ride waypoints rather than origin-to-destination;
  ``trip_events.distance_meters`` is a documented flat-earth approximation.

THE ANCHOR (2026-08-10)
-----------------------
This module used to define a trip as an OBSERVATION GAP: "a rented vehicle
drops out of GBFS free_bike_status, so a ride shows up as two consecutive
observations 10-30 minutes apart with a position jump between them."

Veo does not drop rented vehicles. It keeps them listed for the whole rental,
sampled every 2 minutes, broadcasting their live moving position, with
``is_reserved`` true (see src/ride_watch.py's own measurement, and the
correction note in API_REQUIREMENTS.md). A real rental therefore produces NO
observation gap at all, and the old anchor was mining feed outages that
happened to coincide with movement.

Worse, the 10-30 minute window was calibrated for the pre-2026-07-07
**10-minute** ingest cadence, where it meant 1-3 missed observations. At the
2-minute cadence it demanded a vehicle be missing for 5-15 consecutive cycles.
The module's own note recorded the resulting yield without questioning it:
11.7M consecutive pairs -> 17,401 in-window -> 1,146 that moved -> 243 usable.
``battery_trip_observations`` held 31 rows in total and
``battery_model_coefficients`` was empty; /api/v1/route reported
``battery_model: "unavailable"`` from the day it shipped.

Anchored on reservation episodes instead, over the same fleet on 2026-08-09:
**29,754 episodes with both battery endpoints, 24,954 usable** after the SoC
filters. Burn is p10=2, p50=8, p90=25 percentage points and only 11.9% of
episodes burn less than one SoC step — so the quantization worry that justified
the old 10-minute floor does not apply to a rental-anchored trip.

WHY THE WAYPOINTS MATTER
------------------------
Because the vehicle stays in the feed while it is ridden, Veo hands us a
position every 2 minutes for the whole rental. Routing origin-to-destination
throws that away, and it is not a small loss: measured over 250 episodes, the
waypoint route is 1.32x the direct route at p50 and 3.87x at p90, and 6% of
episodes are loops that return within 400 m of where they started while
actually covering more than 800 m. Under a direct route those arrive in the
regression as a large burn over almost no distance, which is precisely the
shape that wrecks an intercept. Waypoint routing succeeds on 98% of episodes;
the direct two-point route is kept as the fallback for the rest.

DO NOT use mid-ride ``current_range_meters`` as a battery reading. It is a
load-compensated range ESTIMATE, not a fuel gauge: 30.2% of in-ride steps show
range going UP, 20.2% of them by over a kilometre. Only the bracketing
available samples are usable, and those need no settling delay (median
under-read against the 10th post-release sample is 0 m at every offset).

The fit is an ordinary least squares solve via ``numpy.linalg.lstsq`` — four
terms does not justify a scikit-learn dependency, in the spirit of
``src/polyline.py``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import valhalla, weather
from .pg import connection
from .quality import compute_battery_percent

log = logging.getLogger(__name__)

# --- Anchor filter ----------------------------------------------------------
#
# Every bound here was re-derived against 24,954 real reservation episodes
# (2026-08-09) when the anchor changed — see THE ANCHOR in the module
# docstring. The old values were calibrated for gap-anchored trips and are
# actively wrong for rentals: the old 1-mile distance floor kept 33% of
# episodes and the old 8 mph floor kept 8%.
#
# The span is measured between the two BRACKETING available samples, not
# between the first and last reserved sample: that is the interval over which
# the battery delta was actually observed, so it is the interval the burn
# belongs to. It runs up to one cycle long at each end (the flag sets ~1 cycle
# after the rider unlocks and clears ~1 cycle after they park), which is why
# the speed floor below is well under a plausible riding speed.
#
# 240 s is structural, not a judgement: one reserved sample bracketed by two
# available samples at the 2-minute cadence spans exactly that.
MIN_DURATION_S = 240
# p90 of the observed span is 1680 s. An hour is generous for a real rental
# and excludes a reservation someone opened and abandoned.
MAX_DURATION_S = 60 * 60
# Routed distance floor, applied to the WAYPOINT route — i.e. to distance
# actually ridden, not to displacement. The old 1-mile floor existed because a
# shorter gap-anchored trip "burns less than one SoC step"; measured burn on
# rental episodes is p10=2 / p50=8 percentage points, and _accept_pair already
# drops the zero-burn cases outright, so that reasoning does not transfer.
MIN_DISTANCE_METERS = 400.0
# Implied average speed floor over the span. Routed-distance-over-span sits at
# roughly p10=4 / p50=10 mph once the bracketing cycles are included, so this
# is deliberately low: its only job is to reject a vehicle that was reserved
# for a long time and barely moved. Operator vans, which the old 8 mph floor
# was partly aimed at, do not reserve at all — and _rebalance_keys still backs
# this up.
MIN_IMPLIED_MPH = 3.0
# How far back the episode scan reaches BEYOND the reporting window, so that an
# episode already under way at window_start still has its bracketing `pre`
# sample in scope. See the window-edge note above _RENTAL_EPISODES_SQL. One
# MAX_DURATION_S is the tight bound: no qualifying episode is longer.
EPISODE_BRACKET_LOOKBACK_S = MAX_DURATION_S
# Cap on how many in-ride samples are fed to Valhalla as via-points. At
# MAX_DURATION_S and a 2-minute cadence an episode cannot exceed 30, so this
# only ever bites if the cadence changes.
MAX_ROUTE_WAYPOINTS = 40
# A positive jump of this size or more is a battery swap, not a ride.
# Inherited from scripts/analyze_range_signal.py, which established it against
# the R2 archive.
SWAP_JUMP_PCT = 20.0
# Sanity ceiling on a single trip's burn.
MAX_BURN_PCT = 60.0

METERS_PER_MILE = 1609.34


def _implied_mph(distance_meters: float, duration_seconds: float) -> float:
    if duration_seconds <= 0:
        return 0.0
    return (distance_meters / METERS_PER_MILE) / (duration_seconds / 3600.0)


# --- Extraction --------------------------------------------------------------
#
# A trip is a RESERVATION EPISODE. See THE ANCHOR in the module docstring for
# why, and for what the observation-gap model it replaced actually mined.
#
# The shape below is one query in two dialects (Postgres over the hot
# raw_telemetry_points buffer, DuckDB over an R2 archive file) producing the
# same candidate dict, so everything downstream — _accept_pair,
# _rebalance_keys, _route_and_store — is shared:
#
#   grouped   a running count of reservation STARTS per vehicle, which labels
#             every sample with the episode it belongs to. Episode k's rows are
#             the reserved run k followed by the available rows up to the next
#             reservation, so...
#   pre       ...the last available row of episode k-1 is the vehicle at the
#             kerb immediately before rental k: its origin, and its battery at
#             rest. Relabelled to k for the join.
#   post      the first available row of episode k is the drop point and the
#             battery at rest afterwards.
#   ride      the reserved run itself — the in-ride track, which becomes the
#             via-points for routing.
#
# BOTH window edges cut episodes, and they cut them differently:
#
#   window END    an episode still reserved at the edge has no `post` row and
#                 is dropped by the join. The next run picks it up once it
#                 closes. Self-healing, nothing to do.
#   window START  an episode already under way at the edge has its `pre`
#                 sample — the kerb it left from, and its battery at rest —
#                 BEFORE the window, so the join drops it too. That one is NOT
#                 self-healing: the next run's window starts even later, so the
#                 episode is lost for good, and it is lost systematically at
#                 whatever hour the job happens to run.
#
# Hence EPISODE_BRACKET_LOOKBACK_S: the scan reaches back beyond the reporting
# window far enough to guarantee a bracketing `pre` for any episode starting
# just inside it. Over-inclusion is free — re-mining an episode the previous
# run already stored hits the (vehicle_identifier, departed_at) NOT EXISTS
# below, and the ON CONFLICT at insert time behind that.
#
# The archive backfill has the same asymmetry at file boundaries and CANNOT fix
# it this way, because a file is all there is; an episode straddling two
# archive files is simply lost. At ~28k episodes a day against 18 files that is
# a rounding error, and it is called out in
# _rental_episodes_from_archive_file's docstring rather than papered over.
#
# NO STRAIGHT-LINE PRE-FILTER. The gap model used one to avoid paying for a
# Valhalla call on a pair that had not moved. It cannot be used here: 6% of
# episodes are loops that end within 400 m of their origin while covering more
# than 800 m of real riding, and those are exactly the observations a
# displacement filter would throw away.

_RENTAL_EPISODES_SQL = """
WITH obs AS (
    SELECT vehicle_identifier, vehicle_model_name, snapshot_time,
           latitude, longitude, current_range_meters,
           COALESCE(is_reserved, FALSE) AS reserved,
           COALESCE(is_disabled, FALSE) AS disabled
    FROM raw_telemetry_points
    WHERE spatial_status = 'denver_core'
      AND vehicle_identifier IS NOT NULL
      -- scan_start = window_start - EPISODE_BRACKET_LOOKBACK_S; the reporting
      -- window itself is applied to the episode below, not to the scan.
      AND snapshot_time >= %(scan_start)s
      AND snapshot_time <  %(window_end)s
),
marked AS (
    SELECT *, COALESCE(LAG(reserved) OVER w, FALSE) AS prev_reserved
    FROM obs
    WINDOW w AS (PARTITION BY vehicle_identifier ORDER BY snapshot_time)
),
grouped AS (
    SELECT *,
           SUM(CASE WHEN reserved AND NOT prev_reserved THEN 1 ELSE 0 END)
               OVER (PARTITION BY vehicle_identifier ORDER BY snapshot_time
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS episode
    FROM marked
),
ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY vehicle_identifier, episode, reserved
                              ORDER BY snapshot_time) AS rn,
           COUNT(*) OVER (PARTITION BY vehicle_identifier, episode, reserved) AS n
    FROM grouped
),
pre AS (
    SELECT vehicle_identifier, episode + 1 AS episode, vehicle_model_name,
           snapshot_time, latitude, longitude, current_range_meters, disabled
    FROM ranked WHERE NOT reserved AND rn = n
),
post AS (
    SELECT vehicle_identifier, episode, snapshot_time, latitude, longitude,
           current_range_meters
    FROM ranked WHERE NOT reserved AND rn = 1
),
ride AS (
    SELECT vehicle_identifier, episode, COUNT(*) AS waypoint_count,
           ARRAY_AGG(ARRAY[latitude, longitude] ORDER BY snapshot_time) AS waypoints
    FROM ranked WHERE reserved
    GROUP BY vehicle_identifier, episode
)
SELECT
    pre.vehicle_identifier,
    pre.vehicle_model_name,
    pre.snapshot_time  AS departed_at,
    post.snapshot_time AS arrived_at,
    EXTRACT(EPOCH FROM (post.snapshot_time - pre.snapshot_time)) AS duration_seconds,
    pre.latitude, pre.longitude,
    post.latitude  AS lat2,
    post.longitude AS lon2,
    pre.current_range_meters  AS range_start,
    post.current_range_meters AS range_end,
    ride.waypoint_count,
    ride.waypoints
FROM pre
JOIN post USING (vehicle_identifier, episode)
JOIN ride USING (vehicle_identifier, episode)
WHERE pre.current_range_meters IS NOT NULL
  AND post.current_range_meters IS NOT NULL
  -- Disabled is out-of-service, and a van moving a broken vehicle is not a
  -- ride. Filtered on the bracketing sample rather than in `obs`, because
  -- dropping rows mid-run would split one episode into several.
  AND NOT pre.disabled
  -- The episode belongs to this run if it ENDED inside the reporting window.
  -- Anything wholly inside the lookback was already mined by the previous run
  -- and is filtered by the NOT EXISTS below; this just avoids paying to route
  -- it again.
  AND post.snapshot_time >= %(window_start)s
  AND EXTRACT(EPOCH FROM (post.snapshot_time - pre.snapshot_time))
        BETWEEN %(min_s)s AND %(max_s)s
  AND NOT EXISTS (
      SELECT 1 FROM battery_trip_observations b
      WHERE b.vehicle_identifier = pre.vehicle_identifier
        AND b.departed_at = pre.snapshot_time
  )
  -- Double-count guard (sql/051 / PLAN_RIDE_MODE_API.md phase A2 "Battery
  -- ingestion"): a donated ride's window can straddle this episode without
  -- sharing its exact departed_at, so the exact-match NOT EXISTS above would
  -- miss it. ingest_donated_observation() handles the inverse direction.
  AND NOT EXISTS (
      SELECT 1 FROM battery_trip_observations d
      WHERE d.vehicle_identifier = pre.vehicle_identifier
        AND d.source = 'donated_ride'
        AND d.departed_at < post.snapshot_time
        AND d.arrived_at  > pre.snapshot_time
  )
-- RANDOM, not most-recent-first. There are ~25k qualifying episodes a day
-- against a limit in the low thousands, and ORDER BY snapshot_time DESC would
-- mean every run mines the same few hours of the day — a systematic
-- time-of-day, and therefore temperature, bias in a model that has a
-- temperature term. Successive runs accumulate coverage instead; re-runs stay
-- idempotent through the (vehicle_identifier, departed_at) constraint.
ORDER BY RANDOM()
LIMIT %(limit)s
"""


# How far from a trip a cached hourly reading may be and still be used. Without
# a bound, a hole in the cache silently hands a trip a temperature from days
# away and beta_3 absorbs the error as if it were signal.
MAX_TEMPERATURE_GAP_SECONDS = 2 * 3600


def _temperature_at_cur(cur, when: datetime) -> float | None:
    """Same lookup as _temperature_at, over an already-open cursor.

    Split out so ingest_donated_observation (below) — which runs inside a
    caller-managed transaction it does not own a connection object for —
    can reuse the exact same query rather than a second copy that could
    drift from it.
    """
    cur.execute(
        """
        SELECT temperature_c
        FROM hourly_temperature
        WHERE observed_hour BETWEEN %(when)s - %(gap)s * INTERVAL '1 second'
                                AND %(when)s + %(gap)s * INTERVAL '1 second'
        ORDER BY ABS(EXTRACT(EPOCH FROM (observed_hour - %(when)s)))
        LIMIT 1
        """,
        {"when": when, "gap": MAX_TEMPERATURE_GAP_SECONDS},
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def _temperature_at(conn, when: datetime) -> float | None:
    with conn.cursor() as cur:
        return _temperature_at_cur(cur, when)


# A van rebalancing a batch moves several scooters between the same two places
# at the same time; a rider moves one. Any group of this many candidates sharing
# an origin cell, a destination cell and an overlapping time window is treated
# as a rebalance and dropped. The 8 mph floor does not catch these — a van
# easily clears it.
REBALANCE_MIN_GROUP = 3
# ~250 m of latitude. Coarse enough that a van's drop-offs land in one cell,
# fine enough that unrelated riders rarely collide.
REBALANCE_CELL_DEGREES = 0.0025
REBALANCE_WINDOW_SECONDS = 20 * 60


def _rebalance_keys(candidates: list[dict]) -> set:
    """Group keys that look like operator rebalancing rather than rides."""
    from collections import defaultdict

    groups: dict[tuple, set] = defaultdict(set)
    for c in candidates:
        key = (
            round(float(c["latitude"]) / REBALANCE_CELL_DEGREES),
            round(float(c["longitude"]) / REBALANCE_CELL_DEGREES),
            round(float(c["lat2"]) / REBALANCE_CELL_DEGREES),
            round(float(c["lon2"]) / REBALANCE_CELL_DEGREES),
            int(c["departed_at"].timestamp() // REBALANCE_WINDOW_SECONDS),
        )
        groups[key].add(c["vehicle_identifier"])
    return {k for k, vehicles in groups.items() if len(vehicles) >= REBALANCE_MIN_GROUP}


def _rebalance_key(candidate: dict) -> tuple:
    return (
        round(float(candidate["latitude"]) / REBALANCE_CELL_DEGREES),
        round(float(candidate["longitude"]) / REBALANCE_CELL_DEGREES),
        round(float(candidate["lat2"]) / REBALANCE_CELL_DEGREES),
        round(float(candidate["lon2"]) / REBALANCE_CELL_DEGREES),
        int(candidate["departed_at"].timestamp() // REBALANCE_WINDOW_SECONDS),
    )


def _accept_pair(candidate: dict, stats: dict) -> dict | None:
    """Apply the SoC and anchor filters that need no Valhalla call.

    Returns the enriched candidate, or None (having counted a rejection).
    """
    soc_start = compute_battery_percent(candidate["range_start"])
    soc_end = compute_battery_percent(candidate["range_end"])
    if soc_start is None or soc_end is None:
        stats["rejected_soc"] += 1
        return None

    burn = soc_start - soc_end
    if burn <= -SWAP_JUMP_PCT:
        stats["rejected_swap"] += 1
        return None
    if burn == 0:
        # Counted, never stored. The SoC grid is ~1 percentage point, so a short
        # trip can burn less than one step; keeping those would drag the
        # intercept toward zero burn.
        stats["zero_delta"] += 1
        return None
    if burn < 0 or burn > MAX_BURN_PCT:
        stats["rejected_soc"] += 1
        return None

    candidate["soc_start"] = soc_start
    candidate["soc_end"] = soc_end
    candidate["burn"] = burn
    return candidate


def _route_and_store(conn, cand: dict, stats: dict) -> bool:
    """Route a candidate through Valhalla, apply the routed anchors, persist."""
    # Temperature FIRST, before paying for a Valhalla call. A row stored with
    # temperature_c NULL is permanently dead weight: train() filters it out, and
    # the (vehicle_identifier, departed_at) dedupe means a later run will never
    # revisit it. Skipping instead leaves the candidate eligible once the
    # weather cache is backfilled.
    temp = _temperature_at(conn, cand["departed_at"])
    if temp is None:
        stats["rejected_no_temperature"] += 1
        return False

    # Route THROUGH the in-ride track, not origin-to-destination — see WHY THE
    # WAYPOINTS MATTER in the module docstring. The direct route is the
    # fallback for the ~2% of episodes whose via-points Valhalla cannot thread
    # (a sample snapped somewhere unreachable, usually), and is what a source
    # without an in-ride track gets.
    origin = (float(cand["latitude"]), float(cand["longitude"]))
    destination = (float(cand["lat2"]), float(cand["lon2"]))
    waypoints = [(float(a), float(b))
                 for a, b in (cand.get("waypoints") or [])][:MAX_ROUTE_WAYPOINTS]

    summary = None
    if waypoints:
        summary = _route_summary([origin, *waypoints, destination])
        if summary is None:
            stats["waypoint_route_failed"] = stats.get("waypoint_route_failed", 0) + 1
    routed_via_waypoints = summary is not None
    if summary is None:
        summary = _route_summary([origin, destination])
    if summary is None:
        stats["no_route"] += 1
        return False

    distance = summary["distance_meters"]
    if distance is None or distance < MIN_DISTANCE_METERS:
        stats["rejected_distance"] += 1
        return False

    mph = _implied_mph(distance, float(cand["duration_seconds"]))
    if mph < MIN_IMPLIED_MPH:
        stats["rejected_speed"] += 1
        return False

    if routed_via_waypoints:
        stats["routed_via_waypoints"] = stats.get("routed_via_waypoints", 0) + 1

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO battery_trip_observations (
                vehicle_identifier, vehicle_model_name, departed_at, arrived_at,
                duration_seconds, from_lat, from_lon, to_lat, to_lon,
                route_distance_meters, elevation_gain_meters, temperature_c,
                soc_start_percent, soc_end_percent, burn_percent, implied_mph,
                source, waypoint_count
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (vehicle_identifier, departed_at) DO NOTHING
            """,
            (cand["vehicle_identifier"], cand["vehicle_model_name"],
             cand["departed_at"], cand["arrived_at"], float(cand["duration_seconds"]),
             cand["latitude"], cand["longitude"], cand["lat2"], cand["lon2"],
             distance, summary["elevation_gain_meters"], temp,
             cand["soc_start"], cand["soc_end"], cand["burn"], mph,
             "gbfs_rental",
             # NULL, not 0, when the direct route was used: "we had no track"
             # and "the track was one point" are different provenance, and
             # sql/070's own comment leans on the distinction.
             len(waypoints) if routed_via_waypoints else None),
        )
    return True


def _route_summary(points: list[tuple[float, float]]) -> dict | None:
    """Valhalla summary for one ordered list of points, or None if it will
    not route. Split out so _route_and_store can try the waypoint route and
    fall back to the direct one without duplicating the error handling."""
    try:
        body = valhalla.route(points, costing_options={"bicycle_type": "Hybrid"})
    except valhalla.ValhallaError:
        return None
    trips = valhalla.all_trips(body)
    if not trips:
        return None
    return valhalla.trip_summary(trips[0])


def extract_trips(hours: int = 26, limit: int = 2000) -> dict[str, Any]:
    """Mine the hot telemetry buffer for reservation episodes and persist the
    good ones.

    Runs against ``raw_telemetry_points``, which ``archive_if_due`` flushes to
    R2 and TRUNCATES — so the buffer only ever holds the hours since the last
    archive, and the window here is sized to overlap that boundary rather than
    to be complete. (config's ``archive_hours`` is 24, but the archive objects
    in R2 land every 48 h; the window is deliberately not derived from either.)
    Completeness is not the constraint anyway: there are far more qualifying
    episodes per day than ``limit`` takes. Re-runs are idempotent via the
    (vehicle_identifier, departed_at) unique constraint.

    ``limit`` is a real cap, not a formality: there are ~25k qualifying
    episodes a day. The query samples RANDOMLY rather than most-recent-first
    precisely because of it — see the ORDER BY note in _RENTAL_EPISODES_SQL.
    Successive daily runs accumulate coverage across the whole day instead of
    re-mining the same few hours. See ``backfill_trips_from_archive`` to seed
    from history rather than waiting.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=hours)

    # Temperature must cover the window before any row is written, or every
    # observation lands with a NULL the regression then has to discard.
    weather.ensure_coverage(window_start.date(), now.date())

    stats = {
        "candidates": 0, "accepted": 0, "zero_delta": 0,
        "rejected_soc": 0, "rejected_distance": 0, "rejected_speed": 0,
        "rejected_swap": 0, "rejected_rebalance": 0,
        "rejected_no_temperature": 0, "no_route": 0,
        "routed_via_waypoints": 0, "waypoint_route_failed": 0,
    }

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_RENTAL_EPISODES_SQL, {
                "window_start": window_start,
                "scan_start": window_start - timedelta(
                    seconds=EPISODE_BRACKET_LOOKBACK_S),
                "window_end": now,
                "min_s": MIN_DURATION_S,
                "max_s": MAX_DURATION_S,
                "limit": limit,
            })
            cols = [d[0] for d in cur.description]
            candidates = [dict(zip(cols, row)) for row in cur.fetchall()]

        stats["candidates"] = len(candidates)
        rebalanced = _rebalance_keys(candidates)
        log.info("battery extraction: %d candidate episodes in the last %dh "
                 "(%d rebalance groups excluded)", len(candidates), hours, len(rebalanced))

        for cand in candidates:
            if _rebalance_key(cand) in rebalanced:
                stats["rejected_rebalance"] += 1
                continue
            enriched = _accept_pair(cand, stats)
            if enriched is None:
                continue
            if _route_and_store(conn, enriched, stats):
                stats["accepted"] += 1

        # The zero-delta share is the health metric for this pipeline: with a
        # ~1 percentage point SoC grid, a high share means trips are burning
        # less than one step and any fit would be quantization noise. Zeros are
        # never stored, so record the ratio here or it is unrecoverable later.
        scored = stats["accepted"] + stats["zero_delta"]
        stats["zero_delta_fraction"] = (
            round(stats["zero_delta"] / scored, 4) if scored else None)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES ('battery_zero_delta_fraction', %s, NOW())
                ON CONFLICT (key) DO UPDATE
                  SET value = EXCLUDED.value, updated_at = NOW()
                """,
                (str(stats["zero_delta_fraction"]),),
            )
        conn.commit()

    log.info("battery extraction complete: %r", stats)
    return stats


# --- Historical backfill from the R2 parquet archive -------------------------
#
# Seeds the observations table from history instead of waiting weeks for the
# daily job to accumulate. Opt-in and run by hand, because it is the one job
# here that needs more memory than the scheduler's default limit.
#
# MEMORY: archive files are NOT uniform. They span whatever elapsed since the
# previous archive, which is usually 2 days (~12M rows, ~170 MB) but was 7 days
# for raw_20260727 (43M rows, 633 MB) and a month for the oldest. Scanning a
# whole file, the window functions scale with the file, so a fixed memory
# ceiling is a cliff that a large file walks off:
#
#     duckdb.OutOfMemoryException: failed to pin block of size 79.2 MiB
#     (881.1 MiB/953.6 MiB used)
#
# observed on 2026-08-10 after six normal files had succeeded. So the scan is
# CHUNKED BY DAY (_archive_file_days below): one day is ~6M rows whatever the
# file's total span, which bounds memory by cadence rather than by how long the
# archive job happened to sleep. Raising the limit alone would only move the
# cliff to the next oversized file.
#
# Each file is also downloaded to local disk ONCE and chunked from there.
# Chunking straight off s3:// re-reads the whole object per chunk over httpfs —
# measured at 224-256 s per day-chunk against raw_20260727, against seconds for
# the same query on a local copy. 113 GB free against a 633 MB worst case, and
# the copy is removed as soon as the file is done.
#
# Still worth raising the container for the duration, since peak RSS was
# measured at 1,243 MiB and the container default is 1024m:
#
#     docker update --memory 2g --memory-swap 2g scheduler
#     docker compose exec scheduler python -m src.cli backfill_battery_trips
#     docker update --memory 1024m --memory-swap 1024m scheduler
#
# Files are processed one at a time rather than through a single glob: the
# whole archive is ~167M rows, and joining across all of it at once exhausts
# any plausible limit.

ARCHIVE_DUCKDB_MEMORY = "1GB"
# Where an archive file is staged while its day-chunks are scanned. One file at
# a time; see the MEMORY note above.
ARCHIVE_SCRATCH_DIR = "/tmp/battery_backfill"


# Archive files written before the 2026-07-07 cadence cutover are UNUSABLE for
# a reservation-anchored fit, and quietly so — they parse fine and produce
# thousands of rows. Measured per file (episodes/day, median via-points, median
# span, share with <=1 via-point):
#
#   raw_20260703  600 s cadence   13,464/day   1.0 wp   1201 s   64% thin
#   raw_20260706  600 s cadence   18,970/day   1.0 wp   1201 s   60% thin
#   raw_20260808  120 s cadence   28,284/day   4.0 wp    600 s   14% thin
#
# Three problems compound, all in the same direction. At a 600 s cadence a
# median 6-minute rental yields ONE reserved sample or none, so half the
# rentals are invisible and the ones that survive skew long. With one
# via-point the waypoint route degenerates to the direct route, which
# understates ridden distance by ~32% at p50 (see WHY THE WAYPOINTS MATTER).
# And the bracketing samples are 10 minutes apart, so the span carries up to
# 20 minutes of not-riding.
#
# The burn is real, but it gets attributed to an understated distance over an
# inflated span — which inflates beta_1, the distance coefficient, the one
# term the whole model exists to estimate. Excluded by date, not by a
# positional file count, so the boundary survives new archives being written.
ARCHIVE_CADENCE_CUTOVER = date(2026, 7, 8)


def _archive_keys(client, bucket: str) -> list[str]:
    """Archive keys from the 2-minute-cadence era only, oldest first.

    Keys are ``raw/YYYY/MM/DD/raw_YYYYMMDDThhmmssZ.parquet`` and the date is
    when the file was WRITTEN, i.e. the end of the span it covers — so a file
    dated on the cutover still contains pre-cutover samples. The first file
    written strictly after the cutover (2026-07-10, covering 07-08 onward) is
    the first that is wholly 2-minute data.
    """
    keys = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="raw/"):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".parquet"):
                continue
            try:
                written = date(*(int(x) for x in obj["Key"].split("/")[1:4]))
            except (ValueError, TypeError):
                log.warning("archive key with an unparseable date, skipped: %s",
                            obj["Key"])
                continue
            if written <= ARCHIVE_CADENCE_CUTOVER:
                continue
            keys.append(obj["Key"])
    return sorted(keys)


# Episodes to take from any one archive file. A file holds ~56k qualifying
# episodes and there are 14 usable files, so routing all of them would mean
# ~780k Valhalla calls and ~780k rows to fit four coefficients on — hours of
# load for no statistical gain. Sampling per file rather than taking the first
# N keeps the draw spread across every hour of every day in the archive, which
# matters because the model has a temperature term.
BACKFILL_EPISODES_PER_FILE = 1500


def _archive_file_days(con, url: str) -> list[str]:
    """The UTC dates a file covers, oldest first.

    Reads only min/max snapshot_time, which parquet answers from footer
    statistics rather than by scanning. Used to chunk the scan — see the
    MEMORY note above.
    """
    lo, hi = con.execute(
        f"SELECT min(snapshot_time)::VARCHAR, max(snapshot_time)::VARCHAR "
        f"FROM read_parquet('{url}')").fetchone()
    if lo is None or hi is None:
        return []
    start, end = date.fromisoformat(lo[:10]), date.fromisoformat(hi[:10])
    return [(start + timedelta(days=i)).isoformat()
            for i in range((end - start).days + 1)]


def _rental_episodes_from_archive_file(con, url: str, day: str,
                                       limit: int = BACKFILL_EPISODES_PER_FILE) -> list[dict]:
    """The same reservation-episode model as _RENTAL_EPISODES_SQL, in DuckDB
    over one archive file. Kept structurally identical to its Postgres twin so
    the two can be read side by side; the differences are dialect only
    (list_transform for the waypoint array, date_diff for the span) plus the
    two NOT EXISTS de-dupe clauses, which cannot run here because the
    observations table lives in Postgres — backfill relies on the
    (vehicle_identifier, departed_at) ON CONFLICT at insert time instead.

    An episode that straddles two archive files is lost: each file is scanned
    alone, so the episode has its `pre` sample in one file and its `post` in
    the next and no join can see both. The Postgres path solves the equivalent
    problem with EPISODE_BRACKET_LOOKBACK_S; there is no equivalent here, and
    at ~28k episodes a day across 18 files it is a rounding error rather than
    something worth stitching files together for.

    Timestamps come back as text: DuckDB needs pytz to hand a TIMESTAMPTZ to
    Python and the worker image does not ship it.
    """
    sql = f"""
    WITH obs AS (
        SELECT vehicle_identifier, vehicle_model_name, snapshot_time,
               latitude, longitude, current_range_meters,
               coalesce(is_reserved, FALSE) AS reserved,
               coalesce(is_disabled, FALSE) AS disabled
        FROM read_parquet('{url}')
        WHERE spatial_status = 'denver_core' AND vehicle_identifier IS NOT NULL
          -- One day at a time, reaching back far enough for the bracketing
          -- `pre` sample of an episode that starts just after midnight —
          -- the same asymmetry, and the same remedy, as the live path's
          -- EPISODE_BRACKET_LOOKBACK_S.
          AND snapshot_time >= TIMESTAMP '{day} 00:00:00+00'
                               - INTERVAL {EPISODE_BRACKET_LOOKBACK_S} SECOND
          AND snapshot_time <  TIMESTAMP '{day} 00:00:00+00' + INTERVAL 1 DAY
    ),
    marked AS (
        SELECT *, coalesce(lag(reserved) OVER w, FALSE) AS prev_reserved
        FROM obs
        WINDOW w AS (PARTITION BY vehicle_identifier ORDER BY snapshot_time)
    ),
    grouped AS (
        SELECT *,
               sum(CASE WHEN reserved AND NOT prev_reserved THEN 1 ELSE 0 END)
                   OVER (PARTITION BY vehicle_identifier ORDER BY snapshot_time
                         ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS episode
        FROM marked
    ),
    ranked AS (
        SELECT *,
               row_number() OVER (PARTITION BY vehicle_identifier, episode, reserved
                                  ORDER BY snapshot_time) AS rn,
               count(*) OVER (PARTITION BY vehicle_identifier, episode, reserved) AS n
        FROM grouped
    ),
    pre AS (
        SELECT vehicle_identifier, episode + 1 AS episode, vehicle_model_name,
               snapshot_time, latitude, longitude, current_range_meters, disabled
        FROM ranked WHERE NOT reserved AND rn = n
    ),
    post AS (
        SELECT vehicle_identifier, episode, snapshot_time, latitude, longitude,
               current_range_meters
        FROM ranked WHERE NOT reserved AND rn = 1
    ),
    ride AS (
        SELECT vehicle_identifier, episode, count(*) AS waypoint_count,
               list(struct_pack(la := latitude, lo := longitude)
                    ORDER BY snapshot_time) AS waypoints
        FROM ranked WHERE reserved
        GROUP BY vehicle_identifier, episode
    )
    SELECT pre.vehicle_identifier, pre.vehicle_model_name,
           pre.snapshot_time::VARCHAR  AS departed_at,
           post.snapshot_time::VARCHAR AS arrived_at,
           date_diff('second', pre.snapshot_time, post.snapshot_time) AS duration_seconds,
           pre.latitude, pre.longitude,
           post.latitude  AS lat2,
           post.longitude AS lon2,
           pre.current_range_meters  AS range_start,
           post.current_range_meters AS range_end,
           ride.waypoint_count,
           list_transform(ride.waypoints, x -> [x.la, x.lo]) AS waypoints
    FROM pre
    JOIN post USING (vehicle_identifier, episode)
    JOIN ride USING (vehicle_identifier, episode)
    WHERE pre.current_range_meters IS NOT NULL
      AND post.current_range_meters IS NOT NULL
      AND NOT pre.disabled
      -- The episode belongs to this day if it ENDED in it, so an episode
      -- pulled in by the lookback is not also claimed by the previous day.
      AND post.snapshot_time >= TIMESTAMP '{day} 00:00:00+00'
      AND date_diff('second', pre.snapshot_time, post.snapshot_time)
            BETWEEN {MIN_DURATION_S} AND {MAX_DURATION_S}
    -- Random, for the same reason the Postgres path is random: taking the
    -- head of a time-ordered file would sample one part of the day.
    ORDER BY random()
    LIMIT {limit}
    """
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def backfill_trips_from_archive(max_files: int | None = None) -> dict[str, Any]:
    """Seed battery_trip_observations from the R2 parquet archive.

    See the memory note above — raise the container limit before running.
    """
    import duckdb

    from .config import load, r2_credentials

    creds = r2_credentials()
    if creds is None:
        return {"error": "R2 credentials absent"}

    import boto3
    from botocore.client import Config as BotoConfig

    cfg = load().r2
    endpoint = cfg.endpoint_url(creds["account_id"])
    s3 = boto3.client("s3", endpoint_url=endpoint,
                      aws_access_key_id=creds["access_key_id"],
                      aws_secret_access_key=creds["secret_access_key"],
                      config=BotoConfig(signature_version="s3v4"), region_name="auto")
    keys = _archive_keys(s3, creds["bucket"])
    if max_files:
        keys = keys[-max_files:]
    log.info("backfill: %d archive files to process", len(keys))

    stats = {
        "files": 0, "candidates": 0, "accepted": 0, "zero_delta": 0,
        "rejected_soc": 0, "rejected_distance": 0, "rejected_speed": 0,
        "rejected_swap": 0, "rejected_rebalance": 0,
        "rejected_no_temperature": 0, "no_route": 0,
        "routed_via_waypoints": 0, "waypoint_route_failed": 0,
    }

    con = duckdb.connect(":memory:")
    con.execute(f"SET memory_limit='{ARCHIVE_DUCKDB_MEMORY}';")
    con.execute("SET threads=1; SET preserve_insertion_order=false;")
    con.execute("SET temp_directory='/tmp/duck_backfill';")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    for k, v in {
        "s3_endpoint": endpoint.removeprefix("https://"),
        "s3_access_key_id": creds["access_key_id"],
        "s3_secret_access_key": creds["secret_access_key"],
        "s3_region": "auto",
        "s3_url_style": "path",
    }.items():
        con.execute(f"SET {k}='{v}';")

    try:
        with connection() as conn:
            for key in keys:
                log.info("backfill: %s", key)
                local_path = os.path.join(ARCHIVE_SCRATCH_DIR, os.path.basename(key))
                os.makedirs(ARCHIVE_SCRATCH_DIR, exist_ok=True)
                s3.download_file(creds["bucket"], key, local_path)
                days_in_file = _archive_file_days(con, local_path)
                # Spread the file's quota across the days it covers, so a
                # 7-day file does not contribute 7x a 2-day file's weight.
                per_day = max(1, BACKFILL_EPISODES_PER_FILE // max(len(days_in_file), 1))
                candidates = []
                for day in days_in_file:
                    candidates.extend(
                        _rental_episodes_from_archive_file(con, local_path, day, per_day))
                # _rebalance_keys below now runs over a SAMPLE of the file, so
                # it is a weaker backstop than on the live path: a van's
                # drop-offs are less likely to all survive the draw and reach
                # REBALANCE_MIN_GROUP. Acceptable, because under the
                # reservation anchor a rebalancing van never enters the
                # candidate set at all -- vans do not reserve, which is
                # exactly what the 4-10 vehicle simultaneous-movement batches
                # with is_reserved false showed.
                stats["files"] += 1
                stats["candidates"] += len(candidates)

                # Temperature for this file's span, once per file.
                if candidates:
                    days = sorted({c["departed_at"][:10] for c in candidates})
                    weather.ensure_coverage(
                        date.fromisoformat(days[0]), date.fromisoformat(days[-1]))

                for cand in candidates:
                    cand["departed_at"] = datetime.fromisoformat(cand["departed_at"])
                    cand["arrived_at"] = datetime.fromisoformat(cand["arrived_at"])
                rebalanced = _rebalance_keys(candidates)
                for cand in candidates:
                    if _rebalance_key(cand) in rebalanced:
                        stats["rejected_rebalance"] += 1
                        continue
                    enriched = _accept_pair(cand, stats)
                    if enriched is None:
                        continue
                    if _route_and_store(conn, enriched, stats):
                        stats["accepted"] += 1
                conn.commit()
                # The local copy is this file's alone; drop it before pulling
                # the next one so peak disk stays one file, not eighteen.
                try:
                    os.remove(local_path)
                except OSError:
                    log.warning("backfill: could not remove scratch copy %s",
                                local_path)
                log.info("backfill: %s done -> %r", key, stats)
    finally:
        con.close()

    log.info("backfill complete: %r", stats)
    return stats


# --- Fit ---------------------------------------------------------------------

def train(days: int = 60, holdout_days: int = 3) -> dict[str, Any]:
    """Fit the model and append the coefficients. Returns the fit summary."""
    # numpy is a DIRECT requirement (requirements.txt), not a pyarrow
    # transitive one. It was written here as "available via pyarrow", which
    # has not been true since pyarrow 16 made numpy optional; with
    # pyarrow==18.1.0 pinned, nothing pulled numpy in and this function
    # raised ModuleNotFoundError every time the weekly job fired. Still
    # imported lazily, which is the part that was worth keeping: it keeps
    # numpy off the API boot path, where only serving needs to be fast.
    import numpy as np

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)
    holdout_cut = now - timedelta(days=holdout_days)

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT route_distance_meters, elevation_gain_meters,
                       temperature_c, burn_percent, departed_at,
                       vehicle_model_name
                FROM battery_trip_observations
                WHERE departed_at >= %s
                  AND elevation_gain_meters IS NOT NULL
                  AND temperature_c IS NOT NULL
                ORDER BY departed_at
                """,
                (window_start,),
            )
            rows = cur.fetchall()

    if len(rows) < 100:
        msg = f"only {len(rows)} usable observations — refusing to fit"
        log.warning("battery train: %s", msg)
        return {"fitted": False, "reason": msg, "n": len(rows)}

    train_rows = [r for r in rows if r[4] < holdout_cut]
    test_rows = [r for r in rows if r[4] >= holdout_cut]
    if len(train_rows) < 100:
        train_rows, test_rows = rows, []

    # Per-model intercept offsets. A standing Astro and a seated Cosmo do not
    # consume alike, and the fleet mixes all three, so one intercept averages
    # over a real difference. Dummy-coded against the most common model as
    # reference, which keeps `intercept` interpretable on its own.
    counts: dict[str, int] = {}
    for r in train_rows:
        counts[r[5] or "unknown"] = counts.get(r[5] or "unknown", 0) + 1
    reference = max(counts, key=counts.get) if counts else "unknown"
    dummies = [m for m in sorted(counts) if m != reference]

    def design(subset):
        X = np.array([
            [1.0, float(r[0]), float(r[1]), float(r[2])]
            + [1.0 if (r[5] or "unknown") == m else 0.0 for m in dummies]
            for r in subset
        ])
        y = np.array([float(r[3]) for r in subset])
        return X, y

    X, y = design(train_rows)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    predicted = X @ beta
    residuals = y - predicted
    ss_res = float(np.sum(residuals ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
    residual_std = float(np.std(residuals))
    mean_temp = float(np.mean(X[:, 3]))

    holdout_mae = None
    if test_rows:
        Xt, yt = design(test_rows)
        holdout_mae = float(np.mean(np.abs(yt - Xt @ beta)))

    # Reference model is 0 by construction; "_default" is the
    # observation-weighted mean, used when the caller can't name a model — which
    # the route endpoint usually can't, since it prices a route, not a vehicle.
    model_offsets = {reference: 0.0}
    for i, m in enumerate(dummies):
        model_offsets[m] = float(beta[4 + i])
    total_n = sum(counts.values()) or 1
    model_offsets["_default"] = round(
        sum(model_offsets[m] * counts[m] for m in counts) / total_n, 6)

    # Zero-delta share is a property of extraction, not of the stored rows
    # (zeros are never stored), so read back what the last extraction recorded.
    # Left null rather than defaulted to zero — "not measured" and "no
    # quantization loss" are very different claims.
    zero_delta_fraction = None
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM system_state WHERE key = 'battery_zero_delta_fraction'")
            row = cur.fetchone()
    if row and row[0] not in (None, "", "None"):
        try:
            zero_delta_fraction = float(row[0])
        except ValueError:
            pass

    notes = (f"holdout_days={holdout_days} holdout_n={len(test_rows)} "
             f"holdout_mae={holdout_mae:.3f}" if holdout_mae is not None
             else f"holdout_days={holdout_days} holdout_n=0")

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO battery_model_coefficients (
                    window_start, window_end, n_observations,
                    intercept, beta_distance, beta_elevation, beta_temperature,
                    r_squared, residual_std, mean_temperature_c,
                    zero_delta_fraction, model_offsets, notes
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (window_start, now, len(train_rows),
                 float(beta[0]), float(beta[1]), float(beta[2]), float(beta[3]),
                 r2, residual_std, mean_temp, zero_delta_fraction,
                 json.dumps(model_offsets), notes),
            )
        conn.commit()

    result = {
        "fitted": True,
        "n": len(train_rows),
        "intercept": float(beta[0]),
        "beta_distance": float(beta[1]),
        "beta_elevation": float(beta[2]),
        "beta_temperature": float(beta[3]),
        "r_squared": r2,
        "residual_std": residual_std,
        "mean_temperature_c": mean_temp,
        "holdout_mae_percentage_points": holdout_mae,
        "model_offsets": model_offsets,
    }
    log.info("battery train: %r", result)
    if beta[1] <= 0 or beta[2] <= 0:
        log.warning("battery train: expected positive distance and elevation "
                    "coefficients (got b1=%.3g b2=%.3g) — the anchor filter may "
                    "be admitting noise", beta[1], beta[2])
    return result


# --- Serving -----------------------------------------------------------------

_MODEL_CACHE: dict[str, Any] | None = None
# Monotonic timestamp of the last lookup, successful or not. A *negative* result
# has to be cached too: before the first fit — and whenever Postgres is
# unreachable — an uncached miss means every single /api/v1/route request pays a
# database round trip, and a connection failure costs the full pool timeout
# (30s) on each one.
_MODEL_CHECKED_AT: float = 0.0
_MODEL_TTL_SECONDS = 300.0


def latest_model(refresh: bool = False) -> dict[str, Any] | None:
    """Newest fitted coefficients, or None when the model has never been fit."""
    global _MODEL_CACHE, _MODEL_CHECKED_AT
    import time

    fresh = (time.monotonic() - _MODEL_CHECKED_AT) < _MODEL_TTL_SECONDS
    if fresh and not refresh:
        return _MODEL_CACHE

    _MODEL_CHECKED_AT = time.monotonic()
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT intercept, beta_distance, beta_elevation,
                           beta_temperature, mean_temperature_c, r_squared,
                           n_observations, fitted_at, model_offsets
                    FROM battery_model_coefficients
                    ORDER BY fitted_at DESC LIMIT 1
                    """
                )
                row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — never fail a route on a model read
        log.warning("battery model lookup failed: %s", exc)
        _MODEL_CACHE = None
        return None
    if not row:
        _MODEL_CACHE = None
        return None
    _MODEL_CACHE = {
        "intercept": float(row[0]),
        "beta_distance": float(row[1]),
        "beta_elevation": float(row[2]),
        "beta_temperature": float(row[3]),
        "mean_temperature_c": float(row[4]) if row[4] is not None else 15.0,
        "r_squared": float(row[5]) if row[5] is not None else None,
        "n_observations": int(row[6]),
        "fitted_at": row[7].isoformat() if row[7] else None,
        "model_offsets": row[8] or {},
    }
    return _MODEL_CACHE


def estimate_burn_percent(distance_meters: float | None,
                          elevation_gain_meters: float | None,
                          vehicle_model: str | None = None) -> dict[str, Any]:
    """Predicted battery burn for a route, as a percentage of full charge.

    ``vehicle_model`` selects the per-model intercept offset; without it the
    observation-weighted fleet mean is used, which is the right default for the
    route endpoint (it prices a route, not a particular scooter).

    Returns ``{"percent": float|None, "source": str}``. ``source`` is
    ``"regression"`` once a model exists and ``"unavailable"`` before then —
    the caller surfaces it so a client can tell a modelled number from no number
    at all.
    """
    if distance_meters is None:
        return {"percent": None, "source": "unavailable", "reason": "no_distance"}

    model = latest_model()
    if model is None:
        return {"percent": None, "source": "unavailable", "reason": "no_model"}

    temp = weather.current_temperature_c()
    used_fallback = temp is None
    if used_fallback:
        temp = model["mean_temperature_c"]

    climb = elevation_gain_meters if elevation_gain_meters is not None else 0.0
    offsets = model.get("model_offsets") or {}
    if vehicle_model and vehicle_model in offsets:
        offset = float(offsets[vehicle_model])
    else:
        offset = float(offsets.get("_default", 0.0))
    percent = (model["intercept"] + offset
               + model["beta_distance"] * distance_meters
               + model["beta_elevation"] * climb
               + model["beta_temperature"] * temp)
    # A negative predicted burn is nonsense; clamp rather than emit it.
    percent = max(0.0, min(100.0, percent))

    return {
        "percent": round(percent, 1),
        "source": "regression",
        "temperature_c": round(temp, 1) if temp is not None else None,
        "temperature_fallback": used_fallback,
        "elevation_gain_meters": climb,
        "vehicle_model": vehicle_model,
        "model_offset": round(offset, 4),
        "model_fitted_at": model["fitted_at"],
        "model_r_squared": model["r_squared"],
        "model_n": model["n_observations"],
    }


# --- Route adherence (§3G) ---------------------------------------------------

ADHERENCE_THRESHOLD = 0.85


def route_adherence(gps_points: list[tuple[float, float]],
                    proposed_way_ids: set[int]) -> dict[str, Any]:
    """Fraction of a GPS trace's matched length that fell on the proposed route.

    Valhalla reports no confidence score for map matching, so adherence is
    defined explicitly as matched-edge length overlap by OSM way id.
    """
    if len(gps_points) < 2:
        return {"adherent": None, "fraction": None, "reason": "too_few_points"}
    try:
        edges = valhalla.trace_attributes(
            gps_points, {"bicycle_type": "Hybrid"}, shape_match="map_snap")
    except valhalla.ValhallaError as exc:
        log.warning("map matching failed: %s", exc)
        return {"adherent": None, "fraction": None, "reason": "match_failed"}

    total = 0.0
    on_route = 0.0
    for edge in edges:
        length = edge.get("length") or 0.0
        if length <= 0:
            continue
        total += length
        if edge.get("way_id") in proposed_way_ids:
            on_route += length
    if total <= 0:
        return {"adherent": None, "fraction": None, "reason": "no_matched_edges"}

    fraction = on_route / total
    return {
        "adherent": fraction >= ADHERENCE_THRESHOLD,
        "fraction": round(fraction, 4),
        "threshold": ADHERENCE_THRESHOLD,
    }


# --- Donated-ride ingestion (PLAN_RIDE_MODE_API.md phase A2, "Battery
# ingestion"; RIDE_MODE_OVERHAUL_PLAN.md Part 1.4) --------------------------
#
# A verified track donation is a SECOND source for this table, alongside the
# nightly observation-gap mining above — and a better one: a donated trip's
# distance and endpoints come from an HMAC-signed, chain-verified GPS track
# rather than a two-point straight-line inference between feed sightings.
# `source = 'donated_ride'` (sql/051) is what tells the two sources apart,
# and is what the double-count guards on both sides of this boundary key
# off of: the DELETE below (mined -> donated direction) and the extra
# NOT EXISTS clause added to _RENTAL_EPISODES_SQL above (donated -> mined direction).

# Cap on the number of via-points handed to a single Valhalla /route
# request for the elevation re-derivation below. A donated track can carry
# up to ~10,800 points (600 batches x 25 pts, per PLAN_RIDE_MODE_API.md's
# donation cap sanity math) — routing THROUGH that many locations is not
# what /route is for (trace_attributes, called first, is) and risks a
# request Valhalla simply refuses. Downsampling to a still-generous handful
# of via-points keeps the route hugging the actual recorded path (unlike a
# bare start->end route, which can pick a completely different street
# pattern and therefore a wrong elevation profile) while staying well
# inside any sane location limit.
_MAX_ELEVATION_ROUTE_POINTS = 20


def _downsample_for_routing(
    points: list[tuple[float, float]], max_points: int = _MAX_ELEVATION_ROUTE_POINTS,
) -> list[tuple[float, float]]:
    """Evenly-spaced subset of ``points``, always including both ends."""
    if len(points) <= max_points:
        return points
    step = (len(points) - 1) / (max_points - 1)
    indices = sorted({round(i * step) for i in range(max_points)})
    return [points[i] for i in indices]


def _donated_elevation_gain_meters(points: list[tuple[float, float]]) -> float | None:
    """Elevation gain for a donated ride's verified waypoint track.

    PLAN_RIDE_MODE_API.md's A2 "Battery ingestion" section calls for this to
    be "re-derived by map-matching via Valhalla trace_attributes (reuse the
    shade-scoring trace path)". ``valhalla.trace_attributes()``
    (src/valhalla.py — the exact call ``route_adherence()`` above and
    ``src/api_route.py:shade_score()`` both already make) is used here for
    that map-match, over the FULL recorded track, exactly as those two
    callers use it — and if the track fails to snap onto the graph at all,
    that is treated as "the trace call failed": elevation is left NULL and
    nothing raises, without even trying the fallback below.

    DEVIATION, documented rather than silent: the wrapped trace_attributes()
    (src/valhalla.py, not owned by this lane) requests only
    ``edge.way_id``/``edge.length`` — it carries no elevation figure to read
    off the matched edges directly. Elevation is therefore re-derived the
    same way this module's own ``_route_and_store()`` already computes it
    for feed-mined observations: a Valhalla ``/route`` request read through
    ``elevation_gain_meters()``/``trip_summary()`` — but routed THROUGH a
    downsampled subset of the same verified points (``_downsample_for_routing``
    above), not just the two endpoints, so the elevation profile follows the
    path actually ridden rather than whatever street pattern a bare
    two-point route happens to pick. If a future change widens
    trace_attributes' attribute filter (e.g. ``edge.mean_elevation``), this
    can be simplified to read elevation straight off the matched edges.
    """
    if len(points) < 2:
        return None
    try:
        valhalla.trace_attributes(points, {"bicycle_type": "Hybrid"}, shape_match="map_snap")
    except valhalla.ValhallaError as exc:
        log.warning("battery ingestion: donated track failed to map-match, "
                    "elevation left NULL: %s", exc)
        return None

    try:
        body = valhalla.route(
            _downsample_for_routing(points), costing_options={"bicycle_type": "Hybrid"})
    except valhalla.ValhallaError as exc:
        log.warning("battery ingestion: elevation route failed, "
                    "elevation left NULL: %s", exc)
        return None
    trips = valhalla.all_trips(body)
    if not trips:
        return None
    return valhalla.trip_summary(trips[0])["elevation_gain_meters"]


def _resolve_soc(ride_row: dict[str, Any]) -> tuple[float, float] | None:
    """(soc_start, soc_end) percent, or None when either end is unknown.

    soc_start prefers feed_start_battery_percent (sql/049, an independent
    feed-derived reading the rider cannot influence) and falls back to
    reported_start_battery_percent (what the rider read off the vehicle's
    own display) only when the feed had no fresh observation at ride start
    — the same preference order PLAN_RIDE_MODE_API.md's A2 spec states.
    soc_end is always reported_battery_percent (there is no feed-observed
    end battery on tracked_rides — gbfs_end_battery_percent exists but is
    read from the vehicle reappearing on GBFS, an independent corroboration
    signal used elsewhere, not the battery-model's end-of-trip reading).
    """
    start = ride_row.get("feed_start_battery_percent")
    if start is None:
        start = ride_row.get("reported_start_battery_percent")
    end = ride_row.get("reported_battery_percent")
    if start is None or end is None:
        return None
    return float(start), float(end)


def ingest_donated_observation(
    cur, *, ride_row: dict[str, Any], donation_row: dict[str, Any],
) -> dict[str, Any] | None:
    """Insert one ``battery_trip_observations`` row, ``source='donated_ride'``,
    for a verified track donation whose start AND end battery percentages
    are both resolvable — PLAN_RIDE_MODE_API.md phase A2's "Battery
    ingestion". The SOLE way a donated ride's battery signal enters this
    table. Two callers, per the A2 spec:

    * the donation endpoint (owned by another lane), right after a
      donation verifies ``eligible`` with GBFS already resolved at
      donation time;
    * ``src/ride_watch.py:finalize_validation``, on a late
      ``pending_feed`` -> ``eligible`` settle — the only ingestion path
      for a donation that arrived before GBFS resolved.

    ``cur`` is an open cursor in the CALLER's transaction (same contract as
    ``src/points.py:credit_points``) — commit is the caller's
    responsibility, so this lands atomically with whatever settled the
    donation as eligible.

    ``ride_row`` — a plain mapping the caller builds from its own
    ``tracked_rides`` read (this function issues no SELECT against
    ``tracked_rides`` itself); required keys:

        vehicle_identifier              str
        track_key_issued_at             datetime  — ride start; departed_at
        user_reported_ended_at          datetime  — ride end; arrived_at
        feed_start_battery_percent      int | None
        reported_start_battery_percent  float | Decimal | None
        reported_battery_percent        float | Decimal | None  — end battery

    ``donation_row`` — built from the caller's own ``track_donations`` read;
    required keys:

        id               str | uuid.UUID  — donation_id (donated_track_points FK)
        vehicle_model    str | None       — NULL for an unconfirmed model
        distance_meters  float | None     — the verified track distance

    The FIRST and LAST verified waypoints (``from_lat``/``from_lon``,
    ``to_lat``/``to_lon``) are read from ``donated_track_points`` by
    ``donation_row["id"]`` here, rather than accepted as extra parameters —
    this keeps the function self-sufficient for BOTH call sites (the
    donation endpoint already has the full point list close at hand, but
    ``finalize_validation`` does not, and re-deriving it from the same
    stored rows both callers already wrote is cheaper than teaching a
    second caller how to reconstruct it) and gives the map-matched
    elevation re-derivation the FULL recorded track it needs, not just the
    two ends.

    No-op (returns None, writes nothing) when either end's battery percent
    is unresolvable, ``distance_meters`` is unknown, or the donation has no
    stored waypoints — every one of those backs a NOT NULL column on
    ``battery_trip_observations`` (sql/024), so there is nothing honest to
    insert rather than a row of manufactured nulls.

    DOUBLE-COUNT GUARD (sql/051 / A2 spec): before inserting, deletes any
    existing ``battery_trip_observations`` row for this vehicle whose
    ``departed_at`` falls inside [``departed_at``, ``arrived_at``] of THIS
    ride and whose ``source`` is NOT ``'donated_ride'`` (a NULL source
    predates sql/051 and is therefore feed-mined) — the nightly
    ``extract_trips()`` above may already have mined the same trip as a
    lower-quality observation-gap row before this donation landed. Both
    statements run in the caller's transaction, so the delete and the
    insert either both land or neither does.
    """
    soc = _resolve_soc(ride_row)
    if soc is None:
        return None
    soc_start, soc_end = soc

    # REVIEW FIX: the feed-mined path (`_accept_pair`, above) rejects a
    # battery swap (a large jump UP, `burn <= -SWAP_JUMP_PCT`), a zero
    # delta, and any burn outside `(0, MAX_BURN_PCT]` before a candidate
    # ever reaches this table — a donated observation skipped all three of
    # those filters entirely, so e.g. start=5/end=100 (burn=-95) would
    # settle and enter model training uncontested. Apply the SAME bounds
    # here so both sources feed the model equally honest data. Points are
    # unaffected: `credit_battery_contribution` (src/points.py) is a pure
    # function of verified track distance, never of the battery delta, so
    # nothing here changes what a rider is credited — this only gates what
    # reaches `battery_trip_observations` for training.
    burn = soc_start - soc_end
    if burn <= -SWAP_JUMP_PCT or burn <= 0 or burn > MAX_BURN_PCT:
        return None

    distance_m = donation_row.get("distance_meters")
    if distance_m is None:
        return None

    departed_at = ride_row["track_key_issued_at"]
    arrived_at = ride_row["user_reported_ended_at"]
    vehicle_identifier = ride_row["vehicle_identifier"]

    cur.execute(
        "SELECT lat, lon FROM donated_track_points WHERE donation_id = %s ORDER BY seq ASC",
        (str(donation_row["id"]),),
    )
    points = [(float(r[0]), float(r[1])) for r in cur.fetchall()]
    if not points:
        return None
    from_lat, from_lon = points[0]
    to_lat, to_lon = points[-1]

    duration_seconds = (arrived_at - departed_at).total_seconds()
    elevation_gain = _donated_elevation_gain_meters(points)
    temperature_c = _temperature_at_cur(cur, departed_at)

    # DOUBLE-COUNT GUARD — see docstring. Must run before the INSERT and in
    # the same transaction: a crash between the two would either leave a
    # stale feed-mined duplicate (delete lost) or drop the donated row
    # (insert lost) — never a mix of both, since neither statement commits
    # on its own here.
    cur.execute(
        """
        DELETE FROM battery_trip_observations
        WHERE vehicle_identifier = %s
          AND departed_at >= %s AND departed_at <= %s
          AND source IS DISTINCT FROM 'donated_ride'
        """,
        (vehicle_identifier, departed_at, arrived_at),
    )

    cur.execute(
        """
        INSERT INTO battery_trip_observations (
            vehicle_identifier, vehicle_model_name, departed_at, arrived_at,
            duration_seconds, from_lat, from_lon, to_lat, to_lon,
            route_distance_meters, elevation_gain_meters, temperature_c,
            soc_start_percent, soc_end_percent, burn_percent, source
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (vehicle_identifier, departed_at) DO NOTHING
        RETURNING id
        """,
        (vehicle_identifier, donation_row.get("vehicle_model"),
         departed_at, arrived_at, duration_seconds,
         from_lat, from_lon, to_lat, to_lon,
         float(distance_m), elevation_gain, temperature_c,
         soc_start, soc_end, soc_start - soc_end, "donated_ride"),
    )
    row = cur.fetchone()
    if row is None:
        log.info("battery ingestion: no-op (already ingested) vehicle=%s departed_at=%s",
                 vehicle_identifier, departed_at)
        return None
    (new_id,) = row
    log.info("battery ingestion: donated observation id=%s vehicle=%s burn=%.1f",
              new_id, vehicle_identifier, soc_start - soc_end)
    return {"id": int(new_id), "source": "donated_ride",
            "burn_percent": soc_start - soc_end}
