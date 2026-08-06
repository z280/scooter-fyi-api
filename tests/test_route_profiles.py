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


# --- profiles ---------------------------------------------------------------

def test_all_four_profiles_are_configured():
    cfg = load().valhalla
    assert {p.key for p in cfg.profiles} == {"safe", "range", "shade", "express"}
    assert cfg.default_profile == "safe"


def test_only_the_reranked_profiles_ask_for_alternates():
    """`safe` and `express` must not pay for alternates at all.

    Shade stays opt-in (§2C): a rider asking for `express` should never be
    steered toward tree cover. `range` now also ranks its alternates, but on
    climb rather than canopy -- Valhalla's `use_hills` is inert on this graph,
    so the flattest route has to be chosen outside it.
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
        else:
            assert p.rerank_by_shade is False
            assert p.rerank_by_elevation is False
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
