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

Both handlers are per-IP rate limited. `ratelimit.enforce` needs an open cursor
and neither handler otherwise touches Postgres, so the limit opens the one short
connection it needs (`_enforce_ip_limit`) from a route DEPENDENCY rather than the
handler body: the guard then cannot be forgotten on any HTTP path, runs before
any Valhalla work, and leaves both handlers callable in-process (they take no
`Request`, which is how the profile/coverage unit tests drive them).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from . import battery_model, valhalla
from .client_ip import real_client_ip
from .config import RouteProfile, load
from .pg import connection
from .r2_map import load_canopy_coverage
from .ratelimit import enforce

log = logging.getLogger(__name__)

router = APIRouter()

# Rider-facing beta disclaimer, attached to every /route and /route/profiles
# response. Turn-by-turn quality is not where it needs to be yet, and a rider
# following a bad cue on the street pays for it in the real world — clients
# must surface this text (or an equivalent warning) wherever directions are
# shown, and its presence in the payload is what lets them do that without a
# hardcoded string that outlives the beta.
NAV_BETA_WARNING = (
    "Navigation directions are in beta and may be inaccurate or unsafe. "
    "Use your own judgment, watch the road, and obey posted signs, signals, "
    "and traffic laws."
)

# Per-IP rate limits (API_REQUIREMENTS.md §5), as (limit, window_seconds).
# 30/min on /route accommodates Screen 4's four parallel profile fetches plus
# the <=1/min off-route re-route; /route/profiles is a config-only response and
# gets the looser cap.
_LIMIT_ROUTE_PER_IP = (30, 60)
_LIMIT_ROUTE_PROFILES_PER_IP = (60, 60)

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


def _enforce_ip_limit(request: Request, *, bucket: str, limit: tuple[int, int]) -> None:
    """Count this request against `bucket` for the caller's IP, or raise 429.

    `enforce` wants an open cursor inside the caller's transaction, and routing
    is otherwise DB-free, so this opens the only connection either handler
    needs. Keyed on `real_client_ip(request)`: behind the cloudflared sidecar
    `request.client.host` is the loopback address of the tunnel, so every
    caller would share one bucket (`src/client_ip.py`).

    A 429 propagates out with `Retry-After` from `ratelimit.enforce`, and no
    commit happens — the same allow-and-record semantics every other bucket has.
    """
    ip = real_client_ip(request) or "?"
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket=bucket, key=ip,
                    limit=limit[0], window_seconds=limit[1])
        conn.commit()


def _limit_route_ip(request: Request) -> None:
    """Route dependency: 30/min per IP on /route."""
    _enforce_ip_limit(request, bucket="route_ip", limit=_LIMIT_ROUTE_PER_IP)


def _limit_route_profiles_ip(request: Request) -> None:
    """Route dependency: 60/min per IP on /route/profiles."""
    _enforce_ip_limit(request, bucket="route_profiles_ip",
                      limit=_LIMIT_ROUTE_PROFILES_PER_IP)


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


# Valhalla `edge.use` values that mean "a street a car also drives on", i.e.
# somewhere with traffic, buildings and — the point at night — street lighting.
# Everything else (cycleway, footway, path, track) is off-street: pleasant by
# day, and the thing a rider asked to avoid after dark.
#
# `driveway` and `parking_aisle` are deliberately NOT here. They are motor-
# vehicle surfaces but they are not through-streets, and counting them would
# score a car park as well-lit road.
_STREET_USES = frozenset({
    "road", "ramp", "turn_channel", "living_street", "alley",
})


def street_share(trip: dict[str, Any], costing_options: dict[str, Any],
                 shape: list[tuple[float, float]] | None = None) -> float | None:
    """Fraction of a trip's length ridden on streets rather than off-street path.

    THIS IS A PROXY FOR LIGHTING, and an explicit one. The honest signal would
    be OSM's `lit=*`, but its coverage across the Denver clip is 3.2% of ways
    overall and 4.2% on cycleways/trails — far too sparse to rank on; a route
    scored against it would mostly be comparing unknowns. Street share is what
    the available data supports: a lit corridor is overwhelmingly a street, and
    the unlit stretch a rider wants to avoid at night is overwhelmingly an
    isolated trail. Swap this for a real lighting join the day the data exists
    (see `_canopy` for the shape that takes).

    Returns None when the trip can't be snapped back onto the graph. Callers
    treat that as "unknown" rather than "no streets", so a failed trace never
    silently reorders routes — the same rule shade_score follows.
    """
    if shape is None:
        shape = valhalla.trip_shape(trip)
    if len(shape) < 2:
        return None
    try:
        edges = valhalla.trace_attributes(
            shape, costing_options, attributes=("edge.length", "edge.use"))
    except valhalla.ValhallaError as exc:
        log.warning("night scoring failed to trace route: %s", exc)
        return None

    total = 0.0
    on_street = 0.0
    for edge in edges:
        length = edge.get("length") or 0.0
        if length <= 0:
            continue
        total += length
        if (edge.get("use") or "").lower() in _STREET_USES:
            on_street += length
    if total <= 0:
        return None
    return round(on_street / total, 4)


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


@router.get("/api/v1/route", dependencies=[Depends(_limit_route_ip)])
def route(
    from_: str = Query(..., alias="from", description="Origin as 'lat,lon'"),
    to: str = Query(..., description="Destination as 'lat,lon'"),
    profile: str | None = Query(None, description="safe | range | shade | express"),
    vehicle_model: str | None = Query(
        None, description="Optional vehicle model (Astro/Cosmo/Apollo/Rover) "
                          "for a model-specific battery estimate; models "
                          "without a fitted curve fall back to the fleet-wide "
                          "estimate"),
    explain: bool = Query(False, description="Include diagnostics (shade score on every profile)"),
    # Annotated form deliberately: with `maneuvers: bool = Query(False)` the
    # default value is the Query MARKER object, which is truthy, so any
    # in-process caller of this function would get the passthrough enabled
    # (and pay for decoding every leg) without asking for it.
    maneuvers: Annotated[bool, Query(
        description="Include turn-by-turn maneuvers for the nav HUD")] = False,
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
    night_share = None
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
    elif prof.rerank_by_street_share:
        # Same shape as shade above, and for the same reason: Valhalla has no
        # request-tunable "keep me on lit streets" lever, so the choice is made
        # on the response. See street_share for why street share stands in for
        # lighting.
        baseline = cfg.profile(cfg.default_profile)
        if baseline is not None and baseline.key != prof.key:
            # A rider asking for the night profile must never end up on MORE
            # off-street path than the default would have given them. Same
            # guard shade and range carry.
            try:
                trips += valhalla.all_trips(
                    _route_with_retry([origin, dest], baseline))
            except valhalla.ValhallaError as exc:
                log.warning("night baseline route failed, ranking alternates only: %s", exc)
        considered = len(trips)
        shapes = [valhalla.trip_shape(t) for t in trips]
        rated = [(street_share(t, prof.costing_options, sh), t)
                 for t, sh in zip(trips, shapes)]
        measured = [(sc, t) for sc, t in rated if sc is not None]
        if measured:
            night_share, chosen = max(measured, key=lambda pair: pair[0])
        if explain:
            chosen_shape = valhalla.trip_shape(chosen)
            score = shade_score(chosen, prof.costing_options, chosen_shape)
    elif prof.rerank_by_elevation:
        # Pick the flattest alternate, for the same reason shade is re-ranked
        # above: Valhalla's own lever does not work here. `use_hills` is INERT
        # on this graph -- swept 0.0 to 1.0 on five Denver pairs (up to 77 m of
        # climb) it returns a byte-identical shape every time, while `use_roads`
        # and `bicycle_type` change the route in the very same request. The
        # graph does carry grades (23 of 52 edges on the reported pair are
        # non-zero), so this is not missing data; the knob simply does not move
        # the cost enough to reorder anything.
        #
        # Reported case, 3158 W 8th Ave -> Knox Station: the primary route
        # climbed 31.9 m while the third alternate climbed 14.2 m over a route
        # 2 m SHORTER. There was no tradeoff to make -- the flat line was right
        # there, unranked.
        #
        # Free, unlike shade: elevation gain comes out of the route response
        # already (`elevation_interval` is requested), so nothing extra is
        # fetched and no thread pool is needed.
        baseline = cfg.profile(cfg.default_profile)
        if baseline is not None and baseline.key != prof.key:
            # Same guard as shade's: this profile's costing generates its own
            # route family, so ranking only within it can hand the rider MORE
            # climb than the default would have. Whoever picks "Range
            # Maximizer" must never do worse on climb than doing nothing.
            try:
                trips += valhalla.all_trips(
                    _route_with_retry([origin, dest], baseline))
            except valhalla.ValhallaError as exc:
                log.warning("elevation baseline route failed, ranking alternates only: %s", exc)
        considered = len(trips)
        rated = [(valhalla.elevation_gain_meters(t), t) for t in trips]
        # A trip whose gain is unknown keeps Valhalla's ranking rather than
        # winning by default -- None is not flat, it is unmeasured.
        measured = [(g, t) for g, t in rated if g is not None]
        if measured:
            _, chosen = min(measured, key=lambda pair: pair[0])
        if explain:
            chosen_shape = valhalla.trip_shape(chosen)
            score = shade_score(chosen, prof.costing_options, chosen_shape)
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
        "street_share": night_share,
        "battery_percent_estimate": battery.get("percent"),
        # A band, not just a point. Held-out error is ~5.7 pp; a bare number
        # reads as a promise the model cannot keep.
        "battery_percent_low": battery.get("percent_low"),
        "battery_percent_high": battery.get("percent_high"),
        # The climb's share of the cost, so a client can say "the hill is a
        # third of this" instead of just quoting a total.
        "battery_from_elevation_percent": battery.get("from_elevation_percent"),
        "battery_from_elevation_share": battery.get("from_elevation_share"),
        "battery_model": battery.get("source"),
        "graph_bbox": cfg.bbox,
        "beta_warning": NAV_BETA_WARNING,
    }
    if maneuvers:
        # Opt-in: the nav HUD needs them, the route preview on Screen 4 does not,
        # and they roughly double the response size. Shape indices address the
        # `geometry` LineString below, not the per-leg shapes Valhalla numbers.
        properties["maneuvers"] = valhalla.trip_maneuvers(chosen)
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


@router.get("/api/v1/route/profiles",
            dependencies=[Depends(_limit_route_profiles_ip)])
def profiles() -> dict[str, Any]:
    """Advertise the selectable profiles so the client needn't hardcode them."""
    cfg = load().valhalla
    return {
        "default": cfg.default_profile,
        "graph_bbox": cfg.bbox,
        "beta_warning": NAV_BETA_WARNING,
        "profiles": [
            {"key": p.key, "label": p.label, "shade_ranked": p.rerank_by_shade,
             "elevation_ranked": p.rerank_by_elevation,
             "street_ranked": p.rerank_by_street_share}
            for p in cfg.profiles
        ],
    }
