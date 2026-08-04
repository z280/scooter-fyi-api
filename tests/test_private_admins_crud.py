"""/api/v1/private/admins — CRUD on the admin allowlist from a rider session.

The gate is require_admin (allowlist membership), so these routes let an
account admin grant and revoke account admin. That is deliberate and it is a
change from the GitHub-gated portal at /admin/admins, where a GitHub operator
decided who counted. The two properties that keep it survivable are tested
here: every write is attributed (`added_by`), and the last admin cannot be
removed — an empty allowlist locks every account out of /private/* including
this endpoint, recoverable only out of band.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_private
from src.accounts import SessionUser, require_admin

_ME = "boss@example.com"
_OTHER = "second@example.com"


def _user(email: str = _ME) -> SessionUser:
    return SessionUser(
        account_id=7, email=email, scopes=("rider", "admin"),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        sliding=False, method="google", token_sha256="x",
    )


class _Allowlist:
    """In-memory stand-in for the admin_allowlist table."""

    def __init__(self, *emails: str):
        self.rows = [
            {"email": e, "added_by": "cli", "added_at": "2026-07-01T00:00:00+00:00"}
            for e in emails
        ]
        self.calls: list[tuple[str, str, str | None]] = []

    def list_admins(self):
        return [dict(r) for r in self.rows]

    def admin_emails(self, cur=None):
        return frozenset(r["email"] for r in self.rows)

    def add_admin(self, email, added_by):
        norm = email.strip().lower()
        if "@" not in norm or norm.startswith("@") or norm.endswith("@"):
            raise ValueError(email)
        self.calls.append(("add", norm, added_by))
        if any(r["email"] == norm for r in self.rows):
            return False
        self.rows.append({"email": norm, "added_by": added_by,
                          "added_at": "2026-08-04T00:00:00+00:00"})
        return True

    def remove_admin(self, email):
        norm = email.strip().lower()
        self.calls.append(("remove", norm, None))
        before = len(self.rows)
        self.rows = [r for r in self.rows if r["email"] != norm]
        return len(self.rows) != before


@pytest.fixture
def allow(monkeypatch):
    book = _Allowlist(_ME, _OTHER)
    monkeypatch.setattr(api_private.accounts, "list_admins", book.list_admins)
    monkeypatch.setattr(api_private.accounts, "admin_emails", book.admin_emails)
    monkeypatch.setattr(api_private.accounts, "add_admin", book.add_admin)
    monkeypatch.setattr(api_private.accounts, "remove_admin", book.remove_admin)
    # The rate limiter needs a live cursor; these routes are covered for
    # limits by the shared ratelimit tests, not here.
    monkeypatch.setattr(api_private, "connection", _fake_connection)
    monkeypatch.setattr(api_private, "enforce", lambda cur, **kw: None)
    return book


class _FakeCursor:
    def execute(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def commit(self):
        pass


def _fake_connection():
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        yield _FakeConn()

    return _cm()


def _client(user: SessionUser | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(api_private.router)
    app.dependency_overrides[require_admin] = lambda: user or _user()
    return TestClient(app)


def test_list_marks_which_row_is_you(allow):
    body = _client().get("/api/v1/private/admins").json()
    assert body["count"] == 2
    mine = [a for a in body["admins"] if a["is_you"]]
    assert [a["email"] for a in mine] == [_ME]


def test_is_you_is_computed_with_the_allowlist_normalization(allow):
    """A session whose email differs only in case is still you — the client
    must not have to reimplement normalize_email to know which row is
    dangerous to remove."""
    body = _client(_user("BOSS@Example.com")).get("/api/v1/private/admins").json()
    assert [a["email"] for a in body["admins"] if a["is_you"]] == [_ME]


def test_add_records_who_did_it(allow):
    r = _client().post("/api/v1/private/admins", json={"email": "New@Example.com"})
    assert r.status_code == 200, r.text
    assert r.json()["added"] is True
    assert r.json()["email"] == "new@example.com"
    assert ("add", "new@example.com", _ME) in allow.calls
    # The response carries the fresh list so the UI never re-fetches to redraw.
    assert r.json()["count"] == 3


def test_adding_an_existing_admin_is_a_satisfied_no_op(allow):
    r = _client().post("/api/v1/private/admins", json={"email": _OTHER})
    assert r.status_code == 200
    assert r.json()["added"] is False
    assert r.json()["count"] == 2


def test_add_rejects_a_non_email(allow):
    r = _client().post("/api/v1/private/admins", json={"email": "not-an-email"})
    assert r.status_code == 400


def test_remove_takes_the_address_in_the_query_string(allow):
    r = _client().delete(f"/api/v1/private/admins?email={_OTHER}")
    assert r.status_code == 200, r.text
    assert r.json()["removed"] is True
    assert r.json()["count"] == 1


def test_removing_yourself_is_allowed_while_someone_else_remains(allow):
    """Stepping down is legitimate. It is only the empty room that is not."""
    r = _client().delete(f"/api/v1/private/admins?email={_ME}")
    assert r.status_code == 200
    assert r.json()["removed"] is True


def test_the_last_admin_cannot_be_removed(allow):
    allow.rows = [r for r in allow.rows if r["email"] == _ME]
    r = _client().delete(f"/api/v1/private/admins?email={_ME}")
    assert r.status_code == 409
    assert "last admin" in r.json()["detail"]
    # And nothing was deleted on the way to refusing.
    assert allow.admin_emails() == frozenset({_ME})


def test_removing_a_stranger_is_not_a_lockout_risk(allow):
    """The guard keys on 'is this the last ROW', not 'did you ask for one' —
    removing an address that isn't on the list at all is a plain no-op."""
    allow.rows = [r for r in allow.rows if r["email"] == _ME]
    r = _client().delete("/api/v1/private/admins?email=ghost@example.com")
    assert r.status_code == 200
    assert r.json()["removed"] is False
    assert allow.admin_emails() == frozenset({_ME})


def test_every_route_needs_an_admin_session():
    app = FastAPI()
    app.include_router(api_private.router)
    c = TestClient(app)
    assert c.get("/api/v1/private/admins").status_code == 401
    assert c.post("/api/v1/private/admins", json={"email": "a@b.co"}).status_code == 401
    assert c.delete("/api/v1/private/admins?email=a@b.co").status_code == 401
