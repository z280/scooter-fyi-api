"""GET /api/v1/route/walk — the leg before the ride.

Choosing a scooter used to tell the rider it was 300 m away, draw a dashed
straight line, and then hand them off to Google or Apple Maps to actually get
there. That is the one moment the app has a router of its own and was not
using it, and it is also the moment a rider is standing on a pavement
deciding whether to trust the app at all.

What is locked in here is mostly about the walk NOT being a bicycle profile:
it uses Valhalla's pedestrian costing, and it must not inherit the High Injury
Network exclusions, which are a sensible thing to avoid riding along and a
nonsense thing to avoid walking along.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_route, ratelimit
from src.polyline import encode as encode_polyline


_PTS = [(39.7392, -104.9903), (39.7400, -104.9900), (39.7431, -104.9880)]


def _trip(*, maneuvers=None):
    return {
        "legs": [{
            "shape": encode_polyline(_PTS),
            "maneuvers": maneuvers if maneuvers is not None else [
                {"instruction": "Walk north on Bannock Street.",
                 "begin_shape_index": 0, "end_shape_index": 1},
                {"instruction": "You have arrived at your destination.",
                 "begin_shape_index": 1, "end_shape_index": 2},
            ],
        }],
        "summary": {"length": 0.571, "time": 408},
    }


def _app():
    app = FastAPI()
    app.include_router(api_route.router)
    return app


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **kw):
        pass

    def fetchone(self):
        return None


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def commit(self):
        pass


def _install(monkeypatch):
    def fake_enforce(cur, **kw):
        inspect.signature(ratelimit.enforce).bind(cur, **kw)

    @contextmanager
    def fake_connection():
        yield _FakeConn()

    monkeypatch.setattr(api_route, "enforce", fake_enforce)
    monkeypatch.setattr(api_route, "connection", fake_connection)


def _capture(monkeypatch):
    """Record what the endpoint asks Valhalla for."""
    seen: dict = {}

    def fake_route(points, costing_options, **kw):
        seen["points"] = points
        seen["costing_options"] = costing_options
        seen.update(kw)
        return {"trip": _trip()}

    monkeypatch.setattr(api_route.valhalla, "route", fake_route)
    return seen


_QS = {"from": "39.7392,-104.9903", "to": "39.7431,-104.9880"}


def test_it_walks_rather_than_cycling(monkeypatch):
    """The whole point. A bicycle costing would route the rider around the
    High Injury Network — streets that are dangerous to ride down and
    perfectly ordinary to walk along — and would refuse legs that carry no
    cycling permission at all, like a footpath through a park."""
    _install(monkeypatch)
    seen = _capture(monkeypatch)
    TestClient(_app()).get("/api/v1/route/walk", params=_QS)
    assert seen["costing"] == "pedestrian"


def test_it_does_not_pay_for_elevation(monkeypatch):
    """Elevation drives the battery model, and a walk has no battery."""
    _install(monkeypatch)
    seen = _capture(monkeypatch)
    TestClient(_app()).get("/api/v1/route/walk", params=_QS)
    assert seen["with_elevation"] is False


def test_it_returns_a_drawable_line_and_a_believable_eta(monkeypatch):
    _install(monkeypatch)
    _capture(monkeypatch)
    body = TestClient(_app()).get("/api/v1/route/walk", params=_QS).json()
    assert body["type"] == "Feature"
    assert body["geometry"]["type"] == "LineString"
    assert len(body["geometry"]["coordinates"]) == len(_PTS)
    assert body["properties"]["mode"] == "walk"
    assert body["properties"]["distance_meters"] == 571.0
    assert body["properties"]["duration_seconds"] == 408


def test_walking_pace_is_not_a_commuter_pace():
    """Somebody crossing a block to a scooter, phone in hand, checking the
    plate against a photo, is not walking at Valhalla's 5.1 km/h default. An
    optimistic ETA on a two-minute walk is the kind of small lie that makes a
    rider stop believing the other numbers."""
    assert api_route.WALK_COSTING_OPTIONS["walking_speed"] < 5.1


def test_turn_by_turn_is_opt_in(monkeypatch):
    """Same discipline as the ride route: the map line needs no maneuvers and
    they roughly double the response."""
    _install(monkeypatch)
    _capture(monkeypatch)
    client = TestClient(_app())
    assert "maneuvers" not in client.get("/api/v1/route/walk", params=_QS).json()["properties"]
    with_turns = client.get("/api/v1/route/walk",
                            params={**_QS, "maneuvers": "true"}).json()
    assert len(with_turns["properties"]["maneuvers"]) == 2


def test_a_point_outside_the_graph_is_refused_not_clamped(monkeypatch):
    _install(monkeypatch)
    _capture(monkeypatch)
    r = TestClient(_app()).get("/api/v1/route/walk",
                               params={"from": "0,0", "to": "39.7431,-104.9880"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "out_of_coverage"


def test_an_unroutable_walk_says_so_rather_than_500ing(monkeypatch):
    _install(monkeypatch)

    def boom(*a, **kw):
        # Valhalla's own "no path" code — the property the endpoint reads.
        code = sorted(api_route.valhalla.ERR_NO_PATH)[0]
        raise api_route.valhalla.ValhallaError("no path", code=code)

    monkeypatch.setattr(api_route.valhalla, "route", boom)
    r = TestClient(_app()).get("/api/v1/route/walk", params=_QS)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "no_walking_route"


def test_walking_is_not_offered_as_a_ride_profile(monkeypatch):
    """/route/profiles means "how do you want to RIDE". A walk in that list
    would offer a scooter route the rider cannot take on foot, and vice
    versa."""
    _install(monkeypatch)
    body = TestClient(_app()).get("/api/v1/route/profiles").json()
    assert all(p["key"] != "walk" for p in body["profiles"])
