"""Off-feed ride endpoints (src/api_rides.py): `before` validation plus the
start -> waypoints -> end lifecycle.

Uses a fake cursor/connection so the tests exercise real FastAPI request
handling (query parsing, dependency injection, HTTPException status) without
a live Postgres. The end-to-end path against real Postgres is covered by
tests/test_off_feed_rides_lifecycle_pg.py.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_rides
from src.accounts import SessionUser, require_session


class _FakeCursor:
    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def commit(self):
        pass


@contextmanager
def _fake_connection():
    yield _FakeConn()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api_rides, "connection", _fake_connection)
    app = FastAPI()
    app.include_router(api_rides.router)
    app.dependency_overrides[require_session] = lambda: SessionUser(
        account_id=1, email="rider@example.com", scopes=("rider",),
        expires_at=datetime.now(timezone.utc),
        sliding=True, method="google", token_sha256="x",
    )
    return TestClient(app)


def test_before_without_timezone_is_rejected(client):
    r = client.get("/api/v1/rides", params={"before": "2026-06-01T00:00:00"})
    assert r.status_code == 400
    assert "timezone" in r.json()["detail"]


def test_before_with_z_suffix_is_accepted(client):
    r = client.get("/api/v1/rides", params={"before": "2026-06-01T00:00:00Z"})
    assert r.status_code == 200


def test_before_with_explicit_offset_is_accepted(client):
    r = client.get("/api/v1/rides", params={"before": "2026-06-01T00:00:00+00:00"})
    assert r.status_code == 200


def test_before_omitted_is_fine(client):
    r = client.get("/api/v1/rides")
    assert r.status_code == 200


# ---------- lifecycle: start -> waypoints -> end ----------------------------

_RIDE_ID = uuid.uuid4()
_NOW = datetime(2026, 7, 27, 16, 20, tzinfo=timezone.utc)


def _ride_row(
    *, status: str = "active", ended: bool = False,
    distance_m: int | None = None, distance_source: str | None = None,
) -> tuple:
    """Column order must track _RIDE_COLS in src/api_rides.py."""
    return (
        _RIDE_ID, _NOW, _NOW,
        _NOW if ended else None,          # ended_at
        1500 if ended else None,          # duration_s
        distance_m,
        None,                             # est_cost_cents
        None,                             # rate_plan
        True if ended else None,          # started_in_zone
        False if ended else None,         # ended_in_zone
        "" if ended else None,            # polyline
        status, "scooter", "Lime",
        39.74, -104.98,
        39.75 if ended else None,         # end_lat
        -104.99 if ended else None,       # end_lon
        distance_source,
    )


class _SeqCursor:
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


class _SeqConn:
    def __init__(self, fetches):
        self._fetches = fetches
        self.cur: _SeqCursor | None = None

    def cursor(self):
        self.cur = _SeqCursor(self._fetches)
        return self.cur

    def commit(self):
        pass


def _seq_client(monkeypatch, fetches):
    conn = _SeqConn(fetches)

    @contextmanager
    def _conn():
        yield conn

    monkeypatch.setattr(api_rides, "connection", _conn)
    monkeypatch.setattr(api_rides, "enforce", lambda cur, **kw: None)
    app = FastAPI()
    app.include_router(api_rides.router)
    app.dependency_overrides[require_session] = lambda: SessionUser(
        account_id=1, email="rider@example.com", scopes=("rider",),
        expires_at=datetime.now(timezone.utc),
        sliding=True, method="google", token_sha256="x",
    )
    return TestClient(app), conn


def _end_update(conn) -> tuple:
    return next(c for c in conn.cur.executed
                if c[0].startswith("UPDATE rides SET status = 'completed'"))


def test_start_ride_returns_an_active_ride(monkeypatch):
    c, _ = _seq_client(monkeypatch, [_ride_row()])
    r = c.post("/api/v1/rides/start", json={
        "start_lat": 39.74, "start_lon": -104.98,
        "vehicle_kind": "scooter", "operator": "Lime",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["operator"] == "Lime"
    # An active ride has no end yet — these must serialize as null, not crash.
    assert body["ended_at"] is None
    assert body["distance_m"] is None
    assert body["distance_source"] is None


def test_start_ride_409_when_one_is_already_active(monkeypatch):
    """The unique partial index is the arbiter, so the handler has to
    translate its UniqueViolation rather than pre-checking."""
    from psycopg import errors as pg_errors

    class _RaisingCursor(_SeqCursor):
        def execute(self, sql, params=()):
            super().execute(sql, params)
            if sql.lstrip().startswith("INSERT INTO rides"):
                raise pg_errors.UniqueViolation("duplicate")

    conn = _SeqConn([])
    conn.cursor = lambda: setattr(conn, "cur", _RaisingCursor([])) or conn.cur

    @contextmanager
    def _conn():
        yield conn

    monkeypatch.setattr(api_rides, "connection", _conn)
    monkeypatch.setattr(api_rides, "enforce", lambda cur, **kw: None)
    app = FastAPI()
    app.include_router(api_rides.router)
    app.dependency_overrides[require_session] = lambda: SessionUser(
        account_id=1, email="rider@example.com", scopes=("rider",),
        expires_at=datetime.now(timezone.utc),
        sliding=True, method="google", token_sha256="x",
    )
    r = TestClient(app).post("/api/v1/rides/start",
                             json={"start_lat": 39.74, "start_lon": -104.98})
    assert r.status_code == 409


def test_active_ride_is_always_wrapped(monkeypatch):
    c, _ = _seq_client(monkeypatch, [None])
    assert c.get("/api/v1/rides/active").json() == {"active": None}


def test_waypoint_409_when_ride_not_active(monkeypatch):
    c, _ = _seq_client(monkeypatch, [("completed",)])
    r = c.post(f"/api/v1/rides/{_RIDE_ID}/waypoints", json={
        "waypoint_at": "2026-07-27T16:31:00Z", "lat": 39.745, "lon": -104.985,
    })
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "ride_not_active"


def test_waypoint_404_for_someone_elses_ride(monkeypatch):
    c, _ = _seq_client(monkeypatch, [None])
    r = c.post(f"/api/v1/rides/{_RIDE_ID}/waypoints", json={
        "waypoint_at": "2026-07-27T16:31:00Z", "lat": 39.745, "lon": -104.985,
    })
    assert r.status_code == 404


def test_waypoint_requires_tz_aware_timestamp():
    app = FastAPI()
    app.include_router(api_rides.router)
    app.dependency_overrides[require_session] = lambda: SessionUser(
        account_id=1, email="rider@example.com", scopes=("rider",),
        expires_at=datetime.now(timezone.utc),
        sliding=True, method="google", token_sha256="x",
    )
    r = TestClient(app).post(f"/api/v1/rides/{_RIDE_ID}/waypoints", json={
        "waypoint_at": "2026-07-27T16:31:00", "lat": 39.745, "lon": -104.985,
    })
    assert r.status_code == 400


def test_end_ride_409_when_already_completed(monkeypatch):
    c, _ = _seq_client(monkeypatch, [("completed", _NOW, 39.74, -104.98, "waypoints")])
    r = c.patch(f"/api/v1/rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-27T16:45:00Z", "end_lat": 39.75, "end_lon": -104.99,
    })
    assert r.status_code == 409


def test_end_ride_without_waypoints_falls_back_to_straight_line(monkeypatch):
    c, conn = _seq_client(monkeypatch, [
        ("active", _NOW, 39.74, -104.98, None),
        _ride_row(status="completed", ended=True, distance_m=1113,
                  distance_source="straight_line"),
    ])
    r = c.patch(f"/api/v1/rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-27T16:45:00Z", "end_lat": 39.75, "end_lon": -104.98,
    })
    assert r.status_code == 200, r.text
    sql, params = _end_update(conn)
    assert "distance_source = 'straight_line'" in sql
    # 39.74 -> 39.75 at constant longitude is ~1113 m.
    assert any(isinstance(p, int) and 1100 < p < 1120 for p in params)


def test_end_ride_keeps_waypoint_distance(monkeypatch):
    c, conn = _seq_client(monkeypatch, [
        ("active", _NOW, 39.74, -104.98, "waypoints"),
        _ride_row(status="completed", ended=True, distance_m=4321,
                  distance_source="waypoints"),
    ])
    r = c.patch(f"/api/v1/rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-27T16:45:00Z", "end_lat": 39.75, "end_lon": -104.98,
    })
    assert r.status_code == 200, r.text
    sql, _ = _end_update(conn)
    assert "distance_source = 'waypoints'" in sql
    assert "straight_line" not in sql
    # The tracked length must not be overwritten by the fallback.
    assert "distance_m = %s" not in sql


def test_end_ride_rejects_end_before_start(monkeypatch):
    c, _ = _seq_client(monkeypatch, [("active", _NOW, 39.74, -104.98, None)])
    r = c.patch(f"/api/v1/rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-27T10:00:00Z", "end_lat": 39.75, "end_lon": -104.99,
    })
    assert r.status_code == 400


def test_one_shot_post_records_client_supplied_distance(monkeypatch):
    c, conn = _seq_client(monkeypatch, [
        _ride_row(status="completed", ended=True, distance_m=2412,
                  distance_source="client"),
    ])
    r = c.post("/api/v1/rides", json={
        "started_at": "2026-07-27T16:20:00Z", "ended_at": "2026-07-27T16:45:00Z",
        "duration_s": 1500, "distance_m": 2412, "started_in_zone": True,
        "ended_in_zone": False, "polyline": "_p~iF~ps|U_ulLnnqC",
        "vehicle_kind": "scooter", "operator": "Lime",
    })
    assert r.status_code == 200, r.text
    assert r.json()["distance_source"] == "client"
    insert = next(c for c in conn.cur.executed if c[0].startswith("INSERT INTO rides"))
    assert "'client'" in insert[0]


def test_one_shot_post_no_longer_requires_supporter(monkeypatch):
    """Regression guard for the decommercialization (sql/036): logging a
    ride used to require a paid status. Signed-in is now the only gate."""
    c, _ = _seq_client(monkeypatch, [_ride_row(status="completed", ended=True)])
    r = c.post("/api/v1/rides", json={
        "started_at": "2026-07-27T16:20:00Z", "ended_at": "2026-07-27T16:45:00Z",
        "duration_s": 1500, "distance_m": 2412, "started_in_zone": True,
        "ended_in_zone": False, "polyline": "_p~iF~ps|U_ulLnnqC",
    })
    assert r.status_code == 200, r.text


def test_one_shot_post_rejects_undecodable_polyline(monkeypatch):
    c, _ = _seq_client(monkeypatch, [])
    r = c.post("/api/v1/rides", json={
        "started_at": "2026-07-27T16:20:00Z", "ended_at": "2026-07-27T16:45:00Z",
        "duration_s": 1500, "distance_m": 2412, "started_in_zone": True,
        "ended_in_zone": False, "polyline": "!!!not-a-polyline!!!",
    })
    assert r.status_code == 400
