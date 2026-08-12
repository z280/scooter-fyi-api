"""GET /api/v1/route/options — every genuinely different route, once each.

WHAT THIS FIXES, measured against production before it was written:

    safe     2456 m  601 s  164 pts  shape A
    range    2456 m  601 s  164 pts  shape A
    shade    2456 m  601 s  164 pts  shape A
    night    2370 m  558 s  123 pts  shape B
    express  2370 m  421 s  123 pts  shape B

Five options, two roads. And `night` and `express` quoted 9 minutes and 7
minutes for a BYTE-IDENTICAL shape — a costing artefact, not anything a rider
would experience. An app that says the same road takes two different lengths
of time has told the rider its numbers are decorative.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_route, ratelimit
from src.polyline import encode as encode_polyline


# Two roads, the way production returns them.
SHAPE_A = [(39.7392, -104.9903), (39.7380, -104.9880), (39.7319, -104.9721)]
SHAPE_B = [(39.7392, -104.9903), (39.7350, -104.9800), (39.7319, -104.9721)]

# profile key -> (shape, metres, seconds)
LIVE = {
    "safe":    (SHAPE_A, 2.456, 601.3),
    "range":   (SHAPE_A, 2.456, 601.3),
    "shade":   (SHAPE_A, 2.456, 601.3),
    "night":   (SHAPE_B, 2.370, 558.5),
    "express": (SHAPE_B, 2.370, 421.2),
}


class _FakeCursor:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a, **kw): pass
    def fetchone(self): return None


class _FakeConn:
    def cursor(self): return _FakeCursor()
    def commit(self): pass


def _app():
    app = FastAPI()
    app.include_router(api_route.router)
    return app


@pytest.fixture
def client(monkeypatch):
    def fake_enforce(cur, **kw):
        inspect.signature(ratelimit.enforce).bind(cur, **kw)

    @contextmanager
    def fake_connection():
        yield _FakeConn()

    monkeypatch.setattr(api_route, "enforce", fake_enforce)
    monkeypatch.setattr(api_route, "connection", fake_connection)

    def fake_route(points, costing_options, **kw):
        # Identify the profile by the costing options object it was built from.
        for prof in api_route.load().valhalla.profiles:
            if prof.costing_options is costing_options:
                shape, km, secs = LIVE[prof.key]
                return {"trip": {"legs": [{"shape": encode_polyline(shape)}],
                                 "summary": {"length": km, "time": secs}}}
        raise AssertionError("unknown costing options")

    monkeypatch.setattr(api_route.valhalla, "route", fake_route)
    monkeypatch.setattr(api_route.battery_model, "estimate_burn_percent",
                        lambda **kw: {"percent": 8.0, "percent_low": 2.0,
                                      "percent_high": 14.0, "source": "regression"})
    return TestClient(_app())


QS = {"from": "39.7392,-104.9903", "to": "39.7319,-104.9721"}


def test_five_profiles_two_roads_two_options(client):
    """Rule 1. The rider is offered choices, not synonyms."""
    body = client.get("/api/v1/route/options", params=QS).json()
    assert len(body["options"]) == 2


def test_the_same_road_never_shows_two_durations(client):
    """Rule 2. night and express are the same shape; only one survives, so
    there is no second number to contradict the first."""
    body = client.get("/api/v1/route/options", params=QS).json()
    by_distance = {o["distance_meters"]: o for o in body["options"]}
    assert sorted(by_distance) == [2370.0, 2456.0]
    # Every option carries exactly one duration.
    assert all(isinstance(o["duration_seconds"], (int, float)) for o in body["options"])


def test_the_surviving_duration_is_the_conservative_one(client):
    """They are all Valhalla BICYCLE estimates on a bicycle graph, and Denver
    caps these scooters around 15 mph — so the optimistic end is the least
    defensible number in the set. An ETA that runs long costs nothing; one
    that runs short is how somebody misses what they were riding to."""
    body = client.get("/api/v1/route/options", params=QS).json()
    shared = next(o for o in body["options"] if o["distance_meters"] == 2370.0)
    assert shared["duration_seconds"] == 558.5   # night's, not express's 421.2


def test_the_folded_profiles_are_named_not_hidden(client):
    """A rider looking for "the shaded one" can still see that it is this
    one."""
    body = client.get("/api/v1/route/options", params=QS).json()
    first = body["options"][0]
    assert first["key"] == "safe"
    assert {a["key"] for a in first["also"]} == {"range", "shade"}
    shared = next(o for o in body["options"] if o["distance_meters"] == 2370.0)
    assert [a["key"] for a in shared["also"]] == ["express"]


def test_each_option_carries_its_own_line(client):
    """The client draws the chosen route without a second round trip."""
    body = client.get("/api/v1/route/options", params=QS).json()
    for o in body["options"]:
        assert o["geometry"]["type"] == "LineString"
        assert len(o["geometry"]["coordinates"]) >= 2


# --- rule 3: what will be left when I get there -----------------------------

def test_arrival_percentage_not_just_burn(client):
    """The burn is what the model predicts; what the rider wants is what they
    will have left, which they cannot work out in their head in the street."""
    body = client.get("/api/v1/route/options", params={**QS, "battery_percent": 70}).json()
    o = body["options"][0]
    assert o["battery_percent_estimate"] == 8.0
    assert o["arrival_percent"] == 62.0


def test_the_band_is_named_by_what_the_rider_has_left(client):
    """The HIGH burn is the LOW arrival. Naming them by the outcome means
    `arrival_percent_low` is the bad case in both directions."""
    body = client.get("/api/v1/route/options", params={**QS, "battery_percent": 70}).json()
    o = body["options"][0]
    assert o["arrival_percent_low"] == 56.0    # 70 - 14 (worst burn)
    assert o["arrival_percent_high"] == 68.0   # 70 - 2  (best burn)


def test_it_says_when_the_scooter_will_not_make_it(client):
    """Rule 3's whole point."""
    body = client.get("/api/v1/route/options", params={**QS, "battery_percent": 12}).json()
    o = body["options"][0]
    assert o["will_make_it"] is False
    assert o["arrival_percent"] == 4.0


def test_the_verdict_uses_the_pessimistic_end(client):
    """The point of carrying a band is to use it where the answer matters, and
    being stranded is the expensive error.

    20% charge, 2-14% burn: the CENTRAL estimate arrives at 12%, comfortably
    over the 10% reserve, so a verdict read off the point estimate would say
    yes. The bad end arrives at 6%. That is the case this rule exists for."""
    body = client.get("/api/v1/route/options", params={**QS, "battery_percent": 20}).json()
    o = body["options"][0]
    assert o["arrival_percent"] == 12.0        # over the reserve...
    assert o["arrival_percent_low"] == 6.0     # ...but not in the bad case
    assert o["will_make_it"] is False


def test_a_comfortable_charge_is_a_yes(client):
    body = client.get("/api/v1/route/options", params={**QS, "battery_percent": 90}).json()
    assert body["options"][0]["will_make_it"] is True


def test_no_charge_given_means_no_verdict_invented(client):
    """Without knowing what is in the battery there is no honest answer, and a
    cheerful default would be the dishonest one."""
    body = client.get("/api/v1/route/options", params=QS).json()
    o = body["options"][0]
    assert o["arrival_percent"] is None
    assert o["will_make_it"] is None


def test_the_reserve_is_not_zero(client):
    """Arriving on empty stranded the rider for the last block, and Veo will
    not start a vehicle in the low single digits at all."""
    assert api_route.ARRIVAL_RESERVE_PERCENT >= 5


# --- failure behaviour ------------------------------------------------------

def test_one_profile_failing_does_not_fail_the_request(client, monkeypatch):
    """`safe` can legitimately find nothing where `express` does — the High
    Injury Network exclusions are exactly that asymmetry."""
    real = api_route.valhalla.route

    def sometimes(points, costing_options, **kw):
        for prof in api_route.load().valhalla.profiles:
            if prof.costing_options is costing_options and prof.key == "safe":
                raise api_route.valhalla.ValhallaError("no path", code=442)
        return real(points, costing_options, **kw)

    monkeypatch.setattr(api_route.valhalla, "route", sometimes)
    body = client.get("/api/v1/route/options", params=QS).json()
    assert body["profiles_unavailable"] == ["safe"]
    assert len(body["options"]) == 2
    assert body["options"][0]["key"] == "range"


def test_every_profile_failing_is_a_422(client, monkeypatch):
    def never(*a, **kw):
        raise api_route.valhalla.ValhallaError("no path", code=442)

    monkeypatch.setattr(api_route.valhalla, "route", never)
    r = client.get("/api/v1/route/options", params=QS)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "no_route"


def test_out_of_coverage_is_refused(client):
    r = client.get("/api/v1/route/options",
                   params={"from": "0,0", "to": "39.7319,-104.9721"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "out_of_coverage"
