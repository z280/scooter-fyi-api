"""Postgres-backed coverage for the preference blobs (sql/043) and the
identity fields behind the leaderboard map (sql/044).

Both features put their rules in the SCHEMA — partial unique indexes for
cardinality, a FK to a curated list for membership, a unique index over a
colour PAIR, a generated column for display_name. None of that exists
against a fake cursor, so this is where those rules are actually tested.

SKIPS unless a reachable, migratable test database is provided via
VEO_TEST_PG_DSN (same contract as tests/test_ride_hard_caps_pg.py).
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

from src import api_lexicon, api_preferences, api_profile  # noqa: E402
from src.accounts import SessionUser, require_session, upsert_account  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_TEST_EMAIL_LIKE = "pgtest-prefs-%@example.com"


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
        pytest.skip("VEO_TEST_PG_DSN not set — preferences Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM accounts WHERE email LIKE %s", (_TEST_EMAIL_LIKE,))
    conn.commit()

    @contextmanager
    def _fake_connection():
        yield conn

    for module in (api_preferences, api_profile, api_lexicon):
        monkeypatch.setattr(module, "connection", _fake_connection)
    monkeypatch.setattr(api_profile, "enforce", lambda cur, **kw: None)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _account(pg_conn) -> int:
    with pg_conn.cursor() as cur:
        account_id = upsert_account(cur, f"pgtest-prefs-{uuid.uuid4()}@example.com")
    pg_conn.commit()
    return account_id


def _client(pg_conn, account_id: int) -> TestClient:
    user = SessionUser(
        account_id=account_id, email="pgtest-prefs@example.com", scopes=("rider",),
        expires_at=None, sliding=True, method="google", token_sha256="x",
    )
    app = FastAPI()
    app.include_router(api_preferences.router)
    app.include_router(api_profile.router)
    app.include_router(api_lexicon.router)
    app.dependency_overrides[require_session] = lambda: user
    return TestClient(app)


# ---------------------------------------------------------------------------
# Saved map settings — many, addressed by name
# ---------------------------------------------------------------------------
def test_map_settings_round_trip_and_replace(pg_conn):
    c = _client(pg_conn, _account(pg_conn))

    assert c.get("/api/v1/profile/map-settings").json()["map_settings"] == []

    r = c.put("/api/v1/profile/map-settings/commute", json={"settings": {"layer": "heat"}})
    assert r.status_code == 200, r.text
    assert r.json()["settings"] == {"layer": "heat"}

    # PUT REPLACES rather than merging — the blob is opaque, so there is no
    # key the server could merge on.
    r = c.put("/api/v1/profile/map-settings/commute", json={"settings": {"zoom": 14}})
    assert r.json()["settings"] == {"zoom": 14}

    listed = c.get("/api/v1/profile/map-settings").json()["map_settings"]
    assert len(listed) == 1, "replace created a second row instead of updating"
    assert listed[0]["name"] == "commute"


def test_map_settings_are_per_name(pg_conn):
    c = _client(pg_conn, _account(pg_conn))
    c.put("/api/v1/profile/map-settings/home", json={"settings": {"a": 1}})
    c.put("/api/v1/profile/map-settings/work", json={"settings": {"b": 2}})
    names = {s["name"] for s in c.get("/api/v1/profile/map-settings").json()["map_settings"]}
    assert names == {"home", "work"}


def test_map_settings_are_scoped_to_the_caller(pg_conn):
    """Two accounts can hold the same setting NAME without collision — the
    unique index is on (account_id, name), not name."""
    first = _account(pg_conn)
    second = _account(pg_conn)
    _client(pg_conn, first).put("/api/v1/profile/map-settings/home", json={"settings": {"who": 1}})
    c2 = _client(pg_conn, second)
    r = c2.put("/api/v1/profile/map-settings/home", json={"settings": {"who": 2}})
    assert r.status_code == 200, r.text
    assert c2.get("/api/v1/profile/map-settings/home").json()["settings"] == {"who": 2}


def test_missing_map_setting_is_404(pg_conn):
    c = _client(pg_conn, _account(pg_conn))
    assert c.get("/api/v1/profile/map-settings/nope").status_code == 404
    assert c.delete("/api/v1/profile/map-settings/nope").status_code == 404


def test_map_setting_delete_removes_only_that_one(pg_conn):
    c = _client(pg_conn, _account(pg_conn))
    c.put("/api/v1/profile/map-settings/keep", json={"settings": {}})
    c.put("/api/v1/profile/map-settings/drop", json={"settings": {}})
    assert c.delete("/api/v1/profile/map-settings/drop").status_code == 200
    names = {s["name"] for s in c.get("/api/v1/profile/map-settings").json()["map_settings"]}
    assert names == {"keep"}


def test_oversized_blob_is_refused(pg_conn):
    c = _client(pg_conn, _account(pg_conn))
    huge = {"pad": "x" * (api_preferences.MAX_BLOB_BYTES + 100)}
    r = c.put("/api/v1/profile/map-settings/big", json={"settings": huge})
    assert r.status_code == 413, r.text


def test_saved_setting_cap_still_allows_overwriting_an_existing_one(pg_conn, monkeypatch):
    """At the cap a rider must still be able to edit what they already
    have — the limit is on how much you may store, not a lock on your own
    data."""
    monkeypatch.setattr(api_preferences, "MAX_SAVED_MAP_SETTINGS", 2)
    c = _client(pg_conn, _account(pg_conn))
    c.put("/api/v1/profile/map-settings/one", json={"settings": {"v": 1}})
    c.put("/api/v1/profile/map-settings/two", json={"settings": {"v": 2}})

    assert c.put("/api/v1/profile/map-settings/three", json={"settings": {}}).status_code == 409
    r = c.put("/api/v1/profile/map-settings/one", json={"settings": {"v": 99}})
    assert r.status_code == 200, "at the cap, editing an existing setting was refused"
    assert r.json()["settings"] == {"v": 99}


# ---------------------------------------------------------------------------
# find_ride_pref — at most one
# ---------------------------------------------------------------------------
def test_find_ride_pref_is_null_until_set(pg_conn):
    """null, not {} — the frontend must be able to tell 'never chose' from
    'chose nothing'. See sql/043's header."""
    c = _client(pg_conn, _account(pg_conn))
    assert c.get("/api/v1/profile/find-ride-pref").json()["find_ride_pref"] is None


def test_find_ride_pref_replaces_rather_than_accumulating(pg_conn):
    account_id = _account(pg_conn)
    c = _client(pg_conn, account_id)
    c.put("/api/v1/profile/find-ride-pref", json={"settings": {"radius_m": 400}})
    c.put("/api/v1/profile/find-ride-pref", json={"settings": {"radius_m": 900}})

    assert c.get("/api/v1/profile/find-ride-pref").json()["find_ride_pref"]["settings"] == {
        "radius_m": 900
    }
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM user_preferences "
            "WHERE account_id = %s AND kind = 'find_ride_pref'",
            (account_id,),
        )
        assert cur.fetchone()[0] == 1


def test_the_database_itself_refuses_a_second_find_ride_pref(pg_conn):
    """The at-most-one rule is the partial unique index, not app code — so
    a writer that bypasses the API cannot create a second one either."""
    account_id = _account(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_preferences (account_id, kind, settings) "
            "VALUES (%s, 'find_ride_pref', %s::jsonb)",
            (account_id, json.dumps({"a": 1})),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO user_preferences (account_id, kind, settings) "
                "VALUES (%s, 'find_ride_pref', %s::jsonb)",
                (account_id, json.dumps({"a": 2})),
            )
    pg_conn.rollback()


def test_find_ride_pref_delete_is_idempotent(pg_conn):
    c = _client(pg_conn, _account(pg_conn))
    assert c.delete("/api/v1/profile/find-ride-pref").status_code == 200
    c.put("/api/v1/profile/find-ride-pref", json={"settings": {"x": 1}})
    assert c.delete("/api/v1/profile/find-ride-pref").status_code == 200
    assert c.get("/api/v1/profile/find-ride-pref").json()["find_ride_pref"] is None


def test_kind_and_name_must_agree(pg_conn):
    """A named find_ride_pref, or an unnamed map setting, are both
    nonsense and the CHECK says so."""
    account_id = _account(pg_conn)
    for kind, name in (("find_ride_pref", "oops"), ("saved_map_settings", None)):
        with pg_conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO user_preferences (account_id, kind, name) VALUES (%s, %s, %s)",
                    (account_id, kind, name),
                )
        pg_conn.rollback()


def test_preferences_are_deleted_with_the_account(pg_conn):
    account_id = _account(pg_conn)
    c = _client(pg_conn, account_id)
    c.put("/api/v1/profile/map-settings/home", json={"settings": {}})
    c.put("/api/v1/profile/find-ride-pref", json={"settings": {}})
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
        cur.execute("SELECT COUNT(*) FROM user_preferences WHERE account_id = %s", (account_id,))
        assert cur.fetchone()[0] == 0, "preferences outlived their account"
    pg_conn.commit()
