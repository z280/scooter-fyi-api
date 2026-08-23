"""GET /api/v1/route — profile mapping, coverage guard, and shade re-ranking.

Valhalla itself is stubbed: these tests lock in the translation layer (rider
profile -> costing options, bbox rejection, alternate re-ranking) rather than
Valhalla's routing quality, which is verified against the live container.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from src import api_route, valhalla
from src.config import load


# --- helpers ----------------------------------------------------------------

def _leg(shape: str, elevation=None):
    leg = {"shape": shape, "summary": {}}
    if elevation is not None:
        leg["elevation"] = elevation
    return leg


def _trip(length_km=2.0, time_s=600.0, shape="_p~iF~ps|U", elevation=None):
    return {
        "legs": [_leg(shape, elevation)],
        "summary": {"length": length_km, "time": time_s},
    }


@pytest.fixture(autouse=True)
def _no_battery_model(monkeypatch):
    """Routes must serve before any model has been fit."""
    monkeypatch.setattr(
        api_route.battery_model, "estimate_burn_percent",
        lambda **kw: {"percent": None, "source": "unavailable", "reason": "no_model"},
    )


@pytest.fixture(autouse=True)
def _clear_canopy_cache():
    api_route._CANOPY = None
    yield
    api_route._CANOPY = None


@pytest.fixture(autouse=True)
def _clear_bikeways_cache():
    """Without this the first test to touch the sidecar caches whatever the
    host happens to have on disk, and every later test inherits it."""
    api_route._BIKEWAYS = None
    yield
    api_route._BIKEWAYS = None


# --- profiles ---------------------------------------------------------------

def test_every_profile_is_configured():
    cfg = load().valhalla
    assert {p.key for p in cfg.profiles} == {"safe", "range", "shade", "express", "night"}
    assert cfg.default_profile == "safe"


def test_only_the_reranked_profiles_ask_for_alternates():
    """A profile pays for alternates if and only if it ranks them.

    Shade stays opt-in (§2C): a rider asking for `express` should never be
    steered toward tree cover. `range` also ranks its alternates, but on
    climb rather than canopy -- Valhalla's `use_hills` is inert on this graph,
    so the flattest route has to be chosen outside it.

    `safe` joined them: it ranks on bike-network share, because Valhalla's own
    bike-network preference is a hardcoded 0.95 with no request-tunable lever
    and is far too weak to reorder anything. `express` is now the only profile
    that takes Valhalla's first answer unexamined, which is the point of it.
    """
    cfg = load().valhalla
    for p in cfg.profiles:
        if p.key == "shade":
            assert p.rerank_by_shade is True
            assert p.rerank_by_elevation is False
            assert p.alternates >= 2
        elif p.key == "range":
            assert p.rerank_by_elevation is True
            assert p.rerank_by_shade is False
            assert p.alternates >= 2
        elif p.key == "night":
            assert p.rerank_by_street_share is True
            assert p.rerank_by_shade is False
            assert p.rerank_by_elevation is False
            assert p.alternates >= 2
        elif p.key == "safe":
            assert p.rerank_by_bikeway is True
            assert p.rerank_by_shade is False
            assert p.rerank_by_elevation is False
            assert p.rerank_by_street_share is False
            assert p.alternates >= 2
        else:
            assert p.rerank_by_shade is False
            assert p.rerank_by_elevation is False
            assert p.rerank_by_street_share is False
            assert p.rerank_by_bikeway is False
            assert p.alternates == 0


def test_profiles_endpoint_exposes_elevation_ranking():
    out = api_route.profiles()
    by_key = {p["key"]: p for p in out["profiles"]}
    assert by_key["range"]["elevation_ranked"] is True
    assert by_key["shade"]["elevation_ranked"] is False
    assert by_key["shade"]["shade_ranked"] is True


def test_profile_costing_matches_intent():
    cfg = load().valhalla
    assert cfg.profile("express").costing_options["bicycle_type"] == "Road"
    # Range Maximizer must refuse hills outright.
    assert cfg.profile("range").costing_options["use_hills"] == 0.0
    # Safe & Protected must be the most road-averse of the four.
    use_roads = {p.key: p.costing_options.get("use_roads", 1.0) for p in cfg.profiles}
    assert use_roads["safe"] == min(use_roads.values())


def test_unknown_profile_rejected():
    with pytest.raises(HTTPException) as exc:
        api_route.route(from_="39.74,-104.99", to="39.70,-104.95", profile="nope")
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "unknown_profile"


# --- coverage guard ----------------------------------------------------------

def test_out_of_graph_coordinate_is_rejected_not_clamped():
    """A silently relocated origin would yield a confident, wrong battery
    estimate — so this must 400 rather than snap to the nearest edge."""
    # Inside the app's DENVER_BOUNDS but outside the routing graph's clip.
    with pytest.raises(HTTPException) as exc:
        api_route.route(from_="39.88,-105.10", to="39.70,-104.95", profile="safe")
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "out_of_coverage"
    assert exc.value.detail["graph_bbox"] == load().valhalla.bbox


@pytest.mark.parametrize("bad", ["39.74", "abc,def", "39.74,-104.99,1"])
def test_malformed_coordinates_rejected(bad):
    with pytest.raises(HTTPException) as exc:
        api_route.route(from_=bad, to="39.70,-104.95", profile="safe")
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "bad_coordinate"


# --- error translation -------------------------------------------------------

def test_no_suitable_edges_becomes_422_after_retry(monkeypatch):
    """HIN ways are bicycle=no, so snapping failures are expected — they must
    surface as a specific 422, not a generic 500."""
    calls = []

    def fake_route(points, costing_options, alternates=0, radius=None, **kw):
        calls.append(radius)
        raise valhalla.ValhallaError("no suitable edges", code=171)

    monkeypatch.setattr(api_route.valhalla, "route", fake_route)
    with pytest.raises(HTTPException) as exc:
        api_route.route(from_="39.74,-104.99", to="39.70,-104.95", profile="safe")
    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "no_route_from_location"
    # Retried once with a wider radius before giving up.
    assert calls == [None, load().valhalla.retry_radius_meters]


def test_router_down_becomes_503(monkeypatch):
    def fake_route(*a, **kw):
        raise valhalla.ValhallaError("connection refused")

    monkeypatch.setattr(api_route.valhalla, "route", fake_route)
    with pytest.raises(HTTPException) as exc:
        api_route.route(from_="39.74,-104.99", to="39.70,-104.95", profile="safe")
    assert exc.value.status_code == 503


# --- shade re-ranking --------------------------------------------------------

def test_shade_profile_picks_the_leafiest_alternate(monkeypatch):
    shady = _trip(length_km=3.0, shape="SHADY")
    plain = _trip(length_km=2.0, shape="PLAIN")

    monkeypatch.setattr(
        api_route.valhalla, "route",
        lambda *a, **kw: {"trip": plain, "alternates": [{"trip": shady}]})
    monkeypatch.setattr(
        api_route.valhalla, "trip_shape",
        lambda trip: [(1.0, 1.0), (2.0, 2.0)] if trip is plain else [(3.0, 3.0), (4.0, 4.0)])
    monkeypatch.setattr(
        api_route.valhalla, "to_geojson",
        lambda pts: {"type": "LineString", "coordinates": []})

    def fake_trace(shape, costing_options, shape_match="edge_walk"):
        if shape[0][0] == 1.0:          # the "plain" route
            return [{"way_id": 1, "length": 1.0}]
        return [{"way_id": 2, "length": 1.0}]

    monkeypatch.setattr(api_route.valhalla, "trace_attributes", fake_trace)
    api_route._CANOPY = {1: 0.05, 2: 0.80}

    out = api_route.route(from_="39.74,-104.99", to="39.70,-104.95", profile="shade")
    # The longer-but-shadier alternate wins on the shade profile.
    assert out["properties"]["shade_score"] == pytest.approx(0.80)
    assert out["properties"]["distance_meters"] == pytest.approx(3000.0)


def test_missing_canopy_sidecar_leaves_valhalla_ranking_intact(monkeypatch):
    """An absent coverage table must not silently reorder routes."""
    first = _trip(length_km=2.0)
    second = _trip(length_km=5.0)
    monkeypatch.setattr(
        api_route.valhalla, "route",
        lambda *a, **kw: {"trip": first, "alternates": [{"trip": second}]})
    monkeypatch.setattr(
        api_route.valhalla, "to_geojson",
        lambda pts: {"type": "LineString", "coordinates": []})
    api_route._CANOPY = {}

    out = api_route.route(from_="39.74,-104.99", to="39.70,-104.95", profile="shade")
    assert out["properties"]["shade_score"] is None
    assert out["properties"]["distance_meters"] == pytest.approx(2000.0)


def test_unmeasured_ways_are_excluded_from_both_sides(monkeypatch):
    """An unmeasured way is unknown, not treeless.

    Scoring it 0 penalised whole route classes for a gap in the input data —
    with only residential ways measured, that made the shade profile avoid
    tree-lined cycleways, which are Denver's shadiest routes.
    """
    monkeypatch.setattr(
        api_route.valhalla, "trip_shape", lambda trip: [(1.0, 1.0), (2.0, 2.0)])
    monkeypatch.setattr(
        api_route.valhalla, "trace_attributes",
        lambda *a, **kw: [{"way_id": 1, "length": 1.0}, {"way_id": 99, "length": 3.0}])
    api_route._CANOPY = {1: 1.0}

    # way 99 is unmeasured: dropped from numerator AND denominator, so the score
    # is the mean over what was actually measured, not 1/(1+3).
    assert api_route.shade_score(_trip(), {}) == pytest.approx(1.0)


def test_score_is_none_when_nothing_on_the_route_was_measured(monkeypatch):
    monkeypatch.setattr(
        api_route.valhalla, "trip_shape", lambda trip: [(1.0, 1.0), (2.0, 2.0)])
    monkeypatch.setattr(
        api_route.valhalla, "trace_attributes",
        lambda *a, **kw: [{"way_id": 99, "length": 3.0}])
    api_route._CANOPY = {1: 1.0}
    assert api_route.shade_score(_trip(), {}) is None


def test_empty_canopy_load_is_retried_not_cached_forever(monkeypatch):
    """pipeline_worker does not depend on valhalla_map_fetch, so on a cold start
    the sidecar may not have landed yet. Caching that miss for the process
    lifetime would disable shade re-ranking silently."""
    calls = []

    def loader():
        calls.append(1)
        return {} if len(calls) == 1 else {7: 0.5}

    monkeypatch.setattr(api_route, "load_canopy_coverage", loader)
    api_route._CANOPY = None
    api_route._CANOPY_LOADED_AT = 0.0

    assert api_route._canopy() == {}          # miss
    api_route._CANOPY_LOADED_AT = 0.0          # simulate the retry interval elapsing
    assert api_route._canopy() == {7: 0.5}     # retried and succeeded
    assert api_route._canopy() == {7: 0.5}     # success is cached
    assert len(calls) == 2


def test_shade_includes_the_default_profile_route_as_a_candidate(monkeypatch):
    """Shade's costing generates a different route family from the default, so
    re-ranking only within it can return LESS canopy than not asking for shade.
    Measured at -0.0026 on a real Denver pair before this was added."""
    seen_costings = []

    def fake_route(points, costing_options, alternates=0, radius=None, **kw):
        seen_costings.append(costing_options)
        return {"trip": _trip(shape=str(costing_options))}

    monkeypatch.setattr(api_route.valhalla, "route", fake_route)
    monkeypatch.setattr(api_route.valhalla, "trip_shape", lambda t: [(1.0, 1.0), (2.0, 2.0)])
    monkeypatch.setattr(api_route.valhalla, "trace_attributes",
                        lambda *a, **kw: [{"way_id": 1, "length": 1.0}])
    monkeypatch.setattr(api_route.valhalla, "to_geojson",
                        lambda pts: {"type": "LineString", "coordinates": []})
    api_route._CANOPY = {1: 0.5}

    out = api_route.route(from_="39.74,-104.99", to="39.70,-104.95",
                          profile="shade", explain=True)
    # Two route calls: the shade costing and the default profile's.
    assert len(seen_costings) == 2
    default_opts = load().valhalla.profile(load().valhalla.default_profile).costing_options
    assert default_opts in seen_costings
    assert out["properties"]["diagnostics"]["alternates_considered"] >= 2


# --- elevation ---------------------------------------------------------------

def test_elevation_gain_sums_only_ascent():
    trip = _trip(elevation=[1600.0, 1610.0, 1605.0, 1615.0])
    # +10, -5, +10 -> 20m of climbing.
    assert valhalla.elevation_gain_meters(trip) == pytest.approx(20.0)


def test_elevation_gain_is_none_without_elevation_data():
    """A graph built without build_elevation must report None, not 0.0 — the
    battery model treats those very differently."""
    assert valhalla.elevation_gain_meters(_trip()) is None


# --- weather cache completeness (found by Copilot review of 90aeda4) ---------

def test_hours_missing_counts_interior_gaps_not_just_the_envelope():
    """A MIN/MAX envelope check reports a partially-backfilled range as fully
    covered, so trips in the hole silently get no temperature."""
    import inspect

    from src import weather
    src = inspect.getsource(weather._hours_missing)
    assert "COUNT(*)" in src
    # The old envelope-only approach must not come back.
    assert "MIN(observed_hour)" not in inspect.getsource(weather.ensure_coverage)


# --- range profile: flattest alternate wins -----------------------------------
#
# Reported from production, 3158 W 8th Ave -> Knox Station: the battery saver
# returned the HILLIEST of the four profiles (31.9 m climb) and an identical
# shape to `express`, while a 14.2 m alternate existed that was 2 m shorter.
# Valhalla's `use_hills` does not move the cost on this graph at any value.

def test_range_profile_picks_the_flattest_alternate(monkeypatch):
    hilly = _trip(length_km=2.0, elevation=[100, 130, 100])   # 30 m
    flat = _trip(length_km=2.1, elevation=[100, 105, 100])     # 5 m

    monkeypatch.setattr(
        api_route.valhalla, "route",
        lambda *a, **kw: {"trip": hilly, "alternates": [{"trip": flat}]})
    monkeypatch.setattr(
        api_route.valhalla, "to_geojson",
        lambda pts: {"type": "LineString", "coordinates": []})

    out = api_route.route(from_="39.74,-104.99", to="39.70,-104.95", profile="range")
    assert out["properties"]["elevation_gain_meters"] == pytest.approx(5.0)
    # The flatter route wins even though it is LONGER.
    assert out["properties"]["distance_meters"] == pytest.approx(2100.0)


def test_range_keeps_the_primary_when_it_is_already_flattest(monkeypatch):
    """Re-ranking must be a no-op, not a reshuffle, when there is nothing to gain."""
    flat = _trip(length_km=2.0, elevation=[100, 102, 100])
    hilly = _trip(length_km=2.5, elevation=[100, 140, 100])
    monkeypatch.setattr(
        api_route.valhalla, "route",
        lambda *a, **kw: {"trip": flat, "alternates": [{"trip": hilly}]})
    monkeypatch.setattr(
        api_route.valhalla, "to_geojson",
        lambda pts: {"type": "LineString", "coordinates": []})

    out = api_route.route(from_="39.74,-104.99", to="39.70,-104.95", profile="range")
    assert out["properties"]["distance_meters"] == pytest.approx(2000.0)


def test_unmeasured_elevation_never_wins_by_default(monkeypatch):
    """A trip with no elevation samples is unmeasured, not flat.

    Valhalla returns no `elevation` array when the graph was built without
    elevation data. Treating that as 0 m of climb would hand every such route
    the win and silently disable the whole feature.
    """
    known = _trip(length_km=2.0, elevation=[100, 120, 100])   # 20 m
    unknown = _trip(length_km=9.0)                          # None
    monkeypatch.setattr(
        api_route.valhalla, "route",
        lambda *a, **kw: {"trip": known, "alternates": [{"trip": unknown}]})
    monkeypatch.setattr(
        api_route.valhalla, "to_geojson",
        lambda pts: {"type": "LineString", "coordinates": []})

    out = api_route.route(from_="39.74,-104.99", to="39.70,-104.95", profile="range")
    assert out["properties"]["distance_meters"] == pytest.approx(2000.0)


def test_range_considers_the_default_profiles_route_too(monkeypatch):
    """The rider must never get MORE climb than doing nothing would have.

    `range` has its own costing (use_roads 0.3) and so its own route family;
    ranking only within it can be worse on climb than the default's primary.
    Same guard shade already carries.
    """
    calls = []

    def fake_route(points, costing_options, **kw):
        calls.append(dict(costing_options))
        if costing_options.get("use_roads") == 0.3:      # range's own family
            return {"trip": _trip(length_km=2.0, elevation=[100, 140, 100])}
        return {"trip": _trip(length_km=3.0, elevation=[100, 108, 100])}

    monkeypatch.setattr(api_route.valhalla, "route", fake_route)
    monkeypatch.setattr(
        api_route.valhalla, "to_geojson",
        lambda pts: {"type": "LineString", "coordinates": []})

    out = api_route.route(from_="39.74,-104.99", to="39.70,-104.95", profile="range")
    assert len(calls) == 2, "expected the default profile to be routed as a baseline"
    # The default's flatter route wins.
    assert out["properties"]["elevation_gain_meters"] == pytest.approx(8.0)


# --- night profile ------------------------------------------------------------
#
# Reported 2026-08-10: "platte river trail is scary after dark". The honest
# signal would be OSM `lit=*`, but coverage across the Denver clip is 3.2% of
# ways and 4.2% of cycleways -- too sparse to rank on. So the profile ranks on
# street share, an explicit proxy: a lit corridor is overwhelmingly a street,
# and the unlit stretch a rider avoids at night is overwhelmingly an isolated
# trail.

def _edges(*pairs):
    """(length_km, use) -> trace_attributes rows."""
    return [{"length": L, "use": u} for L, u in pairs]


def test_street_share_is_length_weighted(monkeypatch):
    monkeypatch.setattr(api_route.valhalla, "trace_attributes",
                        lambda *a, **kw: _edges((3.0, "road"), (1.0, "cycleway")))
    monkeypatch.setattr(api_route.valhalla, "trip_shape",
                        lambda t: [(1.0, 1.0), (2.0, 2.0)])
    assert api_route.street_share(_trip(), {}) == pytest.approx(0.75)


def test_parking_aisles_do_not_count_as_street():
    """A car park is motor-vehicle surface but it is not a lit through-street,
    and counting it would score a parking lot as the safe night option."""
    assert "parking_aisle" not in api_route._STREET_USES
    assert "driveway" not in api_route._STREET_USES
    assert "road" in api_route._STREET_USES


def test_street_share_is_none_when_the_trace_fails(monkeypatch):
    """Unknown must never read as "no streets" -- same rule as shade."""
    def boom(*a, **kw):
        raise api_route.valhalla.ValhallaError("no match")
    monkeypatch.setattr(api_route.valhalla, "trace_attributes", boom)
    monkeypatch.setattr(api_route.valhalla, "trip_shape",
                        lambda t: [(1.0, 1.0), (2.0, 2.0)])
    assert api_route.street_share(_trip(), {}) is None


def test_night_profile_picks_the_most_street_based_alternate(monkeypatch):
    trail = _trip(length_km=2.0)
    street = _trip(length_km=2.4)
    monkeypatch.setattr(
        api_route.valhalla, "route",
        lambda *a, **kw: {"trip": trail, "alternates": [{"trip": street}]})
    monkeypatch.setattr(
        api_route.valhalla, "trip_shape",
        lambda t: [(1.0, 1.0), (2.0, 2.0)] if t is trail else [(3.0, 3.0), (4.0, 4.0)])
    monkeypatch.setattr(
        api_route.valhalla, "to_geojson",
        lambda pts: {"type": "LineString", "coordinates": []})

    def fake_trace(shape, costing_options, **kw):
        if shape[0][0] == 1.0:                       # the trail route
            return _edges((0.2, "road"), (1.8, "cycleway"))
        return _edges((2.2, "road"), (0.2, "footway"))

    monkeypatch.setattr(api_route.valhalla, "trace_attributes", fake_trace)
    out = api_route.route(from_="39.74,-104.99", to="39.70,-104.95", profile="night")
    # The longer, street-based alternate wins.
    assert out["properties"]["distance_meters"] == pytest.approx(2400.0)
    assert out["properties"]["street_share"] == pytest.approx(0.9167, abs=1e-3)


def test_night_falls_back_to_valhalla_ranking_when_nothing_traces(monkeypatch):
    first = _trip(length_km=2.0)
    second = _trip(length_km=5.0)
    monkeypatch.setattr(
        api_route.valhalla, "route",
        lambda *a, **kw: {"trip": first, "alternates": [{"trip": second}]})
    monkeypatch.setattr(
        api_route.valhalla, "to_geojson",
        lambda pts: {"type": "LineString", "coordinates": []})

    def boom(*a, **kw):
        raise api_route.valhalla.ValhallaError("no match")

    monkeypatch.setattr(api_route.valhalla, "trace_attributes", boom)
    out = api_route.route(from_="39.74,-104.99", to="39.70,-104.95", profile="night")
    assert out["properties"]["street_share"] is None
    assert out["properties"]["distance_meters"] == pytest.approx(2000.0)


def test_night_is_configured_and_advertised():
    cfg = load().valhalla
    night = cfg.profile("night")
    assert night is not None and night.rerank_by_street_share is True
    assert night.alternates >= 2
    # and it must not double up on another profile's ranking
    assert night.rerank_by_shade is False and night.rerank_by_elevation is False
    by_key = {p["key"]: p for p in api_route.profiles()["profiles"]}
    assert by_key["night"]["street_ranked"] is True
    assert by_key["safe"]["street_ranked"] is False


def test_the_range_maximizer_prices_climb_AGAINST_distance(monkeypatch):
    """It used to pick `min(elevation_gain)` outright — the flattest alternate
    won whatever it cost in road. Measured against production on the reported
    pair, Federal & 8th -> 10th & Knox: a 722 m detour to save 4.7 m of climb.

    The profile is called The Range Maximizer, and range is spent on BOTH
    axes. The fitted model (5,570 observations) prices a metre of climb at
    beta_elevation/beta_distance = 0.07171/0.000885, about 81 m of flat road:

        722 m detour to save 4.7 m climb  ->  worth 381 m  ->  REJECT
        341 m detour to save 6.9 m climb  ->  worth 559 m  ->  TAKE

    Both were measured on the live graph, and a correct fix has to get BOTH
    right. Climb-only ranking takes the first; distance-only refuses the
    second. Only pricing the tradeoff answers both, which is why this asserts
    on two cases in opposite directions rather than on "prefer shorter".

    The coefficients are pinned rather than read from the database: reading
    the live model would skip this wherever there is no database, which is CI,
    which is where it most needs to run.
    """
    BETA_D, BETA_E = 0.000885, 0.07171

    def priced(**kw):
        d = kw.get("distance_meters")
        e = kw.get("elevation_gain_meters") or 0.0
        if d is None:
            return {"percent": None, "source": "unavailable"}
        return {"percent": BETA_D * d + BETA_E * e, "source": "regression"}

    monkeypatch.setattr(
        api_route.battery_model, "estimate_burn_percent", priced)
    monkeypatch.setattr(
        api_route.valhalla, "to_geojson",
        lambda pts: {"type": "LineString", "coordinates": []})

    def run(primary, alternate):
        monkeypatch.setattr(
            api_route.valhalla, "route",
            lambda *a, **kw: {"trip": primary, "alternates": [{"trip": alternate}]})
        return api_route.route(
            from_="39.74,-104.99", to="39.70,-104.95", profile="range")

    # THE REPORTED CASE. A long detour for a trivial climb saving must lose.
    direct = _trip(length_km=3.131, elevation=[100, 163.3, 100])
    long_way = _trip(length_km=3.853, elevation=[100, 158.6, 100])
    out = run(direct, long_way)
    assert out["properties"]["distance_meters"] == pytest.approx(3131.0)

    # THE MEASURED CASE. A real climb saving must still win, even though the
    # winning route is longer — this is the assertion that fails if somebody
    # "fixes" the bug by ranking on distance.
    direct2 = _trip(length_km=3.131, elevation=[100, 163.3, 100])
    hillier_but_shorter = _trip(length_km=3.472, elevation=[100, 156.4, 100])
    out2 = run(direct2, hillier_but_shorter)
    assert out2["properties"]["distance_meters"] == pytest.approx(3472.0)


def test_range_still_ranks_by_climb_before_any_model_is_fit(monkeypatch):
    """The fallback. `estimate_burn_percent` returns None until a model
    exists, and climb-only ranking — wrong about tradeoffs but better than no
    ranking — is what this profile did before the model existed. Every other
    test in this file runs through this path, since the module's autouse
    fixture stubs the model away."""
    hilly = _trip(length_km=2.0, elevation=[100, 130, 100])
    flat = _trip(length_km=2.1, elevation=[100, 105, 100])
    monkeypatch.setattr(
        api_route.valhalla, "route",
        lambda *a, **kw: {"trip": hilly, "alternates": [{"trip": flat}]})
    monkeypatch.setattr(
        api_route.valhalla, "to_geojson",
        lambda pts: {"type": "LineString", "coordinates": []})
    out = api_route.route(from_="39.74,-104.99", to="39.70,-104.95", profile="range")
    assert out["properties"]["elevation_gain_meters"] == pytest.approx(5.0)


# --- the bike network -------------------------------------------------------

def _edge(length_km, *, network=0, lane="none", use="road", begin=0, end=1):
    return {"length": length_km, "bicycle_network": network, "cycle_lane": lane,
            "use": use, "begin_shape_index": begin, "end_shape_index": end}


@pytest.mark.parametrize("edge,on_network", [
    (_edge(1.0, network=1), True),                    # lcn route relation
    (_edge(1.0, network=4), True),                    # ncn — any bit counts
    (_edge(1.0, lane="dedicated"), True),             # painted lane
    (_edge(1.0, lane="separated"), True),             # protected lane
    (_edge(1.0, lane="shared"), True),                # sharrow
    (_edge(1.0, use="cycleway"), True),               # off-street trail
    (_edge(1.0, use="path"), True),
    # A SIDEWALK IS NOT A BIKEWAY. `footway` was in the set and credited a
    # route for clipping the pavement; a genuine shared-use path tagged
    # highway=footway carries bicycle=designated and arrives via the sidecar.
    (_edge(1.0, use="footway"), False),
    (_edge(1.0), False),                              # plain residential road
    (_edge(1.0, lane="none", use="road"), False),
])
def test_bikeway_share_counts_every_signal_the_graph_exposes(monkeypatch, edge, on_network):
    """Three independent signals mean "bike network", and missing all three
    means an ordinary street.

    Denver's neighbourhood bikeways carry the first (Hazel Court is in the
    `lcn` relation "Denver D3"), painted lanes the second, trails the third.
    A route scored on only one of them would miss most of the network.
    """
    monkeypatch.setattr(api_route.valhalla, "trace_attributes",
                        lambda *a, **kw: [edge])
    share = api_route.bikeway_share(_trip(), {}, [(39.7, -105.0), (39.71, -105.0)])
    assert share == (1.0 if on_network else 0.0)


def test_bikeway_share_is_none_when_the_trace_fails(monkeypatch):
    """Unknown is not zero. A trip that cannot be snapped back onto the graph
    must keep Valhalla's own ranking rather than being scored as bikeway-free
    and quietly losing to everything."""
    def boom(*a, **kw):
        raise api_route.valhalla.ValhallaError("no match")
    monkeypatch.setattr(api_route.valhalla, "trace_attributes", boom)
    assert api_route.bikeway_share(_trip(), {}, [(39.7, -105.0), (39.71, -105.0)]) is None


def test_safe_prices_bikeway_AGAINST_distance(monkeypatch):
    """The preference must be a tradeoff, not an obsession.

    This is the same trap the Range Maximizer fell into: ranking on `max(share)`
    would send a rider any distance at all to touch a bikeway. At
    BIKEWAY_METER_DISCOUNT a bikeway metre bills at 0.80 of an ordinary one, so
    an all-bikeway route wins only while it is under 1/0.80 = 25% longer.

    Both cases below are measured against that bound, in opposite directions,
    because a fix that only ever prefers the bikeway passes the first and a fix
    that only ever prefers the shorter route passes the second.
    """
    monkeypatch.setattr(api_route.valhalla, "to_geojson",
                        lambda pts: {"type": "LineString", "coordinates": []})
    monkeypatch.setattr(api_route, "_bikeway_detour_candidate",
                        lambda *a, **kw: None)

    def run(primary, alternate, shares):
        monkeypatch.setattr(
            api_route.valhalla, "route",
            lambda *a, **kw: {"trip": primary, "alternates": [{"trip": alternate}]})
        monkeypatch.setattr(
            api_route, "bikeway_share",
            lambda trip, opts, shape=None: shares[id(trip)])
        return api_route.route(from_="39.74,-104.99", to="39.70,-104.95",
                               profile="safe")

    # WORTH IT. The reported case: 1,629 m at 40.3% against 1,642 m at 73.0%.
    # Thirteen metres for two thirds of the ride on a designated bikeway.
    hooker = _trip(length_km=1.629)
    hazel = _trip(length_km=1.642)
    out = run(hooker, hazel, {id(hooker): 0.403, id(hazel): 0.730})
    assert out["properties"]["distance_meters"] == pytest.approx(1642.0)
    assert out["properties"]["bikeway_share"] == pytest.approx(0.730)

    # NOT WORTH IT. Same bikeway gain, but now the detour is 45% — past the
    # point where the discount pays for itself. The short route must win.
    direct = _trip(length_km=1.629)
    scenic = _trip(length_km=2.362)
    out2 = run(direct, scenic, {id(direct): 0.403, id(scenic): 0.730})
    assert out2["properties"]["distance_meters"] == pytest.approx(1629.0)


def test_bikeway_detour_skips_short_off_network_stretches(monkeypatch):
    """Every route leaves the network for the last block to a front door.
    Re-routing around 80 m of residential street spends a Valhalla call to
    rediscover that the rider must still ride down their own road."""
    monkeypatch.setattr(
        api_route.valhalla, "trace_attributes",
        lambda *a, **kw: [_edge(0.08, begin=0, end=1)])   # 80 m, under the floor
    called = []
    monkeypatch.setattr(api_route.valhalla, "route",
                        lambda *a, **kw: called.append(kw) or {"trip": _trip()})
    prof = load().valhalla.profile("safe")
    got = api_route._bikeway_detour_candidate(
        [(39.7, -105.0), (39.71, -105.0)], prof, _trip(),
        [(39.7, -105.0), (39.71, -105.0)])
    assert got is None
    assert called == []


def test_bikeway_detour_excludes_the_longest_off_network_run(monkeypatch):
    """It must route around the WORST stretch, not the first one it meets.

    On the reported pair the trip leaves the network twice — 27 m of Hazel
    Court at the very start, then 613 m of Hooker Street. Excluding the first
    changes nothing; excluding the second is what returns the bikeway route.
    """
    shape = [(39.70 + i * 0.001, -105.0) for i in range(11)]
    monkeypatch.setattr(api_route.valhalla, "trace_attributes", lambda *a, **kw: [
        _edge(0.027, begin=0, end=1),                       # 27 m off-network
        _edge(0.500, use="cycleway", begin=1, end=2),       # back on the network
        _edge(0.613, begin=2, end=8),                       # 613 m off-network
    ])
    seen = {}

    def fake_route(points, opts, **kw):
        seen.update(kw)
        return {"trip": _trip(length_km=1.642)}

    monkeypatch.setattr(api_route.valhalla, "route", fake_route)
    prof = load().valhalla.profile("safe")
    got = api_route._bikeway_detour_candidate(
        [(39.7, -105.0), (39.71, -105.0)], prof, _trip(), shape)
    assert got is not None
    # Midpoint of the 613 m run (shape indices 2..8), not of the 27 m one.
    assert seen["exclude_locations"] == [shape[5]]


def test_bikeway_detour_survives_having_no_answer(monkeypatch):
    """Excluding the only road through can leave no route at all. That is an
    answer — this pair has no bikeway option — and it must never fail a request
    that would otherwise have succeeded."""
    monkeypatch.setattr(api_route.valhalla, "trace_attributes",
                        lambda *a, **kw: [_edge(0.613, begin=0, end=4)])

    def boom(*a, **kw):
        raise api_route.valhalla.ValhallaError("no route")

    monkeypatch.setattr(api_route.valhalla, "route", boom)
    prof = load().valhalla.profile("safe")
    assert api_route._bikeway_detour_candidate(
        [(39.7, -105.0), (39.71, -105.0)], prof, _trip(),
        [(39.7 + i * 0.001, -105.0) for i in range(6)]) is None


def test_profiles_endpoint_advertises_bikeway_ranking():
    """Clients read this to know what a profile actually does; `safe` changing
    from "Valhalla's first answer" to "ranked on the bike network" is exactly
    the kind of thing they must not have to hardcode."""
    by_key = {p["key"]: p for p in api_route.profiles()["profiles"]}
    assert by_key["safe"]["bikeway_ranked"] is True
    assert by_key["express"]["bikeway_ranked"] is False
    assert by_key["shade"]["bikeway_ranked"] is False


def test_the_sidecar_sees_the_designated_ways_the_graph_cannot(monkeypatch):
    """THE 17% THE GRAPH WILL NOT REPORT.

    Valhalla exposes route-relation membership and cycle lanes but folds
    `bicycle=designated` into access parsing, so a way carrying only that tag
    reads as an ordinary street however it is queried. That is 126 km of Denver
    and two of Hazel Court's six segments — the street this preference exists
    for. denver-map-prep publishes those ways; this is where they land.
    """
    edge = _edge(1.0)                     # nothing the graph can see
    edge["way_id"] = 16983484             # Hazel Court, bicycle=designated only
    monkeypatch.setattr(api_route.valhalla, "trace_attributes",
                        lambda *a, **kw: [edge])

    monkeypatch.setattr(api_route, "_bikeways", lambda: {})
    assert api_route.bikeway_share(_trip(), {}, [(39.7, -105.0), (39.71, -105.0)]) == 0.0

    monkeypatch.setattr(api_route, "_bikeways",
                        lambda: {16983484: ("none", "designated")})
    assert api_route.bikeway_share(_trip(), {}, [(39.7, -105.0), (39.71, -105.0)]) == 1.0


def test_a_way_listed_as_neither_is_not_the_network(monkeypatch):
    """The sidecar is keyed by way id, so a lookup hit is not itself the
    answer — a row saying "no facility, no network" must not count."""
    edge = _edge(1.0)
    edge["way_id"] = 999
    monkeypatch.setattr(api_route.valhalla, "trace_attributes",
                        lambda *a, **kw: [edge])
    monkeypatch.setattr(api_route, "_bikeways", lambda: {999: ("none", "none")})
    assert api_route.bikeway_share(_trip(), {}, [(39.7, -105.0), (39.71, -105.0)]) == 0.0


def test_the_preference_still_works_with_no_sidecar(monkeypatch):
    """The sidecar is an improvement, not a dependency. A cold container that
    has not synced it yet must still prefer bikeways on the graph's own
    signals rather than scoring every route zero."""
    edge = _edge(1.0, network=1)          # visible to the graph
    edge["way_id"] = 37308939
    monkeypatch.setattr(api_route.valhalla, "trace_attributes",
                        lambda *a, **kw: [edge])
    monkeypatch.setattr(api_route, "_bikeways", lambda: {})
    assert api_route.bikeway_share(_trip(), {}, [(39.7, -105.0), (39.71, -105.0)]) == 1.0


def test_the_detour_finder_and_the_scorer_agree_on_the_network(monkeypatch):
    """They must read the same predicate. A detour manufactured around a
    stretch the scorer then counts as bikeway is a route that argues with
    itself, and the two used to carry separate copies of the rule."""
    designated = _edge(0.5, begin=0, end=2)
    designated["way_id"] = 16983484
    plain = _edge(0.6, begin=2, end=6)
    plain["way_id"] = 555
    monkeypatch.setattr(api_route.valhalla, "trace_attributes",
                        lambda *a, **kw: [designated, plain])
    monkeypatch.setattr(api_route, "_bikeways",
                        lambda: {16983484: ("none", "designated")})
    seen = {}

    def fake_route(points, opts, **kw):
        seen.update(kw)
        return {"trip": _trip()}

    monkeypatch.setattr(api_route.valhalla, "route", fake_route)
    shape = [(39.70 + i * 0.001, -105.0) for i in range(8)]
    prof = load().valhalla.profile("safe")
    api_route._bikeway_detour_candidate([(39.7, -105.0), (39.71, -105.0)],
                                        prof, _trip(), shape)
    # The designated stretch is on the network, so the run to route around is
    # the 600 m of plain street (shape indices 2..6), not the whole trip.
    assert seen["exclude_locations"] == [shape[4]]
