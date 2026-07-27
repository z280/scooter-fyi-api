"""Postgres-backed integration coverage for the tracked-rides lifecycle —
the parts a fake cursor can't meaningfully exercise: the FK constraints
tying gbfs_left_feed_cycle_id/gbfs_reappeared_cycle_id/
last_checked_cycle_id to a real observation_cycles row (exactly what
cycle.py:_start_cycle() creates before ride_watch ever runs), and the
full start -> leaves feed -> reappears -> user reports -> anti-fraud
redaction lifts flow end-to-end through real SQL.

SKIPS unless a reachable, migratable test database is provided via
VEO_TEST_PG_DSN (see tests/test_daily_trips_rollup_pg.py for the pattern).
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

from src import api_tracked_rides, ride_watch  # noqa: E402
from src.accounts import SessionUser, require_session, upsert_account  # noqa: E402
from src.ingest import TaggedDevice  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
VID = "aaaa000000000000"


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
        pytest.skip("VEO_TEST_PG_DSN not set — tracked-rides Postgres integration test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()

    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM tracked_rides")
        cur.execute("DELETE FROM device_state WHERE vehicle_identifier = %s", (VID,))
        cur.execute("DELETE FROM accounts WHERE email LIKE 'pgtest-ride%@example.com'")
        cur.execute(
            """
            INSERT INTO device_state (
                vehicle_identifier, vehicle_plate, current_lat, current_lon,
                first_observed_at_location, first_ever_observed_at, last_observed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (VID, "1231234", 39.74, -104.98, now, now, now),
        )
    conn.commit()

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_tracked_rides, "connection", _fake_connection)
    monkeypatch.setattr(ride_watch, "connection", _fake_connection)
    monkeypatch.setattr(api_tracked_rides, "enforce", lambda cur, **kw: None)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _seed_cycle(conn) -> uuid.UUID:
    """Mirrors cycle.py:_start_cycle()'s INSERT — in production this row
    always exists (and is committed) before ride_watch ever runs, which
    is exactly what the FK constraints below depend on."""
    cid = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO observation_cycles (cycle_id, start_ts, job_status) VALUES (%s, %s, 'in_progress')",
            (str(cid), datetime.now(timezone.utc)),
        )
    conn.commit()
    return cid


def _client(pg_conn):
    with pg_conn.cursor() as cur:
        account_id = upsert_account(cur, f"pgtest-ride-{uuid.uuid4()}@example.com")
    pg_conn.commit()
    user = SessionUser(
        account_id=account_id, email="pgtest-ride@example.com", scopes=("rider",),
        supporter=False, expires_at=datetime.now(timezone.utc),
        sliding=True, method="google", token_sha256="x",
    )
    app = FastAPI()
    app.include_router(api_tracked_rides.router)
    app.dependency_overrides[require_session] = lambda: user
    return TestClient(app)


def test_full_lifecycle_with_gbfs_watch_and_anti_fraud_redaction(pg_conn):
    client = _client(pg_conn)
    now = datetime.now(timezone.utc)

    # Start
    r = client.post("/api/v1/tracked-rides", json={
        "vehicle_identifier": VID, "start_lat": 39.74, "start_lon": -104.98,
    })
    assert r.status_code == 200, r.text
    ride = r.json()
    assert ride["plate_display_code"] == "ZTRZTRF"
    ride_id = ride["id"]
    pg_conn.commit()

    # A second concurrent start is rejected
    r_dup = client.post("/api/v1/tracked-rides", json={
        "vehicle_identifier": VID, "start_lat": 39.74, "start_lon": -104.98,
    })
    assert r_dup.status_code == 409

    # A waypoint
    r_wp = client.post(f"/api/v1/tracked-rides/{ride_id}/waypoints", json={
        "waypoint_at": (now + timedelta(seconds=30)).isoformat(),
        "lat": 39.741, "lon": -104.981,
    })
    assert r_wp.status_code == 200, r_wp.text
    pg_conn.commit()

    # Cycle 1: vehicle absent from the feed -> left_feed (real FK to
    # observation_cycles, exercised via _seed_cycle)
    cycle1 = _seed_cycle(pg_conn)
    stats1 = ride_watch.update_watches_for_cycle(cycle1, datetime.now(timezone.utc), devices=[])
    assert stats1.newly_left_feed == 1

    # Cycle 2: vehicle reappears
    dev = TaggedDevice(
        device_id="bike-2", vehicle_type_id="1", form_factor="scooter",
        lat=39.7505, lon=-104.9895, spatial_status="denver_core",
        vehicle_identifier=VID, current_range_meters=8000,
    )
    cycle2 = _seed_cycle(pg_conn)
    stats2 = ride_watch.update_watches_for_cycle(cycle2, datetime.now(timezone.utc), devices=[dev])
    assert stats2.newly_reappeared == 1

    # ANTI-FRAUD: redacted in the response even though the DB has it
    r_detail = client.get(f"/api/v1/tracked-rides/{ride_id}")
    assert r_detail.json()["gbfs_reappeared_at"] is None
    with pg_conn.cursor() as cur:
        cur.execute("SELECT gbfs_reappeared_at FROM tracked_rides WHERE id = %s", (ride_id,))
        assert cur.fetchone()[0] is not None  # underlying row DOES have it

    # User reports the end
    r_end = client.patch(f"/api/v1/tracked-rides/{ride_id}/end", json={
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "end_lat": 39.7506, "end_lon": -104.9896,
        "reported_battery_percent": 78.2, "total_cost_cents": 350,
    })
    assert r_end.status_code == 200, r_end.text
    assert r_end.json()["status"] == "completed"
    pg_conn.commit()

    # Redaction lifts post-report
    r_detail2 = client.get(f"/api/v1/tracked-rides/{ride_id}")
    assert r_detail2.json()["gbfs_reappeared_at"] is not None

    # Single-shot: a second end report is rejected
    assert client.patch(f"/api/v1/tracked-rides/{ride_id}/end", json={
        "ended_at": datetime.now(timezone.utc).isoformat(), "end_lat": 1, "end_lon": 1,
    }).status_code == 409

    # No more waypoints once completed
    assert client.post(f"/api/v1/tracked-rides/{ride_id}/waypoints", json={
        "waypoint_at": datetime.now(timezone.utc).isoformat(), "lat": 1, "lon": 1,
    }).status_code == 409


def test_expired_watch_is_not_active_and_expire_stale_watches_closes_it(pg_conn):
    from src.cli import expire_stale_watches

    client = _client(pg_conn)
    r = client.post("/api/v1/tracked-rides", json={
        "vehicle_identifier": VID, "start_lat": 39.74, "start_lon": -104.98,
    })
    ride_id = r.json()["id"]
    pg_conn.commit()

    # Force the watch into the past, as if 3h had already elapsed.
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE tracked_rides SET watch_expires_at = NOW() - INTERVAL '1 minute' WHERE id = %s",
            (ride_id,),
        )
        cur.execute(
            "UPDATE user_device_watch_list SET watch_expires_at = NOW() - INTERVAL '1 minute' "
            "WHERE tracked_ride_id = %s",
            (ride_id,),
        )
    pg_conn.commit()

    assert client.get("/api/v1/tracked-rides/active").json()["active"] is None

    from unittest.mock import patch
    with patch("src.cli.connection", lambda: _NullCtx(pg_conn)):
        result = expire_stale_watches()
    pg_conn.commit()
    assert result["watches_expired"] >= 1
    assert result["rides_expired"] >= 1

    with pg_conn.cursor() as cur:
        cur.execute("SELECT status FROM tracked_rides WHERE id = %s", (ride_id,))
        assert cur.fetchone()[0] == "expired"


class _NullCtx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False
