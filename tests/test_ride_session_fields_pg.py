"""Postgres-backed coverage for the ride-session columns
(sql/049_ride_sessions.sql) and §10's reported fields
(sql/047_tracked_rides_reported_fields.sql) — the parts a fake cursor
cannot exercise: the real CHECK constraints as a backstop behind the
pydantic bounds, the NOT NULL defaults, the freshness-windowed
raw_telemetry_points read, and the acceptance criterion for this phase —
start a ride, then GET .../active and receive the SAME signing key.

Fixture and monkeypatching follow tests/test_tracked_rides_lifecycle_pg.py.

SKIPS unless a reachable, migratable test database is provided via
VEO_TEST_PG_DSN.
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

from src import api_tracked_rides  # noqa: E402
from src.accounts import SessionUser, require_session, upsert_account  # noqa: E402
from src.quality import compute_battery_percent  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
VID = "aaaa000000000000"

# A real entry from data/range_soc_lut.json, so the stamped battery is the
# production derivation rather than an arbitrary number.
_RANGE_METERS = 20708


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
        pytest.skip("VEO_TEST_PG_DSN not set — ride-session Postgres integration test skipped")
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
        cur.execute("DELETE FROM raw_telemetry_points WHERE vehicle_identifier = %s", (VID,))
        cur.execute("DELETE FROM device_state WHERE vehicle_identifier = %s", (VID,))
        cur.execute("DELETE FROM accounts WHERE email LIKE 'pgtest-session%@example.com'")
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
    monkeypatch.setattr(api_tracked_rides, "enforce", lambda cur, **kw: None)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _client(pg_conn):
    with pg_conn.cursor() as cur:
        account_id = upsert_account(cur, f"pgtest-session-{uuid.uuid4()}@example.com")
    pg_conn.commit()
    user = SessionUser(
        account_id=account_id, email="pgtest-session@example.com", scopes=("rider",),
        expires_at=datetime.now(timezone.utc),
        sliding=True, method="google", token_sha256="x",
    )
    app = FastAPI()
    app.include_router(api_tracked_rides.router)
    app.dependency_overrides[require_session] = lambda: user
    return TestClient(app)


def _observe(pg_conn, *, minutes_ago: float, lat: float, lon: float,
             current_range_meters: int | None = _RANGE_METERS) -> None:
    """One raw_telemetry_points row for VID, as the ingest cycle writes it."""
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_telemetry_points (
                snapshot_time, device_id, form_factor, latitude, longitude,
                spatial_status, vehicle_identifier, current_range_meters, is_disabled
            ) VALUES (%s, %s, 'scooter', %s, %s, 'denver_core', %s, %s, false)
            """,
            (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
             "dev-" + VID, lat, lon, VID, current_range_meters),
        )
    pg_conn.commit()


def _start(client, **body):
    payload = {"vehicle_identifier": VID, "start_lat": 39.74, "start_lon": -104.98}
    payload.update(body)
    return client.post("/api/v1/tracked-rides", json=payload)


def _stored(pg_conn, ride_id: str, columns: str) -> tuple:
    with pg_conn.cursor() as cur:
        cur.execute(f"SELECT {columns} FROM tracked_rides WHERE id = %s", (ride_id,))
        return cur.fetchone()


# ---------- the phase's acceptance criterion --------------------------------

def test_start_then_get_active_returns_the_same_signing_key(pg_conn):
    """A client that reloaded mid-ride must be able to resume signing the
    same chain, which only works if /active hands back the key start
    issued."""
    client = _client(pg_conn)
    started = _start(client)
    assert started.status_code == 200, started.text
    pg_conn.commit()
    issued = started.json()["track_signing"]
    assert issued["alg"] == "HS256"
    assert issued["key_id"] == started.json()["id"]

    active = client.get("/api/v1/tracked-rides/active")
    assert active.status_code == 200, active.text
    assert active.json()["active"]["track_signing"] == issued

    detail = client.get(f"/api/v1/tracked-rides/{started.json()['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["track_signing"] == issued

    # And the key really is what the row holds, not something re-minted per
    # response.
    key, nonce = _stored(pg_conn, started.json()["id"], "track_key, track_nonce")
    assert (key, nonce) == (issued["key"], issued["nonce"])


def test_the_list_response_never_carries_the_key(pg_conn):
    client = _client(pg_conn)
    started = _start(client)
    assert started.status_code == 200, started.text
    pg_conn.commit()
    key = started.json()["track_signing"]["key"]

    listed = client.get("/api/v1/tracked-rides")
    assert listed.status_code == 200, listed.text
    assert listed.json()["count"] == 1
    assert "track_signing" not in listed.json()["rides"][0]
    assert key not in listed.text


def test_two_rides_get_different_keys(pg_conn):
    """Per-ride keys, so a compromise is bounded to one ride."""
    keys = set()
    for _ in range(2):
        client = _client(pg_conn)   # a fresh account, so no active-ride 409
        started = _start(client)
        assert started.status_code == 200, started.text
        pg_conn.commit()
        keys.add(started.json()["track_signing"]["key"])
    assert len(keys) == 2


# ---------- the feed-anchored start ----------------------------------------

def test_feed_start_is_stamped_from_the_newest_fresh_observation(pg_conn):
    """Newest wins, and the battery comes from the LUT-backed derivation."""
    _observe(pg_conn, minutes_ago=20, lat=39.700000, lon=-104.900000,
             current_range_meters=0)
    _observe(pg_conn, minutes_ago=2, lat=39.741234, lon=-104.987654)
    client = _client(pg_conn)
    started = _start(client)
    assert started.status_code == 200, started.text
    pg_conn.commit()

    battery, lat, lon = _stored(
        pg_conn, started.json()["id"],
        "feed_start_battery_percent, feed_start_lat, feed_start_lon")
    assert battery == compute_battery_percent(_RANGE_METERS)
    assert lat == pytest.approx(39.741234)
    assert lon == pytest.approx(-104.987654)


def test_feed_start_is_null_when_the_only_observation_is_stale(pg_conn):
    """Past the freshness window we stamp nothing rather than an anchor from
    before somebody else's ride — A2's correlation then falls back to the
    client-supplied start."""
    _observe(pg_conn,
             minutes_ago=api_tracked_rides.FEED_START_MAX_AGE_MINUTES + 5,
             lat=39.70, lon=-104.90)
    client = _client(pg_conn)
    started = _start(client)
    assert started.status_code == 200, started.text
    pg_conn.commit()

    assert _stored(pg_conn, started.json()["id"],
                   "feed_start_battery_percent, feed_start_lat, feed_start_lon") \
        == (None, None, None)


def test_a_vehicle_with_no_observations_at_all_still_starts(pg_conn):
    """device_state knows the vehicle but the 48-hour telemetry buffer has
    been flushed — the ride is not refused over a missing anchor."""
    client = _client(pg_conn)
    started = _start(client)
    assert started.status_code == 200, started.text
    pg_conn.commit()
    assert _stored(pg_conn, started.json()["id"], "feed_start_lat") == (None,)


def test_another_vehicles_observation_is_not_used(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_telemetry_points (
                snapshot_time, device_id, form_factor, latitude, longitude,
                spatial_status, vehicle_identifier, current_range_meters
            ) VALUES (NOW(), 'dev-other', 'scooter', 39.5, -105.5,
                      'denver_core', 'bbbb000000000000', %s)
            """,
            (_RANGE_METERS,),
        )
    pg_conn.commit()
    client = _client(pg_conn)
    started = _start(client)
    assert started.status_code == 200, started.text
    pg_conn.commit()
    assert _stored(pg_conn, started.json()["id"], "feed_start_lat") == (None,)


# ---------- ride_options + start battery storage ---------------------------

def test_ride_options_and_start_battery_round_trip_through_real_columns(pg_conn):
    options = {"cost_hud": True, "speedometer": "digital", "theme": "dark",
               "navigation": False, "save_tracks": True, "battery_modeling": True,
               "nav_improvement": False, "end_survey": True, "own_device": False}
    client = _client(pg_conn)
    started = _start(client, ride_options=options, reported_start_battery_percent=87.5)
    assert started.status_code == 200, started.text
    pg_conn.commit()
    assert started.json()["ride_options"] == options
    assert started.json()["validation"] == {"status": "pending", "reasons": []}

    stored_options, battery = _stored(
        pg_conn, started.json()["id"], "ride_options, reported_start_battery_percent")
    assert stored_options == options
    assert float(battery) == 87.5


def test_a_ride_started_without_them_lands_on_the_column_defaults(pg_conn):
    client = _client(pg_conn)
    started = _start(client)
    assert started.status_code == 200, started.text
    pg_conn.commit()
    assert _stored(pg_conn, started.json()["id"],
                   "ride_options, validation_status, validation_reasons, validated_at") \
        == ({}, "pending", [], None)


# ---------- §10 reported fields, and the CHECKs behind them ----------------

def test_reported_fields_round_trip_through_end(pg_conn):
    client = _client(pg_conn)
    started = _start(client, ride_options={"save_tracks": True})
    assert started.status_code == 200, started.text
    pg_conn.commit()

    ended = client.patch(f"/api/v1/tracked-rides/{started.json()['id']}/end", json={
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "end_lat": 39.75, "end_lon": -104.99,
        "reported_minutes": 1440, "reported_plan": "equity",
    })
    assert ended.status_code == 200, ended.text
    pg_conn.commit()
    assert ended.json()["reported_minutes"] == 1440
    assert ended.json()["reported_plan"] == "equity"
    # save_tracks was on and the feed has not resolved, so the provisional
    # status waits on the feed and nothing is settled yet.
    assert ended.json()["validation"] == {"status": "pending_feed", "reasons": []}
    assert _stored(pg_conn, started.json()["id"],
                   "reported_minutes, reported_plan, validation_status, validated_at") \
        == (1440, "equity", "pending_feed", None)


def test_a_ride_that_never_opted_into_tracking_settles_as_ineligible(pg_conn):
    client = _client(pg_conn)
    started = _start(client, ride_options={"save_tracks": False})
    assert started.status_code == 200, started.text
    pg_conn.commit()

    ended = client.patch(f"/api/v1/tracked-rides/{started.json()['id']}/end", json={
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "end_lat": 39.75, "end_lon": -104.99,
    })
    assert ended.status_code == 200, ended.text
    pg_conn.commit()
    assert ended.json()["validation"] == {
        "status": "ineligible", "reasons": ["tracking_not_opted"]}
    status, reasons, validated_at = _stored(
        pg_conn, started.json()["id"],
        "validation_status, validation_reasons, validated_at")
    assert (status, reasons) == ("ineligible", ["tracking_not_opted"])
    assert validated_at is not None, "a terminal status is stamped as settled"


@pytest.mark.parametrize("column,value", [
    ("reported_minutes", 1441),
    ("reported_minutes", -1),
    ("reported_plan", "gold"),
    ("reported_start_battery_percent", 100.5),
    ("reported_start_battery_percent", -0.1),
    ("validation_status", "not_a_status"),
])
def test_the_checks_are_the_backstop_behind_the_api_bounds(pg_conn, column, value):
    """sql/047 and sql/049 install these as named constraints so a writer
    that bypasses the API cannot store what the API refuses."""
    with pg_conn.cursor() as cur:
        account_id = upsert_account(cur, f"pgtest-session-{uuid.uuid4()}@example.com")
    pg_conn.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        with pg_conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO tracked_rides (
                    account_id, vehicle_identifier, start_lat, start_lon,
                    watch_expires_at, {column}
                ) VALUES (%s, %s, 39.74, -104.98, NOW(), %s)
                """,
                (account_id, VID, value),
            )
    pg_conn.rollback()


@pytest.mark.parametrize("status", [
    "pending", "pending_feed", "eligible", "ineligible", "error"])
def test_every_documented_validation_status_is_storable(pg_conn, status):
    """The mirror of the constraint test: VALIDATION_STATUSES in
    api_tracked_rides must not contain a value the column rejects."""
    assert status in api_tracked_rides.VALIDATION_STATUSES
    with pg_conn.cursor() as cur:
        account_id = upsert_account(cur, f"pgtest-session-{uuid.uuid4()}@example.com")
        cur.execute(
            """
            INSERT INTO tracked_rides (
                account_id, vehicle_identifier, start_lat, start_lon,
                watch_expires_at, validation_status
            ) VALUES (%s, %s, 39.74, -104.98, NOW(), %s)
            """,
            (account_id, VID, status),
        )
    pg_conn.commit()


def test_every_rate_plan_tier_is_storable_as_a_reported_plan(pg_conn):
    """§10 reuses accounts.rate_plan's vocabulary; both CHECKs must agree."""
    with pg_conn.cursor() as cur:
        for plan in ("resident", "visitor", "equity"):
            account_id = upsert_account(cur, f"pgtest-session-{uuid.uuid4()}@example.com")
            cur.execute("UPDATE accounts SET rate_plan = %s WHERE id = %s", (plan, account_id))
            cur.execute(
                """
                INSERT INTO tracked_rides (
                    account_id, vehicle_identifier, start_lat, start_lon,
                    watch_expires_at, reported_plan
                ) VALUES (%s, %s, 39.74, -104.98, NOW(), %s)
                """,
                (account_id, VID, plan),
            )
    pg_conn.commit()
