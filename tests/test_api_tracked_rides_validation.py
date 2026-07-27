"""Tests for src/api_tracked_rides.py's pure response-shaping logic
(_row_to_ride's anti-fraud GBFS redaction is the highest-stakes piece —
tested directly, no DB needed) plus the request-validation guards that
follow the same fake-cursor idiom as tests/test_api_rides_validation.py."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_tracked_rides
from src.accounts import SessionUser, require_session
from src.polyline import encode as encode_polyline

_RIDE_ID = uuid.uuid4()
_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    supporter=False, expires_at=_NOW, sliding=True, method="google", token_sha256="x",
)


def _row(
    *, reported: bool = False, gbfs_reappeared: bool = False,
    path_polyline: str = "",
) -> tuple:
    return (
        _RIDE_ID, "watching", _NOW, 39.74, -104.98, _NOW,
        _NOW if gbfs_reappeared else None,           # gbfs_left_feed_at
        _NOW if gbfs_reappeared else None,            # gbfs_reappeared_at
        39.75 if gbfs_reappeared else None,           # gbfs_end_lat
        -104.99 if gbfs_reappeared else None,         # gbfs_end_lon
        42 if gbfs_reappeared else None,              # gbfs_end_battery_percent
        _NOW if reported else None,                   # user_reported_ended_at
        39.751 if reported else None,                 # end_lat
        -104.991 if reported else None,               # end_lon
        78.2 if reported else None,                   # reported_battery_percent
        350 if reported else None,                    # total_cost_cents
        {}, path_polyline, "aaaa000000000000", _NOW, _NOW,
    )


# ---------- _row_to_ride: anti-fraud GBFS redaction -------------------------

def test_gbfs_fields_are_redacted_until_reported():
    ride = api_tracked_rides._row_to_ride(_row(reported=False, gbfs_reappeared=True))
    assert ride["gbfs_reappeared_at"] is None
    assert ride["gbfs_end_lat"] is None
    assert ride["gbfs_end_lon"] is None
    assert ride["gbfs_end_battery_percent"] is None


def test_gbfs_fields_are_visible_once_reported():
    ride = api_tracked_rides._row_to_ride(_row(reported=True, gbfs_reappeared=True))
    assert ride["gbfs_reappeared_at"] is not None
    assert ride["gbfs_end_lat"] == 39.75
    assert ride["gbfs_end_battery_percent"] == 42


def test_reported_end_fields_are_never_redacted():
    """Only the GBFS side is gated on the report — the rider's own report
    is always visible in their own response."""
    ride = api_tracked_rides._row_to_ride(_row(reported=True))
    assert ride["end_lat"] == 39.751
    assert ride["reported_battery_percent"] == 78.2


def test_path_geojson_decodes_the_polyline():
    points = [(39.74, -104.98), (39.741, -104.981)]
    ride = api_tracked_rides._row_to_ride(_row(path_polyline=encode_polyline(points)))
    assert ride["path_geojson"]["type"] == "LineString"
    assert len(ride["path_geojson"]["coordinates"]) == 2


def test_path_geojson_is_none_for_a_ride_with_no_waypoints_yet():
    ride = api_tracked_rides._row_to_ride(_row(path_polyline=""))
    assert ride["path_geojson"] is None


def test_path_geojson_omitted_when_requested():
    ride = api_tracked_rides._row_to_ride(_row(), path_geojson=False)
    assert "path_geojson" not in ride
    assert "path_polyline" not in ride


# ---------- request validation (fake cursor) --------------------------------

class _FakeCursor:
    def __init__(self, fetches):
        self._fetches = list(fetches)
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._fetches.pop(0)

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, fetches):
        self._fetches = fetches
        self.cur: _FakeCursor | None = None

    def cursor(self):
        self.cur = _FakeCursor(self._fetches)
        return self.cur

    def commit(self):
        pass


def _app():
    app = FastAPI()
    app.include_router(api_tracked_rides.router)
    app.dependency_overrides[require_session] = lambda: _USER
    return app


def _client(monkeypatch, fetches):
    conn = _FakeConn(fetches)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_tracked_rides, "connection", _fake_connection)
    monkeypatch.setattr(api_tracked_rides, "enforce", lambda cur, **kw: None)
    return TestClient(_app()), conn


def test_start_ride_404_on_unknown_vehicle(monkeypatch):
    c, _ = _client(monkeypatch, fetches=[None])  # device_state lookup -> not found
    r = c.post("/api/v1/tracked-rides", json={
        "vehicle_identifier": "aaaa000000000000", "start_lat": 39.74, "start_lon": -104.98,
    })
    assert r.status_code == 404


def test_start_ride_409_when_already_active(monkeypatch):
    c, _ = _client(monkeypatch, fetches=[(1,), (1,)])  # device found, active ride found
    r = c.post("/api/v1/tracked-rides", json={
        "vehicle_identifier": "aaaa000000000000", "start_lat": 39.74, "start_lon": -104.98,
    })
    assert r.status_code == 409


def test_start_ride_rejects_bad_vehicle_identifier_shape():
    r = TestClient(_app()).post("/api/v1/tracked-rides", json={
        "vehicle_identifier": "not-16-hex", "start_lat": 39.74, "start_lon": -104.98,
    })
    assert r.status_code == 422


def test_end_ride_requires_tz_aware_timestamp():
    r = TestClient(_app()).patch(f"/api/v1/tracked-rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-01T12:00:00", "end_lat": 39.75, "end_lon": -104.99,
    })
    assert r.status_code == 400
    assert "ended_at" in r.json()["detail"]


def test_end_ride_404_when_not_found(monkeypatch):
    c, _ = _client(monkeypatch, fetches=[None])
    r = c.patch(f"/api/v1/tracked-rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-01T12:00:00Z", "end_lat": 39.75, "end_lon": -104.99,
    })
    assert r.status_code == 404


def test_end_ride_409_when_already_reported(monkeypatch):
    # (user_reported_ended_at, vehicle_identifier, gbfs_reappeared_at,
    # gbfs_end_lat, gbfs_end_lon) — already reported.
    c, _ = _client(monkeypatch, fetches=[(_NOW, "aaaa000000000000", None, None, None)])
    r = c.patch(f"/api/v1/tracked-rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-01T12:00:00Z", "end_lat": 39.75, "end_lon": -104.99,
    })
    assert r.status_code == 409


def test_end_ride_credits_waypoint_points(monkeypatch):
    # initial SELECT (not ended, no gbfs data), UPDATE (no fetch),
    # waypoint COUNT, credit_points INSERT...RETURNING, final SELECT.
    fetches = [
        (None, "aaaa000000000000", None, None, None),
        (3,),
        (77, _NOW),
        _row(),
    ]
    c, conn = _client(monkeypatch, fetches)
    r = c.patch(f"/api/v1/tracked-rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-01T12:00:00Z", "end_lat": 39.75, "end_lon": -104.99,
    })
    assert r.status_code == 200, r.text
    points_insert = next(c for c in conn.cur.executed if c[0].startswith("INSERT INTO user_points"))
    assert points_insert[1][1] == "waypoint"
    assert points_insert[1][2] == 6  # 2 points * 3 waypoints


def test_end_ride_no_waypoints_and_no_gbfs_data_credits_nothing(monkeypatch):
    fetches = [
        (None, "aaaa000000000000", None, None, None),
        (0,),  # zero waypoints -> credit_waypoint_points no-ops without a query
        _row(),
    ]
    c, conn = _client(monkeypatch, fetches)
    r = c.patch(f"/api/v1/tracked-rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-01T12:00:00Z", "end_lat": 39.75, "end_lon": -104.99,
    })
    assert r.status_code == 200, r.text
    assert not any(c[0].startswith("INSERT INTO user_points") for c in conn.cur.executed)


def test_waypoint_requires_tz_aware_timestamp():
    r = TestClient(_app()).post(f"/api/v1/tracked-rides/{_RIDE_ID}/waypoints", json={
        "waypoint_at": "2026-07-01T12:00:00", "lat": 39.74, "lon": -104.98,
    })
    assert r.status_code == 400


def test_waypoint_409_when_ride_not_found(monkeypatch):
    c, _ = _client(monkeypatch, fetches=[None])
    r = c.post(f"/api/v1/tracked-rides/{_RIDE_ID}/waypoints", json={
        "waypoint_at": "2026-07-01T12:00:00Z", "lat": 39.74, "lon": -104.98,
    })
    assert r.status_code == 404


def test_waypoint_409_when_ride_already_ended(monkeypatch):
    # (user_reported_ended_at, gbfs_reappeared_at, watch_expires_at) — ended.
    c, _ = _client(monkeypatch, fetches=[(_NOW, None, _NOW)])
    r = c.post(f"/api/v1/tracked-rides/{_RIDE_ID}/waypoints", json={
        "waypoint_at": "2026-07-01T12:00:00Z", "lat": 39.74, "lon": -104.98,
    })
    assert r.status_code == 409


def test_waypoint_409_when_watch_expired(monkeypatch):
    from datetime import timedelta
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    c, _ = _client(monkeypatch, fetches=[(None, None, past)])
    r = c.post(f"/api/v1/tracked-rides/{_RIDE_ID}/waypoints", json={
        "waypoint_at": "2026-07-01T12:00:00Z", "lat": 39.74, "lon": -104.98,
    })
    assert r.status_code == 409


def test_active_route_registered_before_ride_id_route():
    """Regression guard: /active must resolve to active_tracked_ride, not
    get_tracked_ride(ride_id='active') — this only holds if the route is
    registered first, since Starlette matches path routes in order."""
    paths = [r.path for r in api_tracked_rides.router.routes]
    assert paths.index("/api/v1/tracked-rides/active") < paths.index("/api/v1/tracked-rides/{ride_id}")


def test_bad_ride_id_returns_400_not_500():
    r = TestClient(_app()).get("/api/v1/tracked-rides/not-a-uuid")
    assert r.status_code == 400


def test_list_rejects_bad_status_filter():
    r = TestClient(_app()).get("/api/v1/tracked-rides", params={"status": "bogus"})
    assert r.status_code == 422
