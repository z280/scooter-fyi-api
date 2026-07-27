"""PUT /api/v1/profile (new phone_number/email/booleans/coords fields) and
the two username-mutation endpoints. GET/PUT's pre-existing rate_plan/
theme/favorites behavior isn't re-tested here — only what's new."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.errors import UniqueViolation
from psycopg.pq import DiagnosticField

from src import api_profile
from src.accounts import InvalidUsernameChoice, SessionUser, require_session

_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    supporter=False, expires_at=datetime.now(timezone.utc),
    sliding=True, method="google", token_sha256="x",
)

# _profile_payload's SELECT tuple shape:
# (email, phone_number, public_username, show_public_username,
#  show_in_leaderboards, rate_plan, theme, favorites, supporter,
#  home_lat, home_lng, work_lat, work_lng)
_PROFILE_ROW = (
    "rider@example.com", None, "brave🦉", True, True,
    "visitor", None, [], False, None, None, None, None,
)


def _unique_violation(constraint_name: str) -> UniqueViolation:
    return UniqueViolation(
        f'duplicate key value violates unique constraint "{constraint_name}"',
        info={DiagnosticField.CONSTRAINT_NAME: constraint_name.encode()},
    )


class _FakeCursor:
    def __init__(self, fetches, raise_on=None):
        self._fetches = list(fetches)
        self.executed: list[tuple[str, list]] = []
        self._raise_on = raise_on  # (sql_prefix, exception)

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if self._raise_on and normalized.startswith(self._raise_on[0]):
            raise self._raise_on[1]

    def fetchone(self):
        return self._fetches.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, fetches, raise_on=None):
        self._fetches = fetches
        self._raise_on = raise_on
        self.cur: _FakeCursor | None = None

    def cursor(self):
        self.cur = _FakeCursor(self._fetches, self._raise_on)
        return self.cur

    def commit(self):
        pass


def _app():
    app = FastAPI()
    app.include_router(api_profile.router)
    app.dependency_overrides[require_session] = lambda: _USER
    return app


def _put_client(monkeypatch, current_email, current_phone, raise_on=None):
    # Third item: maybe_credit_profile_completion's "already awarded?"
    # check, consumed only when the UPDATE succeeds (raise_on tests never
    # reach it — the exception propagates before that call). A truthy
    # value here means "already awarded", so it short-circuits with no
    # further queries — exactly one extra fetchone() either way.
    fetches = [(current_email, current_phone), (1,), _PROFILE_ROW]
    conn = _FakeConn(fetches, raise_on)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_profile, "connection", _fake_connection)
    monkeypatch.setattr(api_profile, "compute_badges", lambda cur, aid, supporter: [])
    return TestClient(_app()), conn


def _get_client(monkeypatch, row):
    conn = _FakeConn([row])

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_profile, "connection", _fake_connection)
    monkeypatch.setattr(api_profile, "compute_badges", lambda cur, aid, supporter: [])
    return TestClient(_app())


# ---------- GET --------------------------------------------------------------

def test_get_profile_includes_new_fields(monkeypatch):
    c = _get_client(monkeypatch, _PROFILE_ROW)
    r = c.get("/api/v1/profile")
    assert r.status_code == 200
    body = r.json()
    assert body["public_username"] == "brave🦉"
    assert body["show_public_username"] is True
    assert body["show_in_leaderboards"] is True
    assert body["home_lat"] is None


# ---------- PUT /api/v1/profile: phone/email ---------------------------------

def _the_update_call(conn):
    """Find the UPDATE accounts statement regardless of its position —
    robust against other queries (e.g. maybe_credit_profile_completion's
    check) being interleaved around it."""
    return next(c for c in conn.cur.executed if c[0].startswith("UPDATE accounts SET"))


def test_put_phone_number_is_normalized_and_saved(monkeypatch):
    c, conn = _put_client(monkeypatch, "rider@example.com", None)
    r = c.put("/api/v1/profile", json={"phone_number": "+1 303-555-1234"})
    assert r.status_code == 200
    update_sql, update_params = _the_update_call(conn)
    assert "phone_number = %s" in update_sql
    assert "+13035551234" in update_params


def test_put_rejects_malformed_phone_number(monkeypatch):
    c, _ = _put_client(monkeypatch, "rider@example.com", None)
    r = c.put("/api/v1/profile", json={"phone_number": "call-me-maybe"})
    assert r.status_code == 400
    assert "E.164" in r.json()["detail"]


def test_put_cannot_null_email_with_no_phone_on_file(monkeypatch):
    c, _ = _put_client(monkeypatch, "rider@example.com", None)
    r = c.put("/api/v1/profile", json={"email": None})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "email" in detail and "phone" in detail


def test_put_can_null_email_when_phone_already_on_file(monkeypatch):
    c, _ = _put_client(monkeypatch, "rider@example.com", "+13035551234")
    r = c.put("/api/v1/profile", json={"email": None})
    assert r.status_code == 200


def test_put_can_null_email_when_phone_added_in_same_request(monkeypatch):
    c, _ = _put_client(monkeypatch, "rider@example.com", None)
    r = c.put("/api/v1/profile", json={"email": None, "phone_number": "+13035551234"})
    assert r.status_code == 200


def test_put_rejects_invalid_email_format(monkeypatch):
    c, _ = _put_client(monkeypatch, "rider@example.com", None)
    r = c.put("/api/v1/profile", json={"email": "not-an-email"})
    assert r.status_code == 400


def test_put_maps_duplicate_phone_number_to_409(monkeypatch):
    c, _ = _put_client(
        monkeypatch, "rider@example.com", None,
        raise_on=("UPDATE accounts SET", _unique_violation("accounts_phone_number_key")),
    )
    r = c.put("/api/v1/profile", json={"phone_number": "+13035551234"})
    assert r.status_code == 409
    assert "phone" in r.json()["detail"]


def test_put_maps_duplicate_email_to_409(monkeypatch):
    c, _ = _put_client(
        monkeypatch, "rider@example.com", None,
        raise_on=("UPDATE accounts SET", _unique_violation("accounts_email_key")),
    )
    r = c.put("/api/v1/profile", json={"email": "taken@example.com"})
    assert r.status_code == 409
    assert "email" in r.json()["detail"]


# ---------- PUT /api/v1/profile: booleans + home/work coordinates ------------

def test_put_rejects_one_sided_home_coordinates(monkeypatch):
    c, _ = _put_client(monkeypatch, "rider@example.com", None)
    r = c.put("/api/v1/profile", json={"home_lat": 39.7})
    assert r.status_code == 400
    assert "home_lat" in r.json()["detail"]


def test_put_rejects_out_of_range_latitude(monkeypatch):
    c, _ = _put_client(monkeypatch, "rider@example.com", None)
    r = c.put("/api/v1/profile", json={"home_lat": 200, "home_lng": -104.9})
    assert r.status_code == 422


def test_put_accepts_paired_home_coordinates(monkeypatch):
    c, _ = _put_client(monkeypatch, "rider@example.com", None)
    r = c.put("/api/v1/profile", json={"home_lat": 39.74, "home_lng": -104.98})
    assert r.status_code == 200


def test_put_accepts_paired_null_home_coordinates_to_clear(monkeypatch):
    c, _ = _put_client(monkeypatch, "rider@example.com", None)
    r = c.put("/api/v1/profile", json={"home_lat": None, "home_lng": None})
    assert r.status_code == 200


def test_put_rejects_null_show_public_username(monkeypatch):
    c, _ = _put_client(monkeypatch, "rider@example.com", None)
    r = c.put("/api/v1/profile", json={"show_public_username": None})
    assert r.status_code == 400


def test_put_accepts_show_in_leaderboards_false(monkeypatch):
    c, conn = _put_client(monkeypatch, "rider@example.com", None)
    r = c.put("/api/v1/profile", json={"show_in_leaderboards": False})
    assert r.status_code == 200
    update_sql, update_params = _the_update_call(conn)
    assert "show_in_leaderboards = %s" in update_sql
    assert False in update_params


def test_put_newly_completing_the_profile_awards_points(monkeypatch):
    """A full walk of maybe_credit_profile_completion's own two queries
    (not-yet-awarded check, then the accounts completeness read) plus its
    credit_points INSERT — distinct from _put_client's shortcut fetch
    queue, which always stubs 'already awarded' to keep the other tests
    focused on their own behavior."""
    complete_accounts_row = ("rider@example.com", "resident", "+13035551234",
                              39.74, -104.98, None, None)
    fetches = [
        ("rider@example.com", None),   # initial email/phone read
        None,                          # not yet awarded
        complete_accounts_row,         # completeness check
        (55, datetime.now(timezone.utc)),  # credit_points INSERT...RETURNING
        _PROFILE_ROW,                  # final _profile_payload SELECT
    ]
    conn = _FakeConn(fetches)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_profile, "connection", _fake_connection)
    monkeypatch.setattr(api_profile, "compute_badges", lambda cur, aid, supporter: [])
    r = TestClient(_app()).put("/api/v1/profile", json={"phone_number": "+13035551234"})
    assert r.status_code == 200, r.text
    points_insert = next(c for c in conn.cur.executed if c[0].startswith("INSERT INTO user_points"))
    assert points_insert[1][:2] == (1, "profile_completion")


# ---------- POST /api/v1/profile/username/regenerate -------------------------

def test_regenerate_username_is_rate_limited_and_calls_assign(monkeypatch):
    calls = []
    conn = _FakeConn([])

    @contextmanager
    def _fake_connection():
        yield conn

    def _fake_enforce(cur, **kw):
        calls.append(kw)

    monkeypatch.setattr(api_profile, "connection", _fake_connection)
    monkeypatch.setattr(api_profile, "enforce", _fake_enforce)
    monkeypatch.setattr(api_profile, "assign_public_username", lambda cur, aid: "bold🦊")

    r = TestClient(_app()).post("/api/v1/profile/username/regenerate")
    assert r.status_code == 200
    assert r.json() == {"public_username": "bold🦊"}
    assert calls[0]["bucket"] == "profile_username_reroll_account"
    assert calls[0]["key"] == "1"
    assert calls[0]["limit"] == 10


# ---------- PUT /api/v1/profile/username (explicit choice) -------------------

def _username_client(monkeypatch, choose_fn):
    conn = _FakeConn([])

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_profile, "connection", _fake_connection)
    monkeypatch.setattr(api_profile, "enforce", lambda cur, **kw: None)
    monkeypatch.setattr(api_profile, "choose_public_username", choose_fn)
    return TestClient(_app())


def test_set_username_requires_at_least_one_field(monkeypatch):
    c = _username_client(monkeypatch, lambda cur, aid, **kw: "unused")
    r = c.put("/api/v1/profile/username", json={})
    assert r.status_code == 400


def test_set_username_success(monkeypatch):
    captured = {}

    def _choose(cur, aid, *, adjective, emoji):
        captured.update(account_id=aid, adjective=adjective, emoji=emoji)
        return "bold🦊"

    c = _username_client(monkeypatch, _choose)
    r = c.put("/api/v1/profile/username", json={"adjective": "bold", "emoji": "🦊"})
    assert r.status_code == 200
    assert r.json() == {"public_username": "bold🦊"}
    assert captured == {"account_id": 1, "adjective": "bold", "emoji": "🦊"}


def test_set_username_partial_adjective_only(monkeypatch):
    def _choose(cur, aid, *, adjective, emoji):
        assert adjective == "bold" and emoji is None
        return "bold🦉"

    c = _username_client(monkeypatch, _choose)
    r = c.put("/api/v1/profile/username", json={"adjective": "bold"})
    assert r.status_code == 200


def test_set_username_invalid_choice_maps_to_400(monkeypatch):
    def _choose(cur, aid, **kw):
        raise InvalidUsernameChoice("'wobbly' is not in the adjective list")

    c = _username_client(monkeypatch, _choose)
    r = c.put("/api/v1/profile/username", json={"adjective": "wobbly"})
    assert r.status_code == 400
    assert "wobbly" in r.json()["detail"]


def test_set_username_collision_maps_to_409(monkeypatch):
    def _choose(cur, aid, **kw):
        raise _unique_violation("accounts_public_username_key")

    c = _username_client(monkeypatch, _choose)
    r = c.put("/api/v1/profile/username", json={"adjective": "bold", "emoji": "🦊"})
    assert r.status_code == 409
