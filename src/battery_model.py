"""Empirical battery-burn model: extraction, fit, and serving.

Replaces a static per-type estimate with a regression on observed trips:

    burn% = b0 + b1*distance_m + b2*elevation_gain_m + b3*temperature_C

Three deliberate departures from the obvious implementation, each forced by the
data (see sql/024_battery_model.sql for the long form):

* The target is state-of-charge PERCENT, recovered through the fleet-wide
  lookup table in data/range_soc_lut.json. ``current_range_meters`` is a
  nonlinear vendor re-encoding of an integer percent and is identical across all
  three vehicle models, so regressing on metres would fit the vendor curve.
* A trip is an OBSERVATION GAP in the telemetry stream, not a row in any of the
  trip tables. A rented vehicle drops out of GBFS free_bike_status, so a ride
  shows up as two consecutive observations 10-30 minutes apart with a position
  jump between them. Neither ``trip_events`` (no duration at all) nor
  ``device_history`` works: measured over 1.37M stops, device_history's
  ``departed_at`` equals the next stop's ``snapshot_time`` at p50, p90 and mean,
  because it records the detecting cycle rather than the departure.
* Distance and elevation are Valhalla's routed values;
  ``trip_events.distance_meters`` is a documented flat-earth approximation.

The fit is an ordinary least squares solve via ``numpy.linalg.lstsq`` — four
terms does not justify a scikit-learn dependency, in the spirit of
``src/polyline.py``.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import valhalla, weather
from .pg import connection
from .quality import compute_battery_percent

log = logging.getLogger(__name__)

# --- Anchor filter ----------------------------------------------------------
# Trips shorter than 10 min or longer than 30 min are dropped: the short end is
# dominated by the ~1-percentage-point SoC quantization floor, the long end by
# vehicles that were parked mid-"trip" or that we simply lost sight of.
MIN_DURATION_S = 10 * 60
MAX_DURATION_S = 30 * 60
# Routed distance floor. Below ~1 mile a typical trip burns less than one SoC
# step, so the observation carries no signal.
MIN_DISTANCE_METERS = 1609.34
# Implied average speed floor: below this the rider was meandering or the
# vehicle was moved by an operator van, neither of which is a rider trip.
MIN_IMPLIED_MPH = 8.0
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
# A trip is an OBSERVATION GAP, not a device_history row.
#
# GBFS free_bike_status lists only available vehicles, so a rented scooter drops
# out of the feed for the duration of the ride and reappears at the destination.
# Two consecutive observations of the same vehicle separated by 10-30 minutes,
# with a position jump across the gap, therefore bracket a trip — and the gap
# itself is the duration.
#
# device_history looked like the natural source (it has departed_at) but is not
# usable: measured over 1.37M stops, departed_at equals the NEXT stop's
# snapshot_time at p50, p90 AND mean, because it records the cycle that detected
# the move rather than the moment of departure. Zero stops fall in the 10-30
# minute band. The observation-gap model is what scripts/analyze_range_signal.py
# already used, and it is the one that survives contact with the data.
#
# Measured on a 2-day archive file: 11.7M consecutive pairs -> 17,401 with a
# 10-30 min gap -> 1,146 that also moved -> 243 usable after the distance and
# SoC filters. Roughly 120 usable observations per day.

# Flat-earth metres between the two ends of a pair. Postgres here has no
# PostGIS; this is the same approximation device_state.py already uses for its
# movement threshold, and it only gates which pairs are worth routing — the
# distance that reaches the regression is Valhalla's.
_STRAIGHT_LINE_M = """
        sqrt(
            pow((o.lat2 - o.latitude) * 111320.0, 2) +
            pow((o.lon2 - o.longitude) * 111320.0 * cos(radians(o.latitude)), 2)
        )
"""

# Cheap pre-filter before paying for a Valhalla call. Deliberately well below
# the 1-mile anchor: straight-line distance always understates the routed path,
# so filtering at 1 mile here would silently drop qualifying trips. The real
# 1-mile test is applied to the routed distance.
MIN_STRAIGHT_LINE_METERS = 800.0

_PAIRS_SQL = f"""
WITH obs AS (
    SELECT
        vehicle_identifier, vehicle_model_name, snapshot_time,
        latitude, longitude, current_range_meters,
        LEAD(snapshot_time)        OVER w AS t2,
        LEAD(latitude)             OVER w AS lat2,
        LEAD(longitude)            OVER w AS lon2,
        LEAD(current_range_meters) OVER w AS range2
    FROM raw_telemetry_points
    WHERE spatial_status = 'denver_core'
      -- A disabled vehicle that vanishes and reappears elsewhere was moved by
      -- an operator van, not ridden. Its battery drain is real but has a
      -- different cause, so it must not train a rider-facing model.
      AND NOT is_disabled
      AND snapshot_time >= %(window_start)s
      AND snapshot_time <  %(window_end)s
    WINDOW w AS (PARTITION BY vehicle_identifier ORDER BY snapshot_time)
)
SELECT
    o.vehicle_identifier,
    o.vehicle_model_name,
    o.snapshot_time AS departed_at,
    o.t2            AS arrived_at,
    EXTRACT(EPOCH FROM (o.t2 - o.snapshot_time)) AS duration_seconds,
    o.latitude, o.longitude, o.lat2, o.lon2,
    o.current_range_meters AS range_start,
    o.range2               AS range_end,
    {_STRAIGHT_LINE_M}     AS straight_line_m
FROM obs o
WHERE o.t2 IS NOT NULL
  AND o.range2 IS NOT NULL
  AND o.current_range_meters IS NOT NULL
  AND EXTRACT(EPOCH FROM (o.t2 - o.snapshot_time)) BETWEEN %(min_s)s AND %(max_s)s
  AND {_STRAIGHT_LINE_M} > %(min_straight_m)s
  AND NOT EXISTS (
      SELECT 1 FROM battery_trip_observations b
      WHERE b.vehicle_identifier = o.vehicle_identifier
        AND b.departed_at = o.snapshot_time
  )
ORDER BY o.snapshot_time DESC
LIMIT %(limit)s
"""


# How far from a trip a cached hourly reading may be and still be used. Without
# a bound, a hole in the cache silently hands a trip a temperature from days
# away and beta_3 absorbs the error as if it were signal.
MAX_TEMPERATURE_GAP_SECONDS = 2 * 3600


def _temperature_at(conn, when: datetime) -> float | None:
    with conn.cursor() as cur:
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
    try:
        body = valhalla.route(
            [(float(cand["latitude"]), float(cand["longitude"])),
             (float(cand["lat2"]), float(cand["lon2"]))],
            costing_options={"bicycle_type": "Hybrid"},
        )
    except valhalla.ValhallaError:
        stats["no_route"] += 1
        return False

    trips = valhalla.all_trips(body)
    if not trips:
        stats["no_route"] += 1
        return False

    summary = valhalla.trip_summary(trips[0])
    distance = summary["distance_meters"]
    if distance is None or distance < MIN_DISTANCE_METERS:
        stats["rejected_distance"] += 1
        return False

    mph = _implied_mph(distance, float(cand["duration_seconds"]))
    if mph < MIN_IMPLIED_MPH:
        stats["rejected_speed"] += 1
        return False

    temp = _temperature_at(conn, cand["departed_at"])

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO battery_trip_observations (
                vehicle_identifier, vehicle_model_name, departed_at, arrived_at,
                duration_seconds, from_lat, from_lon, to_lat, to_lon,
                route_distance_meters, elevation_gain_meters, temperature_c,
                soc_start_percent, soc_end_percent, burn_percent, implied_mph
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (vehicle_identifier, departed_at) DO NOTHING
            """,
            (cand["vehicle_identifier"], cand["vehicle_model_name"],
             cand["departed_at"], cand["arrived_at"], float(cand["duration_seconds"]),
             cand["latitude"], cand["longitude"], cand["lat2"], cand["lon2"],
             distance, summary["elevation_gain_meters"], temp,
             cand["soc_start"], cand["soc_end"], cand["burn"], mph),
        )
    return True


def extract_trips(hours: int = 26, limit: int = 2000) -> dict[str, Any]:
    """Mine the hot telemetry buffer for trips and persist the good ones.

    Runs against ``raw_telemetry_points``, which the archive job flushes to R2
    and truncates every 24 hours — hence the default 26-hour window, which
    overlaps the flush boundary so nothing is missed between runs. Re-runs are
    idempotent via the (vehicle_identifier, departed_at) unique constraint.

    The observations table accumulates: a single run yields ~120 trips, and the
    model becomes trainable after a couple of weeks of daily runs. See
    ``backfill_trips_from_archive`` to seed it from history instead of waiting.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=hours)

    # Temperature must cover the window before any row is written, or every
    # observation lands with a NULL the regression then has to discard.
    weather.ensure_coverage(window_start.date(), now.date())

    stats = {
        "candidates": 0, "accepted": 0, "zero_delta": 0,
        "rejected_soc": 0, "rejected_distance": 0, "rejected_speed": 0,
        "rejected_swap": 0, "rejected_rebalance": 0, "no_route": 0,
    }

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_PAIRS_SQL, {
                "window_start": window_start,
                "window_end": now,
                "min_s": MIN_DURATION_S,
                "max_s": MAX_DURATION_S,
                "min_straight_m": MIN_STRAIGHT_LINE_METERS,
                "limit": limit,
            })
            cols = [d[0] for d in cur.description]
            candidates = [dict(zip(cols, row)) for row in cur.fetchall()]

        stats["candidates"] = len(candidates)
        rebalanced = _rebalance_keys(candidates)
        log.info("battery extraction: %d candidate pairs in the last %dh "
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
# MEMORY: measured at 1,243 MiB peak RSS for a single 11.9M-row archive file.
# The scheduler container is capped at 1024m, so this WILL be OOM-killed
# (exit 137) at the default limit. Raise it for the duration:
#
#     docker update --memory 2g --memory-swap 2g scheduler
#     docker compose exec scheduler python -m src.cli backfill_battery_trips
#     docker update --memory 1024m --memory-swap 1024m scheduler
#
# Files are processed one at a time rather than through a single glob: the
# whole archive is ~167M rows, and joining across all of it at once exhausts
# any plausible limit.

ARCHIVE_DUCKDB_MEMORY = "1GB"


def _archive_keys(client, bucket: str) -> list[str]:
    keys = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="raw/"):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                keys.append(obj["Key"])
    return sorted(keys)


def _pairs_from_archive_file(con, url: str) -> list[dict]:
    """Same observation-gap model as _PAIRS_SQL, over one archive file.

    Timestamps are returned as text: DuckDB needs pytz to hand a TIMESTAMPTZ
    back to Python and the worker image does not ship it.
    """
    sql = f"""
    WITH obs AS (
        SELECT vehicle_identifier, vehicle_model_name, snapshot_time,
               latitude, longitude, current_range_meters,
               LEAD(snapshot_time)        OVER w AS t2,
               LEAD(latitude)             OVER w AS lat2,
               LEAD(longitude)            OVER w AS lon2,
               LEAD(current_range_meters) OVER w AS range2
        FROM read_parquet('{url}')
        WHERE spatial_status = 'denver_core' AND NOT is_disabled
        WINDOW w AS (PARTITION BY vehicle_identifier ORDER BY snapshot_time)
    )
    SELECT vehicle_identifier, vehicle_model_name,
           snapshot_time::VARCHAR AS departed_at,
           t2::VARCHAR            AS arrived_at,
           date_diff('second', snapshot_time, t2) AS duration_seconds,
           latitude, longitude, lat2, lon2,
           current_range_meters AS range_start, range2 AS range_end
    FROM obs
    WHERE t2 IS NOT NULL AND range2 IS NOT NULL AND current_range_meters IS NOT NULL
      AND date_diff('second', snapshot_time, t2) BETWEEN {MIN_DURATION_S} AND {MAX_DURATION_S}
      AND sqrt(pow((lat2 - latitude) * 111320.0, 2) +
               pow((lon2 - longitude) * 111320.0 * cos(radians(latitude)), 2))
          > {MIN_STRAIGHT_LINE_METERS}
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
        "rejected_swap": 0, "rejected_rebalance": 0, "no_route": 0,
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
                url = f"s3://{creds['bucket']}/{key}"
                log.info("backfill: %s", key)
                candidates = _pairs_from_archive_file(con, url)
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
                log.info("backfill: %s done -> %r", key, stats)
    finally:
        con.close()

    log.info("backfill complete: %r", stats)
    return stats


# --- Fit ---------------------------------------------------------------------

def train(days: int = 60, holdout_days: int = 3) -> dict[str, Any]:
    """Fit the model and append the coefficients. Returns the fit summary."""
    import numpy as np  # available via pyarrow; imported lazily to keep API boot light

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
