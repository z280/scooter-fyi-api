"""Postgres-backed coverage for the off-feed ride lifecycle (sql/035) —
the parts a fake cursor can't exercise:

  - the partial unique index that allows only ONE active ride per account
  - the rides_completed_is_complete CHECK, which must accept an active
    ride with half its columns NULL and still reject an incomplete
    *completed* one
  - server-side distance measured from real waypoint rows
  - ON DELETE CASCADE from rides to off_feed_ride_waypoints, which is what
    makes DELETE a true hard delete of the whole route

SKIPS unless a reachable, migratable test database is provided via
VEO_TEST_PG_DSN (see tests/test_tracked_rides_lifecycle_pg.py).
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

psycopg = pytest.importorskip("psycopg")

from src import api_rides  # noqa: E402
from src.accounts import SessionUser, require_session, upsert_account  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_NOW = datetime.now(timezone.utc)


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture()
def pg_conn(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — off-feed rides Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM rides")
        cur.execute("DELETE FROM accounts WHERE email LIKE 'pgtest-offfeed%@example.com'")
    conn.commit()

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_rides, "connection", _fake_connection)
    monkeypatch.setattr(api_rides, "enforce", lambda cur, **kw: None)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _client(pg_conn):
    with pg_conn.cursor() as cur:
        account_id = upsert_account(cur, f"pgtest-offfeed-{uuid.uuid4()}@example.com")
    pg_conn.commit()
    user = SessionUser(
        account_id=account_id, email="pgtest-offfeed@example.com", scopes=("rider",),
        expires_at=_NOW, sliding=True, method="google", token_sha256="x",
    )
    app = FastAPI()
    app.include_router(api_rides.router)
    app.dependency_overrides[require_session] = lambda: user
    return TestClient(app), account_id


def test_full_lifecycle_measures_distance_from_waypoints(pg_conn):
    c, _ = _client(pg_conn)

    start = c.post("/api/v1/rides/start", json={
        "start_lat": 39.74, "start_lon": -104.98,
        "vehicle_kind": "scooter", "operator": "Lime",
        # Pinned rather than defaulted to the server's NOW(), so the
        # duration assertion below isn't a race against the clock.
        "started_at": _NOW.isoformat(),
    })
    assert start.status_code == 200, start.text
    ride = start.json()
    rid = ride["id"]
    # An active ride is legal with ended_at/duration/polyline all NULL —
    # the completed-is-complete CHECK must not fire here.
    assert ride["status"] == "active"
    assert ride["ended_at"] is None and ride["distance_m"] is None

    assert c.get("/api/v1/rides/active").json()["active"]["id"] == rid

    # ~100 m of latitude per step, three steps.
    step = 100.0 / 111_320.0
    for i in range(1, 4):
        r = c.post(f"/api/v1/rides/{rid}/waypoints", json={
            "waypoint_at": (_NOW + timedelta(seconds=30 * i)).isoformat(),
            "lat": 39.74 + i * step, "lon": -104.98,
        })
        assert r.status_code == 200, r.text

    wps = c.get(f"/api/v1/rides/{rid}/waypoints").json()
    assert wps["count"] == 3

    end = c.patch(f"/api/v1/rides/{rid}/end", json={
        "ended_at": (_NOW + timedelta(minutes=25)).isoformat(),
        "end_lat": 39.74 + 3 * step, "end_lon": -104.98,
        "est_cost_cents": 415,
    })
    assert end.status_code == 200, end.text
    done = end.json()
    assert done["status"] == "completed"
    assert done["distance_source"] == "waypoints"
    # Three ~100 m legs, measured along the track — not the straight line.
    assert 290 <= done["distance_m"] <= 310
    assert done["duration_s"] == 1500
    assert done["polyline"]

    # Single-shot: the end can't be re-reported.
    again = c.patch(f"/api/v1/rides/{rid}/end", json={
        "ended_at": (_NOW + timedelta(minutes=30)).isoformat(),
        "end_lat": 39.75, "end_lon": -104.99,
    })
    assert again.status_code == 409


def test_only_one_active_ride_per_account(pg_conn):
    c, _ = _client(pg_conn)
    first = c.post("/api/v1/rides/start", json={"start_lat": 39.74, "start_lon": -104.98})
    assert first.status_code == 200, first.text
    second = c.post("/api/v1/rides/start", json={"start_lat": 39.75, "start_lon": -104.99})
    assert second.status_code == 409


def test_a_completed_ride_frees_the_active_slot(pg_conn):
    c, _ = _client(pg_conn)
    rid = c.post("/api/v1/rides/start",
                 json={"start_lat": 39.74, "start_lon": -104.98}).json()["id"]
    c.patch(f"/api/v1/rides/{rid}/end", json={
        "ended_at": (_NOW + timedelta(minutes=5)).isoformat(),
        "end_lat": 39.75, "end_lon": -104.98,
    })
    again = c.post("/api/v1/rides/start", json={"start_lat": 39.76, "start_lon": -104.97})
    assert again.status_code == 200, again.text


def test_end_without_waypoints_uses_the_straight_line(pg_conn):
    c, _ = _client(pg_conn)
    rid = c.post("/api/v1/rides/start",
                 json={"start_lat": 39.74, "start_lon": -104.98}).json()["id"]
    done = c.patch(f"/api/v1/rides/{rid}/end", json={
        "ended_at": (_NOW + timedelta(minutes=5)).isoformat(),
        "end_lat": 39.75, "end_lon": -104.98,
    }).json()
    assert done["distance_source"] == "straight_line"
    assert 1100 <= done["distance_m"] <= 1120


def test_delete_cascades_to_waypoints(pg_conn):
    """The hard-delete privacy commitment covers the whole route, not just
    the ride row."""
    c, _ = _client(pg_conn)
    rid = c.post("/api/v1/rides/start",
                 json={"start_lat": 39.74, "start_lon": -104.98}).json()["id"]
    c.post(f"/api/v1/rides/{rid}/waypoints", json={
        "waypoint_at": _NOW.isoformat(), "lat": 39.741, "lon": -104.981,
    })
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM off_feed_ride_waypoints WHERE ride_id = %s", (rid,))
        assert cur.fetchone()[0] == 1

    assert c.delete(f"/api/v1/rides/{rid}").status_code == 200
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM off_feed_ride_waypoints WHERE ride_id = %s", (rid,))
        assert cur.fetchone()[0] == 0


def test_one_shot_log_is_stored_as_client_measured(pg_conn):
    c, _ = _client(pg_conn)
    r = c.post("/api/v1/rides", json={
        "started_at": _NOW.isoformat(),
        "ended_at": (_NOW + timedelta(minutes=25)).isoformat(),
        "duration_s": 1500, "distance_m": 2412,
        "started_in_zone": True, "ended_in_zone": False,
        "polyline": "_p~iF~ps|U_ulLnnqC",
        "vehicle_kind": "bicycle", "operator": "personal",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["distance_source"] == "client"
    assert body["distance_m"] == 2412
    assert body["operator"] == "personal"
    # A one-shot log does not occupy the active slot.
    assert c.get("/api/v1/rides/active").json() == {"active": None}
