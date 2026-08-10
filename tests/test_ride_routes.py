"""POST /api/v1/ride-routes (PLAN_RIDE_MODE_API.md phase A3, sql/052).

Same fake-cursor idiom as tests/test_ride_session_fields.py: a monkeypatched
connection/cursor, assertions on the SQL that gets built, and a bare
FastAPI() mounting the single router with require_session overridden.

One deliberate difference from that file's `_FakeConn`: `cursor()` here
hands out a fresh `_FakeCursor` wrapping the SAME underlying fetchone queue
and executed-statement log on every call, rather than a private copy, so
that tests/test_two_posts_for_the_same_ride_both_succeed_and_both_persist
below can drive TWO separate HTTP requests (two separate `with
connection()` / `with conn.cursor()` cycles) against one continuous fixture
without either request seeing the other's leftovers rewound.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src import api_ride_routes
from src.accounts import SessionUser, require_session
from src.config import load
from src.polyline import encode as encode_polyline
from src.ride_limits import MAX_RIDE_DISTANCE_METERS

_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    expires_at=_NOW, sliding=True, method="google", token_sha256="x",
)
_RIDE_ID = uuid.uuid4()

# Well inside config.json's graph_bbox (west -105.06, south 39.65,
# east -104.88, north 39.79).
_ORIGIN = (39.74, -104.99)
_DEST = (39.70, -104.95)
_POLYLINE = encode_polyline([_ORIGIN, _DEST])
_ONE_POINT_POLYLINE = encode_polyline([_ORIGIN])

# South of the graph's clip — inside the app's wider Denver bounds, outside
# the routing graph, same idiom tests/test_route_profiles.py uses.
_OUT_OF_COVERAGE = (39.10, -104.99)


# ---------------------------------------------------------------------------
# Fake cursor / connection
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, fetchones: list, executed: list):
        self._ones = fetchones      # SHARED across every cursor() call — see module docstring
        self.executed = executed    # SHARED likewise

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._ones.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, fetchones):
        self._fetchones = list(fetchones)
        self.executed: list[tuple[str, tuple]] = []
        self.cur: _FakeCursor | None = None

    def cursor(self):
        self.cur = _FakeCursor(self._fetchones, self.executed)
        return self.cur

    def commit(self):
        pass


def _app():
    app = FastAPI()
    app.include_router(api_ride_routes.router)
    app.dependency_overrides[require_session] = lambda: _USER
    return app


def _client(monkeypatch, fetchones=(), enforce_spy=None):
    conn = _FakeConn(fetchones)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_ride_routes, "connection", _fake_connection)
    monkeypatch.setattr(
        api_ride_routes, "enforce",
        enforce_spy if enforce_spy is not None else (lambda cur, **kw: None),
    )
    return TestClient(_app()), conn


def _payload(**overrides) -> dict:
    payload = {
        "tracked_ride_id": None,
        "profile": "safe",
        "origin": list(_ORIGIN),
        "destination": list(_DEST),
        "route_polyline": _POLYLINE,
        "distance_meters": 1200.0,
        "duration_seconds": 300.0,
        "battery_percent_estimate": 4.5,
    }
    payload.update(overrides)
    return payload


def _post(client, **overrides):
    return client.post("/api/v1/ride-routes", json=_payload(**overrides))


def _insert_call(conn):
    return next(e for e in conn.cur.executed if e[0].startswith("INSERT INTO ride_routes"))


# ---------------------------------------------------------------------------
# happy path — null tracked_ride_id (the normal wizard flow)
# ---------------------------------------------------------------------------

def test_null_tracked_ride_id_happy_path(monkeypatch):
    """Screen 4 precedes ride start, so tracked_ride_id is null in the
    normal flow — the survey later links the row to a ride."""
    c, conn = _client(monkeypatch, fetchones=[(uuid.uuid4(),)])
    r = _post(c)
    assert r.status_code == 200, r.text

    _, params = _insert_call(conn)
    assert params[0] is None                    # tracked_ride_id
    assert params[1] == _USER.account_id
    assert params[2] == "safe"
    # No ownership probe was issued — nothing to check against.
    assert not any("FROM tracked_rides" in e[0] for e in conn.cur.executed)


def test_response_shape_is_exactly_ride_route_id(monkeypatch):
    new_id = uuid.uuid4()
    c, _ = _client(monkeypatch, fetchones=[(new_id,)])
    r = _post(c)
    assert r.status_code == 200, r.text
    assert r.json() == {"ride_route_id": str(new_id)}


# ---------------------------------------------------------------------------
# tracked_ride_id: null-or-caller-owned
# ---------------------------------------------------------------------------

def test_owned_tracked_ride_id_is_accepted(monkeypatch):
    new_id = uuid.uuid4()
    c, conn = _client(monkeypatch, fetchones=[(1,), (new_id,)])
    r = _post(c, tracked_ride_id=str(_RIDE_ID))
    assert r.status_code == 200, r.text
    assert r.json()["ride_route_id"] == str(new_id)

    own_sql, own_params = next(
        e for e in conn.cur.executed if "SELECT 1 FROM tracked_rides" in e[0])
    assert own_params == (str(_RIDE_ID), _USER.account_id)

    _, insert_params = _insert_call(conn)
    assert insert_params[0] == str(_RIDE_ID)


def test_non_owned_tracked_ride_id_is_404_not_403(monkeypatch):
    """The FK alone would accept any account's ride id — this is the
    no-existence-oracle idiom every tracked-rides sub-resource uses
    (src/api_ride_screenshots.py)."""
    c, conn = _client(monkeypatch, fetchones=[None])
    r = _post(c, tracked_ride_id=str(_RIDE_ID))
    assert r.status_code == 404
    assert not any(e[0].startswith("INSERT INTO ride_routes") for e in conn.cur.executed)


def test_malformed_tracked_ride_id_is_400():
    r = TestClient(_app()).post(
        "/api/v1/ride-routes", json=_payload(tracked_ride_id="not-a-uuid"))
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# multi-row-per-ride is intended
# ---------------------------------------------------------------------------

def test_two_posts_for_the_same_ride_both_succeed_and_both_persist(monkeypatch):
    """No uniqueness on tracked_ride_id: the S8 New-Destination loop
    legitimately creates a second row for the same ride."""
    first_id, second_id = uuid.uuid4(), uuid.uuid4()
    c, conn = _client(monkeypatch, fetchones=[(1,), (first_id,), (1,), (second_id,)])

    r1 = _post(c, tracked_ride_id=str(_RIDE_ID))
    r2 = _post(c, tracked_ride_id=str(_RIDE_ID),
               route_polyline=encode_polyline([_DEST, _ORIGIN]))
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["ride_route_id"] == str(first_id)
    assert r2.json()["ride_route_id"] == str(second_id)
    assert r1.json()["ride_route_id"] != r2.json()["ride_route_id"]

    inserts = [e for e in conn.cur.executed if e[0].startswith("INSERT INTO ride_routes")]
    assert len(inserts) == 2, "both POSTs must have persisted a row"
    assert inserts[0][1][0] == str(_RIDE_ID)
    assert inserts[1][1][0] == str(_RIDE_ID)

    owner_probes = [e for e in conn.cur.executed if "SELECT 1 FROM tracked_rides" in e[0]]
    assert len(owner_probes) == 2, "each POST re-checks ownership independently"


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------

def test_unknown_profile_is_400():
    r = TestClient(_app()).post(
        "/api/v1/ride-routes", json=_payload(profile="teleport"))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "unknown_profile"
    assert set(r.json()["detail"]["profiles"]) == {"safe", "range", "shade",
                                                   "express", "night"}


@pytest.mark.parametrize("profile", ["safe", "range", "shade", "express", "night"])
def test_every_configured_profile_is_accepted(monkeypatch, profile):
    c, conn = _client(monkeypatch, fetchones=[(uuid.uuid4(),)])
    assert _post(c, profile=profile).status_code == 200
    assert _insert_call(conn)[1][2] == profile


def test_unknown_profile_is_rejected_before_a_connection_is_taken(monkeypatch):
    """Same rule src/api_tracked_rides.py:_serialize_ride_options follows: a
    client bug must not cost a pooled connection."""
    @contextmanager
    def _explode():
        raise AssertionError("the handler took a connection before validating the profile")
        yield  # pragma: no cover

    monkeypatch.setattr(api_ride_routes, "connection", _explode)
    r = TestClient(_app()).post(
        "/api/v1/ride-routes", json=_payload(profile="teleport"))
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# polyline: must decode to >= 2 points
# ---------------------------------------------------------------------------

def test_polyline_decoding_to_fewer_than_2_points_is_400():
    r = TestClient(_app()).post(
        "/api/v1/ride-routes", json=_payload(route_polyline=_ONE_POINT_POLYLINE))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_polyline"


def test_corrupt_polyline_is_400():
    r = TestClient(_app()).post(
        "/api/v1/ride-routes", json=_payload(route_polyline="!!!not-a-polyline!!!"))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_polyline"


def test_two_point_polyline_is_accepted(monkeypatch):
    c, _ = _client(monkeypatch, fetchones=[(uuid.uuid4(),)])
    assert _post(c, route_polyline=_POLYLINE).status_code == 200


# ---------------------------------------------------------------------------
# endpoints must fall inside the routing graph's bbox
# ---------------------------------------------------------------------------

def test_origin_out_of_coverage_is_400():
    r = TestClient(_app()).post(
        "/api/v1/ride-routes", json=_payload(origin=list(_OUT_OF_COVERAGE)))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "out_of_coverage"
    assert r.json()["detail"]["detail"].startswith("origin ")
    assert r.json()["detail"]["graph_bbox"] == load().valhalla.bbox


def test_destination_out_of_coverage_is_400():
    r = TestClient(_app()).post(
        "/api/v1/ride-routes", json=_payload(destination=list(_OUT_OF_COVERAGE)))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "out_of_coverage"
    assert r.json()["detail"]["detail"].startswith("destination ")


# ---------------------------------------------------------------------------
# client-claimed metric bounds -> 422
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [-1, -0.1])
def test_distance_meters_below_zero_is_422(value):
    r = TestClient(_app()).post(
        "/api/v1/ride-routes", json=_payload(distance_meters=value))
    assert r.status_code == 422


def test_distance_meters_above_the_ride_cap_is_422():
    r = TestClient(_app()).post(
        "/api/v1/ride-routes",
        json=_payload(distance_meters=MAX_RIDE_DISTANCE_METERS + 0.1))
    assert r.status_code == 422


@pytest.mark.parametrize("value", [0, 80_000])
def test_distance_meters_at_the_bounds_is_accepted(monkeypatch, value):
    assert MAX_RIDE_DISTANCE_METERS == 80_000.0
    c, conn = _client(monkeypatch, fetchones=[(uuid.uuid4(),)])
    assert _post(c, distance_meters=value).status_code == 200
    assert _insert_call(conn)[1][8] == value


@pytest.mark.parametrize("value", [-1, -0.1])
def test_duration_seconds_below_zero_is_422(value):
    r = TestClient(_app()).post(
        "/api/v1/ride-routes", json=_payload(duration_seconds=value))
    assert r.status_code == 422


def test_duration_seconds_above_the_3h_watch_window_is_422():
    r = TestClient(_app()).post(
        "/api/v1/ride-routes",
        json=_payload(duration_seconds=api_ride_routes.MAX_ROUTE_DURATION_SECONDS + 1))
    assert r.status_code == 422


@pytest.mark.parametrize("value", [0, 10_800])
def test_duration_seconds_at_the_bounds_is_accepted(monkeypatch, value):
    assert api_ride_routes.MAX_ROUTE_DURATION_SECONDS == 10_800
    c, conn = _client(monkeypatch, fetchones=[(uuid.uuid4(),)])
    assert _post(c, duration_seconds=value).status_code == 200
    assert _insert_call(conn)[1][9] == value


@pytest.mark.parametrize("value", [-1, -0.1, 100.1, 101])
def test_battery_percent_estimate_outside_0_100_is_422(value):
    r = TestClient(_app()).post(
        "/api/v1/ride-routes", json=_payload(battery_percent_estimate=value))
    assert r.status_code == 422


@pytest.mark.parametrize("value", [0, 50, 100])
def test_battery_percent_estimate_inside_0_100_is_accepted(monkeypatch, value):
    c, conn = _client(monkeypatch, fetchones=[(uuid.uuid4(),)])
    assert _post(c, battery_percent_estimate=value).status_code == 200
    assert _insert_call(conn)[1][10] == value


def test_battery_percent_estimate_omitted_stays_null(monkeypatch):
    payload = _payload()
    del payload["battery_percent_estimate"]
    c, conn = _client(monkeypatch, fetchones=[(uuid.uuid4(),)])
    r = c.post("/api/v1/ride-routes", json=payload)
    assert r.status_code == 200, r.text
    assert _insert_call(conn)[1][10] is None


# ---------------------------------------------------------------------------
# rate limit: bucket ride_route_account, 30/h, account-scoped
# ---------------------------------------------------------------------------

def test_rate_limit_bucket_is_invoked_correctly(monkeypatch):
    calls = []

    def _spy(cur, **kw):
        calls.append(kw)

    c, _ = _client(monkeypatch, fetchones=[(uuid.uuid4(),)], enforce_spy=_spy)
    assert _post(c).status_code == 200
    assert len(calls) == 1
    assert calls[0] == {
        "bucket": "ride_route_account",
        "key": str(_USER.account_id),
        "limit": 30,
        "window_seconds": 3600,
    }


def test_rate_limit_is_account_scoped(monkeypatch):
    """Keyed on the caller's account id, the same pattern every other
    account-scoped bucket in this codebase uses
    (tracked_ride_start_account, ride_screenshot_account,
    track_donation_account)."""
    calls = []
    c, _ = _client(monkeypatch, fetchones=[(uuid.uuid4(),)],
                    enforce_spy=lambda cur, **kw: calls.append(kw))
    assert _post(c).status_code == 200
    assert calls[0]["key"] == str(_USER.account_id)


def test_rate_limit_denial_prevents_the_insert(monkeypatch):
    def _deny(cur, **kw):
        raise HTTPException(429, "rate limited")

    c, conn = _client(monkeypatch, fetchones=[(uuid.uuid4(),)], enforce_spy=_deny)
    r = _post(c)
    assert r.status_code == 429
    assert not any(e[0].startswith("INSERT INTO ride_routes") for e in conn.cur.executed)
