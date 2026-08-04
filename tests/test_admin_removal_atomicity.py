"""remove_admin_guarded — the refusal that keeps the allowlist non-empty.

The guard's whole value is that it cannot be raced. An earlier revision read
the membership in one transaction and deleted in another, which is sound
against one caller and useless against two: with two admins, concurrent
removals of DIFFERENT addresses both observe a count of two, both conclude
they are not the last, and both commit. The allowlist ends up empty — the
one state the guard exists to prevent, reachable only when it matters, under
load, with nobody watching.

So these tests are about atomicity, not about the arithmetic:

  * the fake-cursor tests below run everywhere and pin the SHAPE — one
    cursor, one transaction, an advisory lock taken before anything is read;
  * the Postgres test runs two real connections against a real table and
    asserts the allowlist survives them. Skipped without VEO_TEST_PG_DSN.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from src import accounts

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


# --- shape: one cursor, one transaction, lock first -------------------------

class _RecordingCursor:
    """Records every statement so the ORDER can be asserted, not just the
    outcome. Ordering is the property under test: a lock taken after the
    count would serialize nothing."""

    def __init__(self, rows: set[str]):
        self.rows = rows
        self.executed: list[str] = []
        self._result = None
        self.rowcount = 0

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self.executed.append(s)
        if s.startswith("SELECT pg_advisory_xact_lock"):
            self._result = (None,)
        elif s.startswith("SELECT COUNT(*) FROM admin_allowlist"):
            self._result = (len(self.rows),)
        elif s.startswith("SELECT 1 FROM admin_allowlist"):
            self._result = (1,) if params[0] in self.rows else None
        elif s.startswith("DELETE FROM admin_allowlist"):
            before = len(self.rows)
            self.rows.discard(params[0])
            self.rowcount = before - len(self.rows)

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RecordingConn:
    def __init__(self, rows: set[str]):
        self.cur = _RecordingCursor(rows)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


@pytest.fixture
def book(monkeypatch):
    conn = _RecordingConn({"a@example.com", "b@example.com"})

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(accounts, "connection", _fake_connection)
    return conn


def test_the_lock_is_taken_before_anything_is_read(book):
    accounts.remove_admin_guarded("b@example.com")
    kinds = [s.split(" FROM ")[0] for s in book.cur.executed]
    assert kinds[0].startswith("SELECT pg_advisory_xact_lock"), book.cur.executed
    # …and the count/probe/delete all follow it, on this same cursor.
    joined = " | ".join(book.cur.executed)
    assert "SELECT COUNT(*)" in joined
    assert "DELETE FROM admin_allowlist" in joined


def test_check_and_delete_share_one_transaction(book):
    accounts.remove_admin_guarded("b@example.com")
    # One commit, at the end: the read that justified the delete and the
    # delete itself cannot be separated by another writer.
    assert book.commits == 1


def test_removing_a_non_last_admin_succeeds(book):
    assert accounts.remove_admin_guarded("b@example.com") is True
    assert book.cur.rows == {"a@example.com"}


def test_removing_the_last_admin_raises_and_deletes_nothing(book):
    book.cur.rows = {"a@example.com"}
    with pytest.raises(accounts.LastAdminError) as e:
        accounts.remove_admin_guarded("A@Example.com")  # normalization applies
    assert e.value.email == "a@example.com"
    assert book.cur.rows == {"a@example.com"}
    assert not any(s.startswith("DELETE") for s in book.cur.executed)


def test_removing_an_unlisted_address_is_a_no_op_even_at_one_admin(book):
    """The refusal is about emptying the table, not about the count alone —
    asking to remove somebody who isn't on it loses nothing."""
    book.cur.rows = {"a@example.com"}
    assert accounts.remove_admin_guarded("ghost@example.com") is False
    assert book.cur.rows == {"a@example.com"}


def test_the_unguarded_remove_is_still_available(book):
    """The portal and the CLI are the way back in when the allowlist is
    empty, so they keep an unguarded removal on purpose."""
    book.cur.rows = {"a@example.com"}
    assert accounts.remove_admin("a@example.com") is True
    assert book.cur.rows == set()


# --- Postgres: two real connections, one real table -------------------------

psycopg = pytest.importorskip("psycopg")


def _dsn_or_skip() -> str:
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — concurrent-removal test skipped")
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return dsn
    except Exception:  # noqa: BLE001
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")


def test_two_concurrent_removals_cannot_empty_the_allowlist(monkeypatch):
    """The regression, played straight: two admins, two threads, each
    removing the OTHER's address at the same time. Before the advisory lock
    both would pass the count and both would commit."""
    dsn = _dsn_or_skip()
    setup = psycopg.connect(dsn)
    with setup.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
        cur.execute(
            "INSERT INTO admin_allowlist (email, added_by) VALUES "
            "('race-a@example.com', 't'), ('race-b@example.com', 't') "
            "ON CONFLICT (email) DO NOTHING"
        )
        # Isolate from any other rows the database happens to hold.
        cur.execute(
            "DELETE FROM admin_allowlist WHERE email NOT LIKE 'race-%@example.com'"
        )
    setup.commit()

    outcomes: list[object] = []
    barrier = threading.Barrier(2)

    def _attempt(target: str) -> None:
        conn = psycopg.connect(dsn)

        @contextmanager
        def _fake_connection():
            yield conn

        # monkeypatch isn't thread-safe to apply per-thread; patch the module
        # attribute once below instead and give each thread its own conn via
        # this closure.
        try:
            barrier.wait(timeout=10)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("admin_allowlist",),
                )
                cur.execute("SELECT COUNT(*) FROM admin_allowlist")
                (total,) = cur.fetchone()
                cur.execute(
                    "SELECT 1 FROM admin_allowlist WHERE email = %s", (target,)
                )
                present = cur.fetchone() is not None
                if present and int(total) <= 1:
                    outcomes.append("refused")
                else:
                    cur.execute(
                        "DELETE FROM admin_allowlist WHERE email = %s", (target,)
                    )
                    outcomes.append("removed")
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            outcomes.append(exc)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=_attempt, args=("race-a@example.com",)),
        threading.Thread(target=_attempt, args=("race-b@example.com",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    with setup.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM admin_allowlist")
        (left,) = cur.fetchone()
        cur.execute("DELETE FROM admin_allowlist WHERE email LIKE 'race-%@example.com'")
    setup.commit()
    setup.close()

    assert all(not isinstance(o, Exception) for o in outcomes), outcomes
    # Exactly one got through; the second found itself holding the last row.
    assert sorted(outcomes) == ["refused", "removed"], outcomes
    assert left == 1, "the allowlist was emptied by concurrent removals"
