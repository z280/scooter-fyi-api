"""Rider-facing bicycle routing: GET /api/v1/route.

Maps a rider-selected profile onto a Valhalla bicycle costing payload and
returns a GeoJSON Feature. All four profiles are free and selectable by anyone —
nothing in this product is paywalled (sql/036_decommercialize.sql), so there is
deliberately no entitlement check here.

Shade is the one profile Valhalla cannot express directly. Its bike-network
discount is a hardcoded 0.95 factor applied to every request, and there is no
`use_trails` option for bicycles. So the `shade` profile asks for alternates and
re-ranks them against the tree-canopy coverage denver-map-prep publishes
alongside the routing graph.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from . import battery_model, valhalla
from .config import RouteProfile, load
from .r2_map import load_canopy_coverage

log = logging.getLogger(__name__)

router = APIRouter()

# way_id -> canopy coverage fraction, loaded lazily from the shared volume.
_CANOPY: dict[int, float] | None = None
_CANOPY_LOADED_AT: float = 0.0
# Only a SUCCESSFUL load is cached indefinitely; a miss is retried on this
# interval. pipeline_worker deliberately does not depend on valhalla_map_fetch —
# the audit API must boot whether or not the routing assets exist — so on a cold
# `docker compose up` the worker can reach /api/v1/route before the sidecar has
# finished downloading. Caching that empty result forever would disable shade
# re-ranking for the life of the process, silently, with routes still 200-ing.
_CANOPY_RETRY_SECONDS = 60.0


def _canopy() -> dict[int, float]:
    global _CANOPY, _CANOPY_LOADED_AT
    import time

    if _CANOPY:
        return _CANOPY
    if _CANOPY is not None and (time.monotonic() - _CANOPY_LOADED_AT) < _CANOPY_RETRY_SECONDS:
        return _CANOPY
    _CANOPY = load_canopy_coverage()
    _CANOPY_LOADED_AT = time.monotonic()
    return _CANOPY


def _parse_point(raw: str, field: str) -> tuple[float, float]:
    parts = raw.split(",")
    if len(parts) != 2:
        raise HTTPException(400, {"error": "bad_coordinate",
                                  "detail": f"{field} must be 'lat,lon'"})
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        raise HTTPException(400, {"error": "bad_coordinate",
                                  "detail": f"{field} must be 'lat,lon'"}) from None
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise HTTPException(400, {"error": "bad_coordinate",
                                  "detail": f"{field} is not a valid lat/lon"})
    return lat, lon


def shade_score(trip: dict[str, Any], costing_options: dict[str, Any],
                shape: list[tuple[float, float]] | None = None) -> float | None:
    """Length-weighted mean canopy coverage over the edges a trip traverses.

    ``shape`` may be passed in by a caller that has already decoded it, to avoid
    decoding the same polyline twice.

    Returns None when the coverage table is unavailable or the trip's shape
    can't be snapped back onto the graph — callers treat that as "unknown"
    rather than "unshaded", so a missing sidecar never silently reorders routes.
    """
    coverage = _canopy()
    if not coverage:
        return None
    if shape is None:
        shape = valhalla.trip_shape(trip)
    if len(shape) < 2:
        return None
    try:
        edges = valhalla.trace_attributes(shape, costing_options)
    except valhalla.ValhallaError as exc:
        log.warning("shade scoring failed to trace route: %s", exc)
        return None

    total_len = 0.0
    weighted = 0.0
    unmeasured_len = 0.0
    for edge in edges:
        length = edge.get("length") or 0.0
        if length <= 0:
            continue
        total_len += length
        way_id = edge.get("way_id")
        if way_id in coverage:
            weighted += length * coverage[way_id]
        else:
            # Not measured by denver-map-prep (motorways, service roads, steps).
            # Excluded from BOTH sides of the ratio rather than scored 0: an
            # unmeasured way is unknown, not treeless, and scoring it 0 would
            # penalise whole route classes for a gap in the input data.
            unmeasured_len += length
    measured_len = total_len - unmeasured_len
    if measured_len <= 0:
        return None
    return round(weighted / measured_len, 4)


def _score_alternates(trips: list[dict[str, Any]],
                      costing_options: dict[str, Any]) -> list[tuple[float | None, dict]]:
    """Score every alternate concurrently.

    Serially this was one /route plus N /trace_attributes calls, each with its
    own timeout — a worst case of (N+1) x timeout before the client saw
    anything. httpx is synchronous here, so a small thread pool is the cheapest
    way to overlap them; N is 3.
    """
    shapes = [valhalla.trip_shape(t) for t in trips]
    if len(trips) == 1:
        return [(shade_score(trips[0], costing_options, shapes[0]), trips[0])]

    with ThreadPoolExecutor(max_workers=min(len(trips), 4)) as pool:
        futures = {
            pool.submit(shade_score, trip, costing_options, shape): (trip, shape)
            for trip, shape in zip(trips, shapes)
        }
        scored: list[tuple[float | None, dict]] = []
        for fut in as_completed(futures):
            trip, _ = futures[fut]
            try:
                scored.append((fut.result(), trip))
            except Exception as exc:  # noqa: BLE001 — one bad alternate must not fail the request
                log.warning("shade scoring raised for an alternate: %s", exc)
                scored.append((None, trip))
    return scored


def _route_with_retry(points, profile: RouteProfile) -> dict[str, Any]:
    """Route, retrying once with a wider search radius on a snapping failure.

    denver-map-prep tags High Injury Network ways `bicycle=no`, so a location
    fronting an arterial can legitimately have no routable edge within
    Valhalla's default radius. One widened retry usually finds the side street.
    """
    cfg = load().valhalla
    try:
        return valhalla.route(points, profile.costing_options,
                              alternates=profile.alternates)
    except valhalla.ValhallaError as exc:
        if not exc.no_suitable_edges:
            raise
        log.info("no suitable edges at default radius; retrying at %dm",
                 cfg.retry_radius_meters)
        return valhalla.route(points, profile.costing_options,
                              alternates=profile.alternates,
                              radius=cfg.retry_radius_meters)


@router.get("/api/v1/route")
def route(
    from_: str = Query(..., alias="from", description="Origin as 'lat,lon'"),
    to: str = Query(..., description="Destination as 'lat,lon'"),
    profile: str | None = Query(None, description="safe | range | shade | express"),
    vehicle_model: str | None = Query(
        None, description="Optional vehicle model (Astro/Cosmo/Apollo) for a "
                          "model-specific battery estimate"),
    explain: bool = Query(False, description="Include diagnostics (shade score on every profile)"),
) -> dict[str, Any]:
    cfg = load().valhalla

    key = profile or cfg.default_profile
    prof = cfg.profile(key)
    if prof is None:
        raise HTTPException(400, {
            "error": "unknown_profile",
            "detail": f"unknown profile {key!r}",
            "profiles": [p.key for p in cfg.profiles],
        })

    origin = _parse_point(from_, "from")
    dest = _parse_point(to, "to")

    # The routing graph is a Denver clip, narrower than both the app's map
    # bounds and the audit's denver_core envelope. Reject up front with the
    # served bbox rather than clamping — a silently relocated origin would
    # produce a confidently wrong distance and battery estimate.
    for label, (lat, lon) in (("from", origin), ("to", dest)):
        if not cfg.contains(lat, lon):
            raise HTTPException(400, {
                "error": "out_of_coverage",
                "detail": f"{label} ({lat}, {lon}) is outside the routing graph",
                "graph_bbox": cfg.bbox,
            })

    try:
        body = _route_with_retry([origin, dest], prof)
    except valhalla.ValhallaError as exc:
        if exc.no_suitable_edges:
            raise HTTPException(422, {
                "error": "no_route_from_location",
                "detail": "No cycling-permitted road near one of the locations. "
                          "High Injury Network streets are excluded from the graph.",
            }) from exc
        if exc.no_path:
            raise HTTPException(422, {
                "error": "no_route",
                "detail": "No cycling route exists between these locations.",
            }) from exc
        log.error("valhalla request failed: %s", exc)
        raise HTTPException(503, {"error": "router_unavailable"}) from exc

    trips = valhalla.all_trips(body)
    if not trips:
        raise HTTPException(422, {"error": "no_route"})

    chosen = trips[0]
    score = None
    considered = len(trips)

    chosen_shape = None
    if prof.rerank_by_shade:
        # Include the DEFAULT profile's route as a candidate. Shade's own
        # costing (use_roads 0.2) generates a different route family from the
        # default (0.1), so re-ranking only within it can return LESS canopy
        # than the rider would have got without asking for shade at all —
        # measured at -0.0026 on a Platte-corridor pair. A rider who selects
        # "Shaded Canopy" must never do worse than the default on shade.
        baseline = cfg.profile(cfg.default_profile)
        if baseline is not None and baseline.key != prof.key:
            try:
                trips += valhalla.all_trips(
                    _route_with_retry([origin, dest], baseline))
            except valhalla.ValhallaError as exc:
                log.warning("shade baseline route failed, scoring alternates only: %s", exc)
        considered = len(trips)
        scored = _score_alternates(trips, prof.costing_options)
        # Trips whose score is unknown keep Valhalla's own ranking; a None must
        # never beat a real measurement.
        rated = [(sc, t) for sc, t in scored if sc is not None]
        if rated:
            score, chosen = max(rated, key=lambda pair: pair[0])
        else:
            score = None
    elif explain:
        # Neutrality diagnostic: score the non-shade profiles too, so the shade
        # bias of the graph itself can be measured.
        chosen_shape = valhalla.trip_shape(chosen)
        score = shade_score(chosen, prof.costing_options, chosen_shape)

    summary = valhalla.trip_summary(chosen)
    battery = battery_model.estimate_burn_percent(
        distance_meters=summary["distance_meters"],
        elevation_gain_meters=summary["elevation_gain_meters"],
        vehicle_model=vehicle_model,
    )

    properties: dict[str, Any] = {
        "profile": prof.key,
        "label": prof.label,
        **summary,
        "shade_score": score,
        "battery_percent_estimate": battery.get("percent"),
        "battery_model": battery.get("source"),
        "graph_bbox": cfg.bbox,
    }
    if explain:
        properties["diagnostics"] = {
            "alternates_considered": considered,
            "costing_options": prof.costing_options,
            "canopy_ways_loaded": len(_canopy()),
            "battery_detail": battery,
        }

    if chosen_shape is None:
        chosen_shape = valhalla.trip_shape(chosen)
    return {
        "type": "Feature",
        "geometry": valhalla.to_geojson(chosen_shape),
        "properties": properties,
    }


@router.get("/api/v1/route/profiles")
def profiles() -> dict[str, Any]:
    """Advertise the selectable profiles so the client needn't hardcode them."""
    cfg = load().valhalla
    return {
        "default": cfg.default_profile,
        "graph_bbox": cfg.bbox,
        "profiles": [
            {"key": p.key, "label": p.label, "shade_ranked": p.rerank_by_shade}
            for p in cfg.profiles
        ],
    }
