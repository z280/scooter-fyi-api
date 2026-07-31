"""Postgres-backed coverage for sql/050_ride_mode_usuals.sql.

Three of this migration's four deliverables do not exist against a fake
cursor, so this is the only place they are actually tested:

  * `ride_mode_usual` is a permitted `user_preferences.kind` at all
    (the widened user_preferences_kind_allowed).
  * A Usual REQUIRES a name (the extended
    user_preferences_name_matches_kind — a TOTAL rule that would reject
    every ride_mode_usual row if only the kind list had been widened).
  * idx_user_prefs_usual_name exists and is partial on kind — which is both
    the one-per-(account, name) rule AND the arbiter
    src/api_preferences.py's `ON CONFLICT (account_id, name) WHERE kind =
    'ride_mode_usual'` infers. Without the index every PUT fails outright,
    so the round-trip below is a test of the migration and not only of the
    handler.

And the fourth: that replaying the whole sql/ set over a stored Usual is a
no-op rather than a constraint that reverts to a list without
'ride_mode_usual' in it.

SKIPS unless a reachable, migratable test database is provided via
VEO_TEST_PG_DSN (same contract as tests/test_user_preferences_pg.py).
NEVER point that at production: the fixture executes every migration.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

psycopg = pytest.importorskip("psycopg")

from src import api_preferences  # noqa: E402
from src.accounts import SessionUser, require_session, upsert_account  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_TEST_EMAIL_LIKE = "pgtest-usuals-%@example.com"
_USUAL_BLOB = {
    "label": "Morning commute",
    "ride_options": {"cost_hud": True, "speedometer": "digital", "save_tracks": True},
}


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
        pytest.skip("VEO_TEST_PG_DSN not set — ride usuals Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    _apply_all(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM accounts WHERE email LIKE %s", (_TEST_EMAIL_LIKE,))
    conn.commit()

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_preferences, "connection", _fake_connection)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _account(pg_conn) -> int:
    with pg_conn.cursor() as cur:
        account_id = upsert_account(cur, f"pgtest-usuals-{uuid.uuid4()}@example.com")
    pg_conn.commit()
    return account_id


def _client(pg_conn, account_id: int) -> TestClient:
    user = SessionUser(
        account_id=account_id, email="pgtest-usuals@example.com", scopes=("rider",),
        expires_at=None, sliding=True, method="google", token_sha256="x",
    )
    app = FastAPI()
    app.include_router(api_preferences.router)
    app.dependency_overrides[require_session] = lambda: user
    return TestClient(app)


# ---------------------------------------------------------------------------
# The endpoints, against the real table
# ---------------------------------------------------------------------------
def test_usual_round_trip_and_replace(pg_conn):
    c = _client(pg_conn, _account(pg_conn))

    assert c.get("/api/v1/profile/ride-usuals").json()["ride_usuals"] == []

    r = c.put("/api/v1/profile/ride-usuals/commute", json={"settings": _USUAL_BLOB})
    assert r.status_code == 200, r.text
    assert r.json()["settings"] == _USUAL_BLOB

    # The upsert's arbiter is sql/050's partial index; a replace must update
    # the row rather than insert a second one.
    r = c.put("/api/v1/profile/ride-usuals/commute", json={"settings": {"label": "v2"}})
    assert r.status_code == 200, r.text
    assert r.json()["settings"] == {"label": "v2"}

    listed = c.get("/api/v1/profile/ride-usuals").json()["ride_usuals"]
    assert len(listed) == 1, "replace created a second row instead of updating"
    assert listed[0]["name"] == "commute"

    assert c.delete("/api/v1/profile/ride-usuals/commute").status_code == 200
    assert c.get("/api/v1/profile/ride-usuals/commute").status_code == 404


def test_usuals_and_map_settings_share_a_name_without_colliding(pg_conn):
    """Both partial unique indexes cover (account_id, name); sql/050's is
    partial on kind, so the namespaces are separate."""
    c = _client(pg_conn, _account(pg_conn))
    assert c.put("/api/v1/profile/map-settings/commute",
                 json={"settings": {"which": "map"}}).status_code == 200
    assert c.put("/api/v1/profile/ride-usuals/commute",
                 json={"settings": {"which": "usual"}}).status_code == 200

    assert c.get("/api/v1/profile/map-settings/commute").json()["settings"] == {
        "which": "map"
    }
    assert c.get("/api/v1/profile/ride-usuals/commute").json()["settings"] == {
        "which": "usual"
    }
    assert c.get("/api/v1/profile/map-settings").json()["map_settings"][0]["settings"] == {
        "which": "map"
    }
    assert c.get("/api/v1/profile/ride-usuals").json()["ride_usuals"][0]["settings"] == {
        "which": "usual"
    }


def test_the_usuals_cap_still_allows_overwriting_an_existing_one(pg_conn, monkeypatch):
    monkeypatch.setattr(api_preferences, "MAX_RIDE_USUALS", 2)
    c = _client(pg_conn, _account(pg_conn))
    c.put("/api/v1/profile/ride-usuals/one", json={"settings": {"v": 1}})
    c.put("/api/v1/profile/ride-usuals/two", json={"settings": {"v": 2}})

    assert c.put("/api/v1/profile/ride-usuals/three",
                 json={"settings": {}}).status_code == 409
    r = c.put("/api/v1/profile/ride-usuals/one", json={"settings": {"v": 99}})
    assert r.status_code == 200, "at the cap, editing an existing Usual was refused"
    assert r.json()["settings"] == {"v": 99}


def test_usuals_are_deleted_with_the_account(pg_conn):
    account_id = _account(pg_conn)
    _client(pg_conn, account_id).put(
        "/api/v1/profile/ride-usuals/commute", json={"settings": {}}
    )
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
        cur.execute(
            "SELECT COUNT(*) FROM user_preferences "
            "WHERE account_id = %s AND kind = 'ride_mode_usual'",
            (account_id,),
        )
        assert cur.fetchone()[0] == 0, "Usuals outlived their account"
    pg_conn.commit()


# ---------------------------------------------------------------------------
# The constraints themselves
# ---------------------------------------------------------------------------
def test_the_kind_is_permitted_at_all(pg_conn):
    """The widened user_preferences_kind_allowed. Written directly, not
    through the API, so the CHECK is what is under test."""
    account_id = _account(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_preferences (account_id, kind, name, settings) "
            "VALUES (%s, 'ride_mode_usual', 'direct', %s::jsonb)",
            (account_id, json.dumps(_USUAL_BLOB)),
        )
    pg_conn.commit()


def test_a_usual_must_have_a_name(pg_conn):
    """user_preferences_name_matches_kind, extended. A nameless Usual would
    be unreachable through the only API that reads it."""
    account_id = _account(pg_conn)
    with pg_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO user_preferences (account_id, kind, name) "
                "VALUES (%s, 'ride_mode_usual', NULL)",
                (account_id,),
            )
    pg_conn.rollback()


def test_the_database_refuses_a_second_usual_of_the_same_name(pg_conn):
    """idx_user_prefs_usual_name — so a writer bypassing the API cannot
    create two Usuals a read would have to pick between."""
    account_id = _account(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_preferences (account_id, kind, name) "
            "VALUES (%s, 'ride_mode_usual', 'commute')",
            (account_id,),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO user_preferences (account_id, kind, name) "
                "VALUES (%s, 'ride_mode_usual', 'commute')",
                (account_id,),
            )
    pg_conn.rollback()


def test_two_accounts_can_both_have_a_usual_of_the_same_name(pg_conn):
    """The index is on (account_id, name), not name."""
    first, second = _account(pg_conn), _account(pg_conn)
    for account_id in (first, second):
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_preferences (account_id, kind, name) "
                "VALUES (%s, 'ride_mode_usual', 'commute')",
                (account_id,),
            )
    pg_conn.commit()


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------
def test_replaying_the_migrations_over_a_stored_usual_is_a_no_op(pg_conn):
    """The guarded-rewrite shape (sql/040/041/042). A replay that
    unconditionally re-added the pre-050 kind list would take the whole
    boot down on a CheckViolation against this row."""
    account_id = _account(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_preferences (account_id, kind, name, settings) "
            "VALUES (%s, 'ride_mode_usual', 'commute', %s::jsonb)",
            (account_id, json.dumps(_USUAL_BLOB)),
        )
    pg_conn.commit()

    _apply_all(pg_conn)
    _apply_all(pg_conn)   # replay must be repeatable, not survivable once

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT settings FROM user_preferences "
            "WHERE account_id = %s AND kind = 'ride_mode_usual' AND name = 'commute'",
            (account_id,),
        )
        assert cur.fetchone()[0] == _USUAL_BLOB, "the replay destroyed a stored Usual"

    # And the replayed constraints still permit every current kind, so a
    # silently reinstated older list cannot hide until the next rider write.
    with pg_conn.cursor() as cur:
        for kind, name in (
            ("saved_map_settings", "after-replay"),
            ("ride_mode_usual", "after-replay"),
            ("find_ride_pref", None),
        ):
            cur.execute(
                "INSERT INTO user_preferences (account_id, kind, name) VALUES (%s, %s, %s)",
                (account_id, kind, name),
            )
    pg_conn.commit()
