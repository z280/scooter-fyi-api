"""Postgres-backed regression coverage for sql/042 — the widened
auth_sessions.method CHECK.

This is the bug a fake cursor structurally cannot catch: every unit test
of the typed-code door passes a stub cursor that accepts any INSERT, so
the door looked fine in CI while being dead in production. The constraint
only exists in a real database, so the test that proves the door works has
to talk to one.

Covers:
  - the full emailed-code round trip (request -> verify -> session row),
    asserting the minted row really carries method='email_code'
  - every method value the code can mint being accepted, and a bogus one
    still being rejected — a widened CHECK that accepts anything would
    "pass" this file's first test while removing the protection
  - replay safety, since src/pg.py re-runs files and these fixtures
    execute the whole sql/ directory on every run

SKIPS unless a reachable, migratable test database is provided via
VEO_TEST_PG_DSN (same contract as tests/test_ride_hard_caps_pg.py).
NEVER point that at production: the fixture executes every migration.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

psycopg = pytest.importorskip("psycopg")

from src import api_auth  # noqa: E402
from src.accounts import hash_token  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

# Every value src/accounts.py:mint_session can be called with today, plus
# the one sql/042 pre-authorizes for the SMS door. Kept as a literal list
# rather than derived from the code so that adding a mint call with a new
# method fails HERE, loudly, instead of in production at 2am.
_MINTABLE_METHODS = ("google", "magic_link", "email_code", "sms_code")

_TEST_EMAIL_LIKE = "pgtest-method-%@example.com"


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
        pytest.skip("VEO_TEST_PG_DSN not set — auth method Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM accounts WHERE email LIKE %s", (_TEST_EMAIL_LIKE,))
        cur.execute("DELETE FROM login_codes WHERE email LIKE %s", (_TEST_EMAIL_LIKE,))
    conn.commit()

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_auth, "connection", _fake_connection)
    # Rate limiting is exercised by its own tests; here it would just make
    # a repeated-run fixture flaky.
    monkeypatch.setattr(api_auth, "enforce", lambda cur, **kw: None)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(api_auth.router)
    # auth_sessions.issued_ip is INET, and TestClient reports the client
    # host as the literal string "testclient". Production always arrives
    # through the Cloudflare Tunnel with CF-Connecting-IP set
    # (src/client_ip.py), so setting it here is the representative path,
    # not a workaround.
    return TestClient(app, headers={"CF-Connecting-IP": "203.0.113.7"})


def _replay(pg_conn, filename: str = "042_auth_session_methods.sql") -> None:
    with pg_conn.cursor() as cur:
        cur.execute((SQL_DIR / filename).read_text())
    pg_conn.commit()


def test_emailed_code_door_mints_a_session(pg_conn, monkeypatch):
    """The regression: POST /auth/code -> /auth/code/verify used to 500 on
    the final INSERT because 'email_code' wasn't an allowed method."""
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(api_auth, "postmark_credentials",
                        lambda: {"token": "t", "sender": "s@example.com"})
    monkeypatch.setattr(api_auth, "send_login_code",
                        lambda email, code: sent.append((email, code)))

    email = f"pgtest-method-{uuid.uuid4()}@example.com"
    c = _client()

    r = c.post("/api/v1/auth/code", json={"email": email})
    assert r.status_code == 202, r.text
    assert len(sent) == 1
    _, code = sent[0]

    r = c.post("/api/v1/auth/code/verify", json={"email": email, "code": code})
    assert r.status_code == 200, r.text
    token = r.json()["token"]

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT method FROM auth_sessions WHERE token_sha256 = %s",
            (hash_token(token),),
        )
        row = cur.fetchone()
    assert row is not None, "verify returned a token but stored no session"
    assert row[0] == "email_code"


def test_every_mintable_method_is_accepted(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (email) VALUES (%s) RETURNING id",
            (f"pgtest-method-{uuid.uuid4()}@example.com",),
        )
        (account_id,) = cur.fetchone()
        for method in _MINTABLE_METHODS:
            cur.execute(
                """
                INSERT INTO auth_sessions (token_sha256, account_id, method, expires_at)
                VALUES (%s, %s, %s, NOW() + INTERVAL '1 day')
                """,
                (f"{method}-{uuid.uuid4()}", account_id, method),
            )
    pg_conn.commit()


def test_unknown_method_is_still_rejected(pg_conn):
    """Widening the list must not mean removing it."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (email) VALUES (%s) RETURNING id",
            (f"pgtest-method-{uuid.uuid4()}@example.com",),
        )
        (account_id,) = cur.fetchone()
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                """
                INSERT INTO auth_sessions (token_sha256, account_id, method, expires_at)
                VALUES (%s, %s, 'carrier_pigeon', NOW() + INTERVAL '1 day')
                """,
                (str(uuid.uuid4()), account_id),
            )
    pg_conn.rollback()


def test_replay_keeps_the_widened_list(pg_conn):
    """src/pg.py re-runs files; a later widening must not be reverted by
    this file running again afterwards."""
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            ALTER TABLE auth_sessions DROP CONSTRAINT auth_sessions_method_allowed;
            ALTER TABLE auth_sessions ADD CONSTRAINT auth_sessions_method_allowed
                CHECK (method IN ('google', 'magic_link', 'email_code',
                                  'sms_code', 'future_door'));
            """
        )
    pg_conn.commit()

    _replay(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
             WHERE conname = 'auth_sessions_method_allowed'
               AND conrelid = 'auth_sessions'::regclass
            """
        )
        (definition,) = cur.fetchone()
    assert "future_door" in definition, "replay reverted a later widening"
    for method in _MINTABLE_METHODS:
        assert method in definition
