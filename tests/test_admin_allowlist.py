"""admin_allowlist CRUD + membership query (the ADMIN_EMAILS replacement).

Drives accounts.add_admin / remove_admin / list_admins / admin_emails against
a stateful in-memory fake of the admin_allowlist table.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from src import accounts

_AT = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


class _FakeCursor:
    def __init__(self, store: dict):
        self.store = store           # email -> (added_by, added_at)
        self._result: list = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT email, added_by, added_at FROM admin_allowlist"):
            self._result = [(e, v[0], v[1]) for e, v in self.store.items()]
        elif s.startswith("SELECT email FROM admin_allowlist"):
            self._result = [(e,) for e in self.store]
        elif s.startswith("INSERT INTO admin_allowlist"):
            email, added_by = params
            if email in self.store:
                self.rowcount = 0            # ON CONFLICT DO NOTHING
            else:
                self.store[email] = (added_by, _AT)
                self.rowcount = 1
        elif s.startswith("DELETE FROM admin_allowlist"):
            (email,) = params
            self.rowcount = 1 if self.store.pop(email, None) is not None else 0
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchall(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _FakeCursor(self.store)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def store(monkeypatch):
    data: dict = {}

    @contextmanager
    def _conn():
        yield _FakeConn(data)

    monkeypatch.setattr(accounts, "connection", _conn)
    return data


def test_add_normalizes_and_is_idempotent(store):
    assert accounts.add_admin("  ZNeill@Gmail.com ", added_by="octocat") is True
    assert accounts.add_admin("zneill@gmail.com", added_by="someone") is False  # already present
    assert store == {"zneill@gmail.com": ("octocat", _AT)}  # stored normalized, first writer wins


def test_admin_emails_reads_table(store):
    accounts.add_admin("a@example.com", added_by="cli")
    accounts.add_admin("b@example.com", added_by="cli")
    assert accounts.admin_emails() == {"a@example.com", "b@example.com"}


def test_is_admin_email_uses_the_table(store):
    accounts.add_admin("z@neill.io", added_by="cli")
    admin = accounts.SessionUser(
        account_id=1, email="Z@Neill.IO", scopes=("rider",), expires_at=_AT, sliding=True, method="magic_link", token_sha256="x" * 64,
    )
    assert accounts.is_admin_email(admin) is True


def test_remove(store):
    accounts.add_admin("gone@example.com", added_by="cli")
    assert accounts.remove_admin("GONE@example.com") is True   # normalized match
    assert accounts.remove_admin("gone@example.com") is False  # already gone
    assert accounts.admin_emails() == frozenset()


def test_list_admins_shape(store):
    accounts.add_admin("a@example.com", added_by="octocat")
    rows = accounts.list_admins()
    assert rows == [{"email": "a@example.com", "added_by": "octocat", "added_at": _AT.isoformat()}]


@pytest.mark.parametrize("bad", ["", "   ", "notanemail", "@nope.com", "nope@"])
def test_add_rejects_non_email(store, bad):
    with pytest.raises(ValueError):
        accounts.add_admin(bad, added_by="cli")
