"""Postgres-backed integration coverage for the parts of the
public_username system a fake cursor can't meaningfully exercise:
upsert_account's (xmax = 0) insert/update branch, assign_public_
username's real UNIQUE-constraint interaction, the generated
public_username column, and sql/026's duplicate-email cleanup DO block.

SKIPS unless a reachable, migratable test database is provided via
VEO_TEST_PG_DSN, e.g.:

    VEO_TEST_PG_DSN='postgresql://testuser@/veo_test?host=/tmp/veopg_XXXXX&port=5544' pytest \\
        tests/test_accounts_username_pg.py -v

(see tests/test_daily_trips_rollup_pg.py for the same pattern).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from src import accounts  # noqa: E402  (after importorskip by design)

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


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
        pytest.skip("VEO_TEST_PG_DSN not set — public_username Postgres integration test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("DELETE FROM accounts WHERE email LIKE 'pgtest-%@example.com'")
    conn.commit()

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(accounts, "connection", _fake_connection)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def test_upsert_account_assigns_username_only_on_first_insert(pg_conn):
    email = "pgtest-new@example.com"
    with pg_conn.cursor() as cur:
        account_id = accounts.upsert_account(cur, email)
        cur.execute("SELECT public_username FROM accounts WHERE id = %s", (account_id,))
        first_username = cur.fetchone()[0]
    pg_conn.commit()
    assert first_username is not None

    with pg_conn.cursor() as cur:
        again_id = accounts.upsert_account(cur, email)
        cur.execute("SELECT public_username FROM accounts WHERE id = %s", (again_id,))
        second_username = cur.fetchone()[0]
    pg_conn.commit()
    assert again_id == account_id
    assert second_username == first_username


def test_two_new_accounts_get_different_usernames(pg_conn):
    with pg_conn.cursor() as cur:
        id_a = accounts.upsert_account(cur, "pgtest-a@example.com")
        id_b = accounts.upsert_account(cur, "pgtest-b@example.com")
        cur.execute("SELECT public_username FROM accounts WHERE id = %s", (id_a,))
        username_a = cur.fetchone()[0]
        cur.execute("SELECT public_username FROM accounts WHERE id = %s", (id_b,))
        username_b = cur.fetchone()[0]
    pg_conn.commit()
    assert username_a != username_b


def test_duplicate_public_username_is_rejected_by_the_db(pg_conn):
    with pg_conn.cursor() as cur:
        id_a = accounts.upsert_account(cur, "pgtest-c@example.com")
        id_b = accounts.upsert_account(cur, "pgtest-d@example.com")
        cur.execute("SELECT username_adjective, username_emoji FROM accounts WHERE id = %s", (id_a,))
        adjective, emoji = cur.fetchone()
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "UPDATE accounts SET username_adjective = %s, username_emoji = %s WHERE id = %s",
                (adjective, emoji, id_b),
            )
    # No pg_conn.commit() after an aborted statement — fixture teardown rolls back.


def test_choose_public_username_persists_a_specific_pair(pg_conn):
    with pg_conn.cursor() as cur:
        account_id = accounts.upsert_account(cur, "pgtest-e@example.com")
        cur.execute("SELECT word FROM sfw_adjectives LIMIT 1")
        (adjective,) = cur.fetchone()
        cur.execute("SELECT emoji FROM emoji_nouns LIMIT 1")
        (emoji,) = cur.fetchone()
        result = accounts.choose_public_username(cur, account_id, adjective=adjective, emoji=emoji)
        assert result == f"{adjective}{emoji}"
        cur.execute("SELECT public_username FROM accounts WHERE id = %s", (account_id,))
        assert cur.fetchone()[0] == f"{adjective}{emoji}"
    pg_conn.commit()


def test_choose_public_username_invalid_word_raises_before_any_write(pg_conn):
    with pg_conn.cursor() as cur:
        account_id = accounts.upsert_account(cur, "pgtest-f@example.com")
        cur.execute("SELECT public_username FROM accounts WHERE id = %s", (account_id,))
        before = cur.fetchone()[0]
        with pytest.raises(accounts.InvalidUsernameChoice):
            accounts.choose_public_username(cur, account_id, adjective="not-a-real-word", emoji=None)
        cur.execute("SELECT public_username FROM accounts WHERE id = %s", (account_id,))
        after = cur.fetchone()[0]
    pg_conn.commit()
    assert before == after


def test_dedup_migration_keeps_most_recently_active_duplicate(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_email_key")
        cur.execute(
            "INSERT INTO accounts (email, last_login_at) VALUES "
            "('pgtest-dupe@example.com', '2020-01-01T00:00:00Z') RETURNING id"
        )
        older_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO accounts (email, last_login_at) VALUES "
            "('pgtest-dupe@example.com', '2026-06-01T00:00:00Z') RETURNING id"
        )
        newer_id = cur.fetchone()[0]

        cur.execute((SQL_DIR / "026_dedup_account_emails.sql").read_text())

        cur.execute("SELECT id FROM accounts WHERE email = 'pgtest-dupe@example.com'")
        remaining = [r[0] for r in cur.fetchall()]

    assert remaining == [newer_id]
    assert older_id not in remaining
    # No commit — fixture teardown's conn.rollback() undoes the dropped
    # constraint and the manually-inserted duplicate rows.
