"""Rider-facing bicycle routing: GET /api/v1/route.

Maps a rider-selected profile onto a Valhalla bicycle costing payload and
returns a GeoJSON Feature. All four profiles are free and selectable by anyone —
nothing in this product is paywalled (sql/036_decommercialize.sql), so there is
deliberately no entitlement check here.

FOUR OF THE FIVE PROFILES ARE RANKED ON THE RESPONSE, NOT IN THE GRAPH, because
Valhalla has no request-tunable lever for what they are about. `shade` has no
canopy input at all and re-ranks alternates against the tree-coverage table
denver-map-prep publishes alongside the graph. `night` has no lighting input.
`range` has `use_hills`, but it is inert on this graph — swept 0.0 to 1.0 it
returns byte-identical shapes. And `safe` has a bike-network preference that
exists and does nothing: a hardcoded 0.95 factor, no option to raise it, not
enough weight to reorder anything. `express` is the only profile that takes
Valhalla's first answer as given.

`safe` goes one step further than the others. Re-ranking can only choose among
the routes Valhalla offers, and on the reported pair it offers two, neither of
them the bikeway — so `_bikeway_detour_candidate` manufactures the missing one
by excluding the primary route's worst off-network stretch.

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


#: What a metre of designated bikeway costs, as a fraction of an ordinary metre.
#:
#: This is the whole preference, and it is deliberately ONE number rather than a
#: score plus a separate detour cap: at 0.85 a fully-bikeway route can be at most
#: 1/0.85 = 18% longer than a rival with none, because that is exactly where the
#: discount stops paying for itself. A second cap would be a knob that could
#: disagree with this one.
#:
#: Calibrated, not chosen. Swept over 104 Denver route sets (26 real ride
#: origin/destination pairs from `tracked_rides` plus 90 seeded random pairs at
#: 0.6-4 km, the range scooter trips actually occupy), scoring every candidate
#: including the manufactured one from `_bikeway_detour_candidate`:
#:
#:     discount   bikeway share   mean distance   worst detour
#:       1.00     40.0% -> 36.6%      -0.9%           0.0%
#:       0.90     40.0% -> 44.3%      -0.5%           5.9%
#:       0.85     40.0% -> 45.8%      -0.3%           5.9%   <- here
#:       0.80     40.0% -> 46.5%      -0.1%          13.0%
#:       0.75     40.0% -> 47.5%      +0.1%          19.9%
#:
#: 0.85 takes most of the available gain while the worst case a rider can be
#: handed stays under 6%, which nobody notices; 0.80 buys 0.7 of a point more
#: share and doubles that worst case. Note the top row: ranking these same
#: candidates by distance alone LOWERS bikeway share to 36.6%, so the shortest
#: route is actively anti-correlated with the bike network — the preference is
#: not free-riding on a coincidence.
#:
#: Mean distance is NEGATIVE at every setting: re-ranking picks up shorter
#: alternates as often as longer ones, because Valhalla's first choice is not
#: the shortest either. In aggregate this costs riders nothing.
BIKEWAY_METER_DISCOUNT = 0.85

# Valhalla `edge.cycle_lane` values that mean "there is bike infrastructure on
# this edge". `none` is the fourth value and the only one excluded.
_BIKE_LANES = frozenset({"shared", "dedicated", "separated"})

# `edge.use` values that ARE the bike infrastructure rather than merely carrying
# some. A trail is not "a road with a lane on it", it is the thing itself.
_BIKEWAY_USES = frozenset({"cycleway", "path", "footway"})


def bikeway_share(trip: dict[str, Any], costing_options: dict[str, Any],
                  shape: list[tuple[float, float]] | None = None) -> float | None:
    """Fraction of a trip's length ridden on Denver's designated bike network.

    WHY THIS IS SCORED HERE AND NOT ASKED OF THE ROUTER. Valhalla knows about
    the network — `edge.bicycle_network` comes back `1` (local) on Hazel Court
    and `0` on Hooker Street one block west, which is exactly the distinction a
    rider wants made. What it will not do is act on it: the bike-network
    discount is a hardcoded 0.95 applied inside the costing model, with no
    request-tunable lever, and 5% is not enough to reorder anything. Measured on
    the reported pair (3158 W 8th Ave -> Lakewood Gulch Trail), the router rode
    27 m north on Hazel Court — a designated neighbourhood bikeway, tagged
    `bicycle=designated` and carried in the `lcn` relation "Denver D3" — then
    turned LEFT off it, jogged 85 m west, and climbed 606 m of untagged
    residential Hooker Street instead. Staying on the bikeway the whole way
    costs 13 m on a 1,629 m ride: 0.8%. The preference was not traded away, it
    was never priced.

    Same shape as `shade_score` and `street_share` above, and the same reason:
    the graph will not rank this, so the response is ranked instead.

    WHAT COUNTS, AND WHY ABSENCE MEANS SOMETHING HERE. Three signals, all of
    them already in `trace_attributes` and so free of any new sidecar asset:

    * ``bicycle_network`` — membership of an OSM bicycle route relation. This is
      Denver's own D-numbered neighbourhood bikeway network, 346 km of it.
    * ``cycle_lane`` — a painted, dedicated or separated lane on the edge.
    * ``use`` in cycleway/path/footway — the off-street trails.

    Together 630 km of the clip's 8,421 km of bike-usable road and path, and
    83% of everything OSM flags as bike infrastructure. The remaining 17% is
    `bicycle=designated` carried on a way with no relation and no lane tag,
    which Valhalla folds into access rather than exposing — two of Hazel
    Court's own southern segments are in that gap, though the northbound
    segment this bug is about is not. Closing it means publishing a bikeway
    table from denver-map-prep the way canopy coverage already is; until then
    this scorer under-counts, and under-counting only ever costs a route it
    should have preferred, never invents one.

    Note the contrast with `street_share` directly above, which explicitly
    REFUSED to rank on `lit=*` at 3.2% coverage. The difference is not the
    percentage, it is what a missing tag means. An unlit-tagged street is a
    street nobody has surveyed; an untagged residential street is genuinely not
    a designated bikeway. Sparse knowledge cannot be ranked on. A sparse
    network is the network.

    Returns None when the trip can't be snapped back onto the graph — callers
    treat that as "unknown" rather than "no bikeway", so a failed trace never
    silently reorders routes.
    """
    if shape is None:
        shape = valhalla.trip_shape(trip)
    if len(shape) < 2:
        return None
    try:
        edges = valhalla.trace_attributes(
            shape, costing_options,
            attributes=("edge.length", "edge.bicycle_network",
                        "edge.cycle_lane", "edge.use"))
    except valhalla.ValhallaError as exc:
        log.warning("bikeway scoring failed to trace route: %s", exc)
        return None

    total = 0.0
    on_network = 0.0
    for edge in edges:
        length = edge.get("length") or 0.0
        if length <= 0:
            continue
        total += length
        if (edge.get("bicycle_network") or 0) \
                or (edge.get("cycle_lane") or "").lower() in _BIKE_LANES \
                or (edge.get("use") or "").lower() in _BIKEWAY_USES:
            on_network += length
    if total <= 0:
        return None
    return round(on_network / total, 4)


#: Shortest off-network stretch worth manufacturing a detour around, in metres.
#:
#: Below this there is nothing to fix: every route leaves the network for the
#: last block to a front door, and re-routing around 80 m of residential street
#: spends a Valhalla call to discover that the rider must still ride down their
#: own road. 200 m is about two Denver blocks.
_BIKEWAY_DETOUR_MIN_METERS = 200.0


def _bikeway_detour_candidate(
    points: list[tuple[float, float]],
    profile: RouteProfile,
    trip: dict[str, Any],
    shape: list[tuple[float, float]],
) -> dict[str, Any] | None:
    """Manufacture one route that avoids this trip's worst off-network stretch.

    WHY THIS EXISTS. Ranking can only choose among the routes Valhalla offers,
    and on the reported pair Valhalla offers two — neither of them the bikeway.
    Asking for more does not help: `alternates` at 3, 5, 8 and 12 all return the
    same two candidates. The bike-network route was not out-ranked, it was never
    a candidate, so no re-ranking of any strength could have found it.

    So it is constructed. Trace the chosen route, find the longest CONSECUTIVE
    run of edges that are not on the bike network, and ask Valhalla for a route
    that may not pass through that run's midpoint. That is a well-posed
    question — "get me there without riding down the middle of Hooker Street" —
    and Valhalla answers it directly.

    On the reported pair (3158 W 8th Ave -> Lakewood Gulch Trail) the worst run
    is 613 m of Hooker Street, and excluding its midpoint returns the route up
    Hazel Court: 1,642 m at 73.0% on the network against 1,629 m at 40.3%.
    Thirteen metres — 0.8% — for two thirds of the ride moved onto a designated
    bikeway. Priced at BIKEWAY_METER_DISCOUNT that is 1,462 effective metres
    against 1,531, so the ranking now has something to choose and chooses it.

    Costs exactly one extra /route. The trace is already paid for by scoring.

    Returns None whenever the detour is not worth asking for or Valhalla cannot
    answer — a manufactured candidate is a bonus, never a requirement, and this
    must never be able to fail a request that would otherwise have succeeded.
    """
    if len(shape) < 2:
        return None
    try:
        edges = valhalla.trace_attributes(
            shape, profile.costing_options,
            attributes=("edge.length", "edge.bicycle_network", "edge.cycle_lane",
                        "edge.use", "edge.begin_shape_index", "edge.end_shape_index"))
    except valhalla.ValhallaError as exc:
        log.warning("bikeway detour failed to trace route: %s", exc)
        return None

    # Longest consecutive off-network run, measured in metres and remembered by
    # the shape indices that bound it.
    best: tuple[float, int, int] | None = None
    run_len, run_begin, run_end = 0.0, None, None
    for edge in edges:
        length = (edge.get("length") or 0.0) * 1000.0
        on_network = ((edge.get("bicycle_network") or 0)
                      or (edge.get("cycle_lane") or "").lower() in _BIKE_LANES
                      or (edge.get("use") or "").lower() in _BIKEWAY_USES)
        if on_network:
            run_len, run_begin, run_end = 0.0, None, None
            continue
        if run_begin is None:
            run_begin = edge.get("begin_shape_index")
        run_end = edge.get("end_shape_index")
        run_len += length
        if run_begin is not None and run_end is not None \
                and (best is None or run_len > best[0]):
            best = (run_len, run_begin, run_end)

    if best is None or best[0] < _BIKEWAY_DETOUR_MIN_METERS:
        return None
    mid = (best[1] + best[2]) // 2
    if not 0 <= mid < len(shape):
        return None

    try:
        body = valhalla.route(points, profile.costing_options,
                              exclude_locations=[shape[mid]])
    except valhalla.ValhallaError as exc:
        # Routinely expected: excluding the only road through can leave no
        # route at all. That is an answer — this pair has no bikeway option.
        log.info("no route avoiding the off-network stretch: %s", exc)
        return None
    candidates = valhalla.all_trips(body)
    return candidates[0] if candidates else None


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


def _score_bikeway(trips: list[dict[str, Any]],
                   shapes: list[list[tuple[float, float]]],
                   costing_options: dict[str, Any],
                   ) -> list[tuple[float | None, dict[str, Any]]]:
    """Bike-network score for every candidate, computed concurrently.

    Same shape and the same reasoning as `_score_alternates`, with one extra
    reason to bother: this runs on the DEFAULT profile, so the cost is paid by
    every rider on every route rather than by whoever opted into shade.

    Order is preserved. `as_completed` would return these in whatever order the
    traces finish, and the caller breaks ties with `min`, so an unstable order
    would make identically-scored routes come back differently between two
    requests for the same ride.
    """
    if len(trips) == 1:
        return [(bikeway_share(trips[0], costing_options, shapes[0]), trips[0])]

    def score(pair: tuple[dict[str, Any], list[tuple[float, float]]]) -> float | None:
        trip, shape = pair
        try:
            return bikeway_share(trip, costing_options, shape)
        except Exception as exc:  # noqa: BLE001 — one bad candidate must not fail the request
            log.warning("bikeway scoring raised for a candidate: %s", exc)
            return None

    with ThreadPoolExecutor(max_workers=min(len(trips), 4)) as pool:
        scores = list(pool.map(score, zip(trips, shapes)))
    return list(zip(scores, trips))


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
    bikeway_pct = None
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
    elif prof.rerank_by_bikeway:
        # Prefer Denver's designated bike network, PRICED AGAINST DISTANCE.
        # See `bikeway_share` for why the graph will not do this itself, and
        # BIKEWAY_METER_DISCOUNT for how the exchange rate was calibrated.
        #
        # Ranked the way the Range Maximizer is, and for the reason that one
        # had to be fixed: a preference that ignores distance is not a
        # preference, it is a detour generator. `max(share)` would send a rider
        # any distance at all to touch a bikeway. Discounting bikeway metres
        # and minimising the total keeps both axes in one comparable number.
        #
        # No baseline route is added here, unlike shade/night/range above.
        # Those profiles each ride their OWN costing options, so ranking within
        # their route family can do worse than the default would have. This IS
        # the default profile — its candidates already are the baseline, and
        # re-adding it would score the same trip twice.
        shapes = [valhalla.trip_shape(t) for t in trips]
        # Valhalla's alternates do not include the bike-network route, so one
        # is manufactured from the primary's worst off-network stretch. Added
        # to the pool, not preferred: it still has to win on the same number.
        extra = _bikeway_detour_candidate(
            [origin, dest], prof, trips[0], shapes[0])
        if extra is not None:
            trips = trips + [extra]
            shapes = shapes + [valhalla.trip_shape(extra)]
        considered = len(trips)
        # Scored concurrently for the reason `_score_alternates` is: this is
        # the DEFAULT profile, so every rider pays this latency, and four
        # serial /trace_attributes calls are four round trips deep.
        rated = _score_bikeway(trips, shapes, prof.costing_options)
        measured = [(sh_, t) for sh_, t in rated if sh_ is not None]
        if measured:
            def _effective_meters(pair: tuple[float, dict[str, Any]]) -> tuple[float, float]:
                share_, trip = pair
                dist = float(valhalla.trip_summary(trip)["distance_meters"] or 0.0)
                # Every bikeway metre billed at the discount, every other metre
                # at full price. Distance breaks ties so two routes priced the
                # same resolve to the shorter one.
                return (dist * (1.0 - share_ * (1.0 - BIKEWAY_METER_DISCOUNT)), dist)

            bikeway_pct, chosen = min(measured, key=_effective_meters)
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

        # RANKED BY PREDICTED ENERGY, NOT BY CLIMB. This used to be
        # `min(gain)` — the flattest alternate won outright, whatever it cost
        # in distance. Measured against production on the reported pair,
        # Federal & 8th -> 10th & Knox: it detoured 722 m to save 4.7 m of
        # climb. On another it returned a route byte-identical to `safe`.
        #
        # The profile is called The Range Maximizer, and range is spent on
        # BOTH axes. Four metres of climb is a rounding error against most of
        # a kilometre of extra road, and a rider who picked this profile to
        # get further was being sent further out of their way to get less far.
        #
        # `estimate_burn_percent` already prices exactly this tradeoff, from
        # the fitted model, in the units the rider cares about — and it is the
        # same function that fills in `battery_percent_estimate` a few lines
        # below, so the route is now ranked by the number it reports.
        def _cost(trip: dict[str, Any]) -> tuple[float, float] | None:
            gain = valhalla.elevation_gain_meters(trip)
            if gain is None:
                # None is not flat, it is unmeasured — such a trip keeps
                # Valhalla's own ranking rather than winning by default.
                return None
            summary = valhalla.trip_summary(trip)
            burn = battery_model.estimate_burn_percent(
                distance_meters=summary["distance_meters"],
                elevation_gain_meters=gain,
                vehicle_model=vehicle_model,
            )
            percent = burn.get("percent")
            if percent is None:
                # No model yet (or none for this vehicle). Fall back to the
                # old behaviour rather than to nothing: climb-only ranking is
                # wrong about tradeoffs but still beats not ranking at all,
                # and it is what this profile did before the model existed.
                return (float(gain), float(summary["distance_meters"] or 0.0))
            # Distance breaks ties, so two routes the model prices the same
            # resolve to the shorter one instead of to whichever Valhalla
            # happened to list first.
            return (float(percent), float(summary["distance_meters"] or 0.0))

        rated = [(_cost(t), t) for t in trips]
        measured = [(c, t) for c, t in rated if c is not None]
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
        # Fraction of the ride on Denver's designated bike network. Only
        # measured on profiles that rank by it — None elsewhere means "not
        # scored", not "no bikeway".
        "bikeway_share": bikeway_pct,
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


# Walking costing for the leg BEFORE the ride. Deliberately small and fixed:
# there is nothing for a rider to tune about walking two blocks, so this is not
# a selectable profile and does not appear in /route/profiles — that list means
# "how do you want to RIDE", and putting a walk in it would offer a scooter
# route the rider cannot take on foot and vice versa.
#
# `walking_speed` is Valhalla's km/h. 4.5 is a shade under its 5.1 default:
# somebody crossing a city block to a scooter, phone in hand, checking numbers
# against a photo, is not walking at a commuter's pace, and an optimistic ETA
# on a two-minute walk is the kind of small lie that makes a rider stop
# believing the other numbers.
WALK_COSTING_OPTIONS: dict[str, Any] = {
    "walking_speed": 4.5,
    # Sidewalks and crossings are the point — a rider on foot is not bound by
    # the High Injury Network exclusions the bicycle profiles enforce.
    "use_ferry": 0,
}


# How much charge a rider should still have when they get there. Not zero:
# a scooter that arrives empty stranded them for the last block, and Veo will
# not start a vehicle in the low single digits at all (observed on their own
# map — devices showing 3-4% are displayed but refuse to start). Ten points is
# a short detour's worth of slack on top of that.
ARRIVAL_RESERVE_PERCENT = 10.0


def _shape_key(coords: list[list[float]]) -> str:
    """Identity of a road, to five decimal places (~1 m).

    Two profiles that produce the same road ARE the same route, however
    differently they were asked for. Rounding rather than comparing floats
    exactly because the same shape reaches us via two independent polyline
    decodes.
    """
    return "|".join(f"{lon:.5f},{lat:.5f}" for lon, lat in coords)


def _arrival_battery(burn: dict[str, Any],
                     battery_percent: float | None) -> dict[str, Any]:
    """Turn a predicted BURN into "what you will have left when you arrive".

    The burn is what the model predicts; the arrival percentage is what the
    rider actually wants and cannot work out in their head while standing in
    the street. Both ends of the burn band are carried through, and the
    will-it-make-it verdict is taken from the PESSIMISTIC end — the whole
    point of having a band is to use it when the answer matters, and here the
    cost of being wrong is being stranded.
    """
    out: dict[str, Any] = {
        "arrival_percent": None,
        "arrival_percent_low": None,
        "arrival_percent_high": None,
        "will_make_it": None,
        "reserve_percent": ARRIVAL_RESERVE_PERCENT,
    }
    percent = burn.get("percent")
    if battery_percent is None or percent is None:
        return out

    def left(spent: float | None) -> float | None:
        if spent is None:
            return None
        return round(max(0.0, battery_percent - spent), 1)

    # The HIGH burn is the LOW arrival. Naming them by what the rider has when
    # they get there, not by what the model spent, so `arrival_percent_low`
    # means the bad case in both directions.
    out["arrival_percent"] = left(percent)
    out["arrival_percent_low"] = left(burn.get("percent_high"))
    out["arrival_percent_high"] = left(burn.get("percent_low"))
    worst = out["arrival_percent_low"]
    out["will_make_it"] = (worst if worst is not None else out["arrival_percent"]) \
        >= ARRIVAL_RESERVE_PERCENT
    return out


@router.get("/api/v1/route/options", dependencies=[Depends(_limit_route_ip)])
def route_options(
    from_: str = Query(..., alias="from", description="Origin as 'lat,lon'"),
    to: str = Query(..., description="Destination as 'lat,lon'"),
    vehicle_model: str | None = Query(
        None, description="Vehicle model for a model-specific battery estimate"),
    battery_percent: float | None = Query(
        None, ge=0, le=100,
        description="The vehicle's CURRENT charge, so each option can say what "
                    "will be left on arrival and whether it will get there"),
) -> dict[str, Any]:
    """Every genuinely different route to a destination, once each.

    THE PROBLEM THIS SOLVES. Asking for all five profiles separately offered
    the rider five choices that were two roads. Measured on one Denver trip:

        safe     2456 m  601 s  164 pts  shape A
        range    2456 m  601 s  164 pts  shape A
        shade    2456 m  601 s  164 pts  shape A
        night    2370 m  558 s  123 pts  shape B
        express  2370 m  421 s  123 pts  shape B

    Two failures, both bad. Three entries were the same road under different
    names. And `night` and `express` quoted 9 minutes and 7 minutes for a
    BYTE-IDENTICAL shape — the difference is entirely a costing artefact
    (their speed knobs differ), not anything the rider would experience. An
    app that says the same road takes two different lengths of time has told
    the rider its numbers are decorative.

    So: route every profile, group by the shape that comes back, and return
    one option per distinct road.

    WHICH DURATION SURVIVES a group is the interesting question, since they
    are all estimates of the same ride. The slowest, deliberately. They are
    all Valhalla BICYCLE estimates on a graph built for bicycles, and Denver
    caps these scooters around 15 mph, so the optimistic end is the least
    defensible number in the set. An ETA that runs long costs a rider nothing;
    one that runs short is how somebody misses the thing they were riding to.
    Same reasoning as the walking pace in `walk`.

    The dropped profiles are not hidden — each option lists the other names
    that produce it, so a rider looking for "the shaded one" can still see
    that it is this one.
    """
    cfg = load().valhalla
    origin = _parse_point(from_, "from")
    dest = _parse_point(to, "to")

    for label, (lat, lon) in (("from", origin), ("to", dest)):
        if not cfg.contains(lat, lon):
            raise HTTPException(400, {
                "error": "out_of_coverage",
                "detail": f"{label} ({lat}, {lon}) is outside the routing graph",
                "graph_bbox": cfg.bbox,
            })

    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    failures: list[str] = []

    for prof in cfg.profiles:
        try:
            body = _route_with_retry([origin, dest], prof)
        except valhalla.ValhallaError:
            # One profile failing is not the request failing: the HIN
            # exclusions mean `safe` can legitimately find nothing where
            # `express` does. Note it and carry on.
            failures.append(prof.key)
            continue
        trips = valhalla.all_trips(body)
        if not trips:
            failures.append(prof.key)
            continue
        trip = trips[0]
        shape = valhalla.trip_shape(trip)
        geometry = valhalla.to_geojson(shape)
        key = _shape_key(geometry["coordinates"])
        summary = valhalla.trip_summary(trip)

        group = groups.get(key)
        if group is None:
            groups[key] = {
                "key": prof.key,
                "label": prof.label,
                "also": [],
                "geometry": geometry,
                "summary": summary,
                "profile": prof,
            }
            order.append(key)
            continue
        group["also"].append({"key": prof.key, "label": prof.label})
        # Conservative: the slowest estimate of the same road wins.
        if (summary["duration_seconds"] or 0) > (group["summary"]["duration_seconds"] or 0):
            group["summary"]["duration_seconds"] = summary["duration_seconds"]

    if not groups:
        raise HTTPException(422, {
            "error": "no_route",
            "detail": "No route exists between these locations.",
            "profiles_tried": [p.key for p in cfg.profiles],
        })

    options: list[dict[str, Any]] = []
    for key in order:
        g = groups[key]
        summary = g["summary"]
        burn = battery_model.estimate_burn_percent(
            distance_meters=summary["distance_meters"],
            elevation_gain_meters=summary["elevation_gain_meters"],
            vehicle_model=vehicle_model,
        )
        options.append({
            "key": g["key"],
            "label": g["label"],
            "also": g["also"],
            **summary,
            "battery_percent_estimate": burn.get("percent"),
            "battery_percent_low": burn.get("percent_low"),
            "battery_percent_high": burn.get("percent_high"),
            "battery_model": burn.get("source"),
            **_arrival_battery(burn, battery_percent),
            "geometry": g["geometry"],
        })

    return {
        "graph_bbox": cfg.bbox,
        "beta_warning": NAV_BETA_WARNING,
        "profiles_unavailable": failures,
        "options": options,
    }


@router.get("/api/v1/route/walk", dependencies=[Depends(_limit_route_ip)])
def walk(
    from_: str = Query(..., alias="from", description="Origin as 'lat,lon'"),
    to: str = Query(..., description="Destination as 'lat,lon'"),
    maneuvers: Annotated[bool, Query(
        description="Include turn-by-turn walking directions")] = False,
) -> dict[str, Any]:
    """Walk from where the rider is standing to the vehicle they picked.

    THE LEG THAT WAS MISSING. Choosing a scooter told the rider it was 300 m
    away and drew a dashed straight line to it — then handed them off to Google
    or Apple Maps to actually get there. That is the one moment the app has a
    router of its own and was not using it, and it is also the moment a rider
    is standing on a pavement deciding whether to trust the app at all.

    Pedestrian costing runs on the SAME tiles the bicycle profiles use — no
    rebuild, no second graph — so this is the existing router asked a different
    question. Crucially it is not one of the bicycle profiles: those exclude
    the High Injury Network, which is a sensible thing to avoid riding along
    and a nonsense thing to avoid walking along.
    """
    cfg = load().valhalla
    origin = _parse_point(from_, "from")
    dest = _parse_point(to, "to")

    for label, (lat, lon) in (("from", origin), ("to", dest)):
        if not cfg.contains(lat, lon):
            raise HTTPException(400, {
                "error": "out_of_coverage",
                "detail": f"{label} ({lat}, {lon}) is outside the routing graph",
                "graph_bbox": cfg.bbox,
            })

    try:
        body = valhalla.route([origin, dest], WALK_COSTING_OPTIONS,
                              costing="pedestrian", with_elevation=False)
    except valhalla.ValhallaError as exc:
        # A walk that cannot be routed is not a dead end the way an unroutable
        # ride is: the rider can see the scooter on the map and walk to it.
        # Say so plainly rather than pretending the vehicle is unreachable.
        if exc.no_suitable_edges or exc.no_path:
            raise HTTPException(422, {
                "error": "no_walking_route",
                "detail": "No walking route found between these points.",
            }) from exc
        raise

    trips = valhalla.all_trips(body)
    if not trips:
        raise HTTPException(422, {
            "error": "no_walking_route",
            "detail": "No walking route found between these points.",
        })
    trip = trips[0]
    shape = valhalla.trip_shape(trip)
    summary = valhalla.trip_summary(trip)
    properties: dict[str, Any] = {
        "mode": "walk",
        "distance_meters": summary["distance_meters"],
        "duration_seconds": summary["duration_seconds"],
    }
    if maneuvers:
        properties["maneuvers"] = valhalla.trip_maneuvers(trip)
    return {
        "type": "Feature",
        "geometry": valhalla.to_geojson(shape),
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
