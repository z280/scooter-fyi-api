"""Postgres-backed coverage for sql/081_favorite_devices.sql.

Four things that do not exist against a fake cursor:

  * the one-row-per-(rider, vehicle) constraint, which is also the arbiter
    src/api_favorites.py's `ON CONFLICT (account_id, vehicle_identifier)`
    infers — without it every keep fails outright;
  * the nickname length CHECK;
  * the cascade, so letting go of an account lets go of its favourites;
  * and the ACTUAL QUERY. `_rows_for` LEFT JOINs favorite_devices to
    device_state and to this cycle's raw_telemetry_points across fifteen
    columns; a fake cursor validates none of those names. This file runs it
    against the real schema with a real fleet row, which is the only place a
    typo in it would be caught before deploy.

The rules themselves — the gate, the withholding, the state machine — are
tested against the fake in tests/test_favorite_devices.py, where the fleet
states that matter can be arranged one at a time.

SKIPS unless a reachable, migratable test database is provided via
VEO_TEST_PG_DSN. NEVER point that at production: the fixture executes every
migration.
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

from src import api_favorites  # noqa: E402
from src.accounts import SessionUser, require_session, upsert_account  # noqa: E402
from src.identity import hash_plate  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_TEST_EMAIL_LIKE = "pgtest-favs-%@example.com"

_PLATE = "10-25 543"
_VID = hash_plate(_PLATE)
_QR = f"https://veoride.com/x?number={_PLATE}"
_POS = (39.7392, -104.9903)


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


def _apply_all(conn) -> None:
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()


@pytest.fixture()
def pg_conn(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — favorites Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    _apply_all(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM accounts WHERE email LIKE %s", (_TEST_EMAIL_LIKE,))
        cur.execute("DELETE FROM device_state WHERE vehicle_identifier = %s", (_VID,))
    conn.commit()

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_favorites, "connection", _fake_connection)
    monkeypatch.setattr(api_favorites, "enforce", lambda cur, **kw: None)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _account(pg_conn) -> int:
    with pg_conn.cursor() as cur:
        account_id = upsert_account(cur, f"pgtest-favs-{uuid.uuid4()}@example.com")
    pg_conn.commit()
    return account_id


def _fleet(pg_conn, *, reserved: bool = False, in_feed: bool = True) -> None:
    """One vehicle, in device_state and (optionally) in this cycle's feed."""
    now = datetime.now(timezone.utc)
    cycle_id = uuid.uuid4()
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO observation_cycles (cycle_id, job_status) VALUES (%s, 'complete')",
            (cycle_id,),
        )
        cur.execute(
            "INSERT INTO snapshot_metadata_core (cycle_id, snapshot_time) VALUES (%s, %s)",
            (cycle_id, now),
        )
        cur.execute(
            """
            INSERT INTO device_state (
                vehicle_identifier, current_lat, current_lon,
                first_observed_at_location, first_ever_observed_at,
                last_observed_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (vehicle_identifier) DO UPDATE SET
                current_lat = EXCLUDED.current_lat,
                current_lon = EXCLUDED.current_lon,
                last_observed_at = EXCLUDED.last_observed_at
            """,
            (_VID, _POS[0], _POS[1], now, now, now),
        )
        if in_feed:
            cur.execute(
                """
                INSERT INTO raw_telemetry_points (
                    cycle_id, snapshot_time, device_id, form_factor,
                    latitude, longitude, spatial_status,
                    vehicle_identifier, is_disabled, is_reserved,
                    current_range_meters, vehicle_model_name, vehicle_use_type
                ) VALUES (%s, %s, 'bike-1', 'scooter', %s, %s, 'denver_core',
                          %s, false, %s, 12000, 'Cosmo', 'sitting')
                """,
                (cycle_id, now, _POS[0], _POS[1], _VID, reserved),
            )
    pg_conn.commit()


def _client(pg_conn, account_id: int) -> TestClient:
    user = SessionUser(
        account_id=account_id, email="pgtest-favs@example.com", scopes=("rider",),
        expires_at=None, sliding=True, method="google", token_sha256="x",
    )
    app = FastAPI()
    app.include_router(api_favorites.router)
    app.dependency_overrides[require_session] = lambda: user
    return TestClient(app)


def _body(**over):
    return {"vehicle_identifier": _VID, "qr_raw_value": _QR,
            "lat": _POS[0], "lng": _POS[1], **over}


# ---------------------------------------------------------------------------
# The real query
# ---------------------------------------------------------------------------
def test_keep_list_rename_and_let_go_against_the_real_schema(pg_conn):
    """The fifteen-column LEFT JOIN, exercised. A fake cursor cannot fail on a
    column name; this does."""
    _fleet(pg_conn)
    c = _client(pg_conn, _account(pg_conn))

    assert c.get("/api/v1/profile/favorite-devices").json()["favorite_devices"] == []

    r = c.post("/api/v1/profile/favorite-devices", json=_body(nickname="My Rover"))
    assert r.status_code == 201, r.text
    assert r.json()["favorite"]["state"] == "available"
    assert r.json()["favorite"]["nickname"] == "My Rover"
    assert r.json()["favorite"]["vehicle_model_name"] == "Cosmo"
    assert r.json()["favorite"]["battery_percent"] is not None

    listed = c.get("/api/v1/profile/favorite-devices").json()["favorite_devices"]
    assert [f["vehicle_identifier"] for f in listed] == [_VID]

    r = c.patch(f"/api/v1/profile/favorite-devices/{_VID}",
                json={"notify_on_available": True})
    assert r.status_code == 200, r.text
    assert r.json()["favorite"]["notify_on_available"] is True

    assert c.delete(f"/api/v1/profile/favorite-devices/{_VID}").status_code == 200
    assert c.get("/api/v1/profile/favorite-devices").json()["favorite_devices"] == []


def test_the_withholding_survives_the_real_query(pg_conn):
    """Not a duplicate of the fake-cursor test: this one proves the reserved
    flag makes it out of raw_telemetry_points and through the join, which is
    where the rule would silently stop working if a column were renamed."""
    _fleet(pg_conn, reserved=True)
    c = _client(pg_conn, _account(pg_conn))
    c.post("/api/v1/profile/favorite-devices", json=_body())

    row = c.get("/api/v1/profile/favorite-devices").json()["favorite_devices"][0]
    assert row["state"] == "in_use"
    assert row["position_withheld"] is True
    assert "lat" not in row and "battery_percent" not in row


def test_re_keeping_updates_the_row_instead_of_inserting_a_second(pg_conn):
    """The ON CONFLICT arbiter is favorite_devices_unique. Without that
    constraint the upsert has nothing to infer and every keep 500s."""
    _fleet(pg_conn)
    account_id = _account(pg_conn)
    c = _client(pg_conn, account_id)

    first = c.post("/api/v1/profile/favorite-devices", json=_body(nickname="Mine"))
    assert first.json()["already_favorited"] is False
    second = c.post("/api/v1/profile/favorite-devices", json=_body())
    assert second.status_code == 201, second.text
    assert second.json()["already_favorited"] is True
    # A re-scan with no nickname keeps the one they typed.
    assert second.json()["favorite"]["nickname"] == "Mine"

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM favorite_devices WHERE account_id = %s",
            (account_id,),
        )
        assert cur.fetchone()[0] == 1
    pg_conn.commit()


# ---------------------------------------------------------------------------
# The constraints
# ---------------------------------------------------------------------------
def test_the_database_refuses_a_second_row_for_one_rider_and_vehicle(pg_conn):
    account_id = _account(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO favorite_devices (account_id, vehicle_identifier) "
            "VALUES (%s, %s)", (account_id, _VID),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO favorite_devices (account_id, vehicle_identifier) "
                "VALUES (%s, %s)", (account_id, _VID),
            )
    pg_conn.rollback()


def test_two_riders_can_keep_the_same_scooter(pg_conn):
    """The constraint is on the PAIR. A popular scooter is not a conflict."""
    first, second = _account(pg_conn), _account(pg_conn)
    with pg_conn.cursor() as cur:
        for account_id in (first, second):
            cur.execute(
                "INSERT INTO favorite_devices (account_id, vehicle_identifier) "
                "VALUES (%s, %s)", (account_id, _VID),
            )
    pg_conn.commit()


def test_an_over_long_nickname_is_refused(pg_conn):
    account_id = _account(pg_conn)
    with pg_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO favorite_devices "
                "(account_id, vehicle_identifier, nickname) VALUES (%s, %s, %s)",
                (account_id, _VID, "n" * 41),
            )
    pg_conn.rollback()


def test_favorites_are_deleted_with_the_account(pg_conn):
    account_id = _account(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO favorite_devices (account_id, vehicle_identifier) "
            "VALUES (%s, %s)", (account_id, _VID),
        )
        cur.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
        cur.execute(
            "SELECT COUNT(*) FROM favorite_devices WHERE account_id = %s",
            (account_id,),
        )
        assert cur.fetchone()[0] == 0, "favourites outlived their account"
    pg_conn.commit()


def test_a_retired_vehicle_reads_gone_and_withholds_its_last_position(pg_conn):
    """What retirement actually looks like: the vehicle stops appearing in new
    cycles while its device_state row persists. (An earlier version of this
    test deleted device_state instead and asserted the same thing — which was
    wrong about the code AND about the fleet. The vehicle was still in the
    latest cycle's feed, so `available` was the right answer.)"""
    _fleet(pg_conn)
    account_id = _account(pg_conn)
    c = _client(pg_conn, account_id)
    c.post("/api/v1/profile/favorite-devices", json=_body())

    stale = datetime.now(timezone.utc) - timedelta(
        hours=api_favorites.GONE_AFTER_HOURS + 1
    )
    later = uuid.uuid4()
    with pg_conn.cursor() as cur:
        # A newer complete cycle that does not contain this vehicle.
        cur.execute(
            "INSERT INTO observation_cycles (cycle_id, job_status) "
            "VALUES (%s, 'complete')", (later,),
        )
        cur.execute(
            "INSERT INTO snapshot_metadata_core (cycle_id, snapshot_time) "
            "VALUES (%s, NOW())", (later,),
        )
        cur.execute(
            "UPDATE device_state SET last_observed_at = %s "
            "WHERE vehicle_identifier = %s", (stale, _VID),
        )
    pg_conn.commit()

    row = c.get("/api/v1/profile/favorite-devices").json()["favorite_devices"][0]
    assert row["state"] == "gone"
    assert row["position_withheld"] is True
    assert "lat" not in row


def test_a_favorite_outlives_its_device_state_row(pg_conn):
    """No foreign key to device_state, on purpose — a favourite that vanished
    from a rider's list because a row was cleaned up elsewhere would be a
    worse answer than one that says "not seen since". The row must still
    render rather than disappearing or throwing."""
    _fleet(pg_conn, in_feed=False)
    account_id = _account(pg_conn)
    c = _client(pg_conn, account_id)
    c.post("/api/v1/profile/favorite-devices", json=_body())

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM device_state WHERE vehicle_identifier = %s", (_VID,))
    pg_conn.commit()

    rows = c.get("/api/v1/profile/favorite-devices").json()["favorite_devices"]
    assert [r["vehicle_identifier"] for r in rows] == [_VID]
    # Nothing left that could say where it is, so nothing is claimed.
    assert rows[0]["state"] == "gone"
    assert rows[0]["position_withheld"] is True


def test_the_notify_index_is_partial(pg_conn):
    """The per-cycle availability watch is a targeted indexed read, not a
    scan — the same contract src/ride_watch.py's own index carries."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'idx_favorite_devices_notify'"
        )
        row = cur.fetchone()
    assert row is not None, "idx_favorite_devices_notify is missing"
    assert "WHERE notify_on_available" in row[0]


def test_replaying_the_migrations_over_a_stored_favorite_is_a_no_op(pg_conn):
    account_id = _account(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO favorite_devices "
            "(account_id, vehicle_identifier, nickname) VALUES (%s, %s, 'Mine')",
            (account_id, _VID),
        )
    pg_conn.commit()

    _apply_all(pg_conn)
    _apply_all(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT nickname FROM favorite_devices "
            "WHERE account_id = %s AND vehicle_identifier = %s",
            (account_id, _VID),
        )
        assert cur.fetchone()[0] == "Mine", "the replay destroyed a stored favourite"
    pg_conn.commit()
