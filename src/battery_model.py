"""Empirical battery-burn model: extraction, fit, and serving.

Replaces a static per-type estimate with a regression on observed trips:

    burn% = b0 + b1*distance_m + b2*elevation_gain_m + b3*temperature_C

Three deliberate departures from the obvious implementation, each forced by the
data (see sql/024_battery_model.sql for the long form):

* The target is state-of-charge PERCENT, recovered through the fleet-wide
  lookup table in data/range_soc_lut.json. ``current_range_meters`` is a
  nonlinear vendor re-encoding of an integer percent and is identical across all
  three vehicle models, so regressing on metres would fit the vendor curve.
* Trips come from ``device_history`` (which has ``departed_at``, hence a real
  duration) rather than ``trip_events`` (which has only ``detected_at``).
* Distance and elevation are Valhalla's routed values;
  ``trip_events.distance_meters`` is a documented flat-earth approximation.

The fit is an ordinary least squares solve via ``numpy.linalg.lstsq`` — four
terms does not justify a scikit-learn dependency, in the spirit of
``src/polyline.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
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

_CANDIDATE_SQL = """
WITH stops AS (
    SELECT
        h.vehicle_identifier,
        h.snapshot_time                                        AS arrived_at,
        h.departed_at,
        h.lat, h.lon,
        LEAD(h.snapshot_time) OVER w                           AS next_arrived_at,
        LEAD(h.lat)           OVER w                           AS next_lat,
        LEAD(h.lon)           OVER w                           AS next_lon
    FROM device_history h
    WHERE h.departed_at IS NOT NULL
      AND h.snapshot_time >= %(window_start)s
      AND h.snapshot_time <  %(window_end)s
    WINDOW w AS (PARTITION BY h.vehicle_identifier ORDER BY h.snapshot_time)
)
SELECT
    s.vehicle_identifier,
    s.departed_at,
    s.next_arrived_at        AS arrived_at,
    s.lat                    AS from_lat,
    s.lon                    AS from_lon,
    s.next_lat               AS to_lat,
    s.next_lon               AS to_lon,
    EXTRACT(EPOCH FROM (s.next_arrived_at - s.departed_at)) AS duration_seconds
FROM stops s
WHERE s.next_arrived_at IS NOT NULL
  AND s.next_lat IS NOT NULL
  AND EXTRACT(EPOCH FROM (s.next_arrived_at - s.departed_at))
      BETWEEN %(min_s)s AND %(max_s)s
  AND NOT EXISTS (
      SELECT 1 FROM battery_trip_observations o
      WHERE o.vehicle_identifier = s.vehicle_identifier
        AND o.departed_at = s.departed_at
  )
ORDER BY s.departed_at DESC
LIMIT %(limit)s
"""

# SoC at each end of the trip: the last reading before departure and the first
# after arrival, from the 48-hour hot buffer.
_SOC_SQL = """
SELECT
    (SELECT r.current_range_meters
       FROM raw_telemetry_points r
      WHERE r.vehicle_identifier = %(vid)s
        AND r.snapshot_time <= %(departed_at)s
      ORDER BY r.snapshot_time DESC LIMIT 1) AS range_start,
    (SELECT r.current_range_meters
       FROM raw_telemetry_points r
      WHERE r.vehicle_identifier = %(vid)s
        AND r.snapshot_time >= %(arrived_at)s
      ORDER BY r.snapshot_time ASC LIMIT 1) AS range_end,
    (SELECT r.vehicle_model_name
       FROM raw_telemetry_points r
      WHERE r.vehicle_identifier = %(vid)s
      ORDER BY r.snapshot_time DESC LIMIT 1) AS model
"""


def _temperature_at(conn, when: datetime) -> float | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT temperature_c FROM hourly_temperature
            ORDER BY ABS(EXTRACT(EPOCH FROM (observed_hour - %s)))
            LIMIT 1
            """,
            (when,),
        )
        row = cur.fetchone()
    return float(row[0]) if row else None


def extract_trips(days: int = 30, limit: int = 5000) -> dict[str, Any]:
    """Find candidate trips, route them through Valhalla, and persist observations.

    Only trips older than the ERA5 publication lag are considered, so every
    stored row has a real temperature rather than a silently defaulted one.
    """
    now = datetime.now(timezone.utc)
    window_end = now - timedelta(days=weather.ARCHIVE_LAG_DAYS)
    window_start = window_end - timedelta(days=days)

    weather.ensure_coverage(window_start.date(), window_end.date())

    stats = {
        "candidates": 0, "accepted": 0, "zero_delta": 0,
        "rejected_soc": 0, "rejected_distance": 0, "rejected_speed": 0,
        "rejected_swap": 0, "no_route": 0,
    }

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_CANDIDATE_SQL, {
                "window_start": window_start,
                "window_end": window_end,
                "min_s": MIN_DURATION_S,
                "max_s": MAX_DURATION_S,
                "limit": limit,
            })
            candidates = cur.fetchall()
        stats["candidates"] = len(candidates)
        log.info("battery extraction: %d candidate trips in %s..%s",
                 len(candidates), window_start.date(), window_end.date())

        for (vid, departed_at, arrived_at, from_lat, from_lon,
             to_lat, to_lon, duration_s) in candidates:
            with conn.cursor() as cur:
                cur.execute(_SOC_SQL, {
                    "vid": vid, "departed_at": departed_at, "arrived_at": arrived_at})
                soc_row = cur.fetchone()
            if not soc_row or soc_row[0] is None or soc_row[1] is None:
                stats["rejected_soc"] += 1
                continue

            soc_start = compute_battery_percent(soc_row[0])
            soc_end = compute_battery_percent(soc_row[1])
            model = soc_row[2]
            if soc_start is None or soc_end is None:
                stats["rejected_soc"] += 1
                continue

            burn = soc_start - soc_end
            if burn <= -SWAP_JUMP_PCT:
                stats["rejected_swap"] += 1
                continue
            if burn == 0:
                # Counted but not stored: a zero here is quantization, and
                # keeping them would bias the intercept toward zero burn.
                stats["zero_delta"] += 1
                continue
            if burn < 0 or burn > MAX_BURN_PCT:
                stats["rejected_soc"] += 1
                continue

            try:
                body = valhalla.route(
                    [(float(from_lat), float(from_lon)), (float(to_lat), float(to_lon))],
                    costing_options={"bicycle_type": "Hybrid"},
                )
            except valhalla.ValhallaError:
                stats["no_route"] += 1
                continue
            trips = valhalla.all_trips(body)
            if not trips:
                stats["no_route"] += 1
                continue

            summary = valhalla.trip_summary(trips[0])
            distance = summary["distance_meters"]
            if distance is None or distance < MIN_DISTANCE_METERS:
                stats["rejected_distance"] += 1
                continue

            mph = _implied_mph(distance, float(duration_s))
            if mph < MIN_IMPLIED_MPH:
                stats["rejected_speed"] += 1
                continue

            temp = _temperature_at(conn, departed_at)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO battery_trip_observations (
                        vehicle_identifier, vehicle_model_name, departed_at, arrived_at,
                        duration_seconds, from_lat, from_lon, to_lat, to_lon,
                        route_distance_meters, elevation_gain_meters, temperature_c,
                        soc_start_percent, soc_end_percent, burn_percent, implied_mph
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    ) ON CONFLICT (vehicle_identifier, departed_at) DO NOTHING
                    """,
                    (vid, model, departed_at, arrived_at, float(duration_s),
                     from_lat, from_lon, to_lat, to_lon,
                     distance, summary["elevation_gain_meters"], temp,
                     soc_start, soc_end, burn, mph),
                )
            stats["accepted"] += 1

        # The zero-delta share is the headline health metric for this pipeline:
        # the SoC grid is ~1 percentage point, so a high share means trips are
        # burning less than one step and the fit would be mostly quantization
        # noise. Zeros are never stored as observations, so record the ratio
        # here or it is unrecoverable at fit time.
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
                       temperature_c, burn_percent, departed_at
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

    def design(subset):
        X = np.array([[1.0, float(r[0]), float(r[1]), float(r[2])] for r in subset])
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
                    zero_delta_fraction, notes
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (window_start, now, len(train_rows),
                 float(beta[0]), float(beta[1]), float(beta[2]), float(beta[3]),
                 r2, residual_std, mean_temp, zero_delta_fraction, notes),
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
                           n_observations, fitted_at
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
    }
    return _MODEL_CACHE


def estimate_burn_percent(distance_meters: float | None,
                          elevation_gain_meters: float | None) -> dict[str, Any]:
    """Predicted battery burn for a route, as a percentage of full charge.

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
    percent = (model["intercept"]
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
