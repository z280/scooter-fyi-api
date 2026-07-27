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


def test_only_shade_is_reranked_and_asks_for_alternates():
    """The other three must not pay for alternates or shade scoring.

    This is the guard for the whole point of §2C: shade is opt-in, so a rider
    asking for `express` should never be steered toward tree cover.
    """
    cfg = load().valhalla
    for p in cfg.profiles:
        if p.key == "shade":
            assert p.rerank_by_shade is True
            assert p.alternates >= 2
        else:
            assert p.rerank_by_shade is False
            assert p.alternates == 0


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


def test_unmeasured_ways_score_as_unshaded(monkeypatch):
    """Arterials and cycleways are absent from the table (only residentials are
    measured) and must count as 0, not be skipped from the denominator."""
    monkeypatch.setattr(
        api_route.valhalla, "trip_shape", lambda trip: [(1.0, 1.0), (2.0, 2.0)])
    monkeypatch.setattr(
        api_route.valhalla, "trace_attributes",
        lambda *a, **kw: [{"way_id": 1, "length": 1.0}, {"way_id": 99, "length": 3.0}])
    api_route._CANOPY = {1: 1.0}

    score = api_route.shade_score(_trip(), {})
    assert score == pytest.approx(0.25)


# --- elevation ---------------------------------------------------------------

def test_elevation_gain_sums_only_ascent():
    trip = _trip(elevation=[1600.0, 1610.0, 1605.0, 1615.0])
    # +10, -5, +10 -> 20m of climbing.
    assert valhalla.elevation_gain_meters(trip) == pytest.approx(20.0)


def test_elevation_gain_is_none_without_elevation_data():
    """A graph built without build_elevation must report None, not 0.0 — the
    battery model treats those very differently."""
    assert valhalla.elevation_gain_meters(_trip()) is None
