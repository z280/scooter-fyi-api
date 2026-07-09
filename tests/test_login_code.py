"""Email code sign-in: POST /api/v1/auth/code + /api/v1/auth/code/verify.

Covers the pure helpers (format, normalization, keyed+email-bound hash) and
the request/verify flows against a stateful fake of the login_codes table.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src import api_auth

_SNAP = datetime(2026, 7, 6, 14, 30, tzinfo=timezone.utc)


def _request() -> Request:
    return Request({
        "type": "http", "method": "POST", "path": "/x",
        "headers": [(b"user-agent", b"pytest")], "query_string": b"",
    })


# ---------- pure helpers ------------------------------------------------------
def test_generate_code_format():
    for _ in range(500):
        c = api_auth._generate_code()
        assert api_auth._CODE_RE.match(c), c            # AA000AA
        assert "I" not in c and "O" not in c            # ambiguous letters excluded


def test_normalize_code_strips_and_uppercases():
    assert api_auth._normalize_code(" ab-123 xy ") == "AB123XY"
    assert api_auth._normalize_code("aa000aa") == "AA000AA"
    assert api_auth._normalize_code(None) == ""


def test_hash_code_is_deterministic_email_and_code_bound():
    h = api_auth._hash_code("Z@Neill.IO", "AB123XY")
    assert h == api_auth._hash_code("z@neill.io", "AB123XY")      # email normalized
    assert h != api_auth._hash_code("other@x.io", "AB123XY")      # bound to email
    assert h != api_auth._hash_code("z@neill.io", "AB123XZ")      # bound to code
    assert len(h) == 64                                          # sha256 hex


# ---------- shared fake DB ----------------------------------------------------
class _FakeCur:
    def __init__(self, state):
        self.state = state
        self._fetch = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.state.setdefault("sql", []).append((s, params))
        if s.startswith("INSERT INTO login_codes"):
            self.state["inserted"] = params            # (email, code_hash, expires_at, ip)
            self._fetch = None
        elif s.startswith("SELECT id, code_hash, attempts FROM login_codes"):
            self._fetch = self.state.get("row")
        elif "attempts = attempts + 1" in s:
            self.state["attempt_bumped"] = True
            self._fetch = None
        elif "RETURNING id" in s:                       # success single-use burn
            self.state["burned"] = True
            self._fetch = (1,) if self.state.get("burn_wins", True) else None
        elif "SET used_at = NOW() WHERE id = %s" in s:  # too-many-attempts burn
            self.state["burned_toomany"] = True
            self._fetch = None
        else:                                           # UPDATE-supersede, DELETE-prune
            self._fetch = None

    def fetchone(self):
        return self._fetch

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return _FakeCur(self.state)

    def commit(self):
        self.state["committed"] = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def env(monkeypatch):
    state: dict = {}

    @contextmanager
    def _conn():
        yield _FakeConn(state)

    monkeypatch.setattr(api_auth, "connection", _conn)
    monkeypatch.setattr(api_auth, "enforce", lambda *a, **k: None)  # no rate-limit DB
    monkeypatch.setattr(api_auth, "postmark_credentials", lambda: {"token": "t", "sender": "s"})
    monkeypatch.setattr(api_auth, "upsert_account", lambda cur, email: 42)
    monkeypatch.setattr(api_auth, "mint_session",
                        lambda cur, **kw: ("SESSIONTOKEN", _SNAP))
    return state


# ---------- request flow ------------------------------------------------------
def test_request_sends_code_and_stores_matching_hash(env, monkeypatch):
    sent = {}
    monkeypatch.setattr(api_auth, "send_login_code",
                        lambda email, code: sent.update(email=email, code=code))
    out = api_auth.auth_code_request(_request(), api_auth.CodeRequestIn(email="Z@Neill.IO"))
    assert out == {"sent": True}
    # emailed a well-formed code to the normalized address
    assert sent["email"] == "z@neill.io"
    assert api_auth._CODE_RE.match(sent["code"])
    # stored hash matches HMAC(email, emailed code) — not the code itself
    email, code_hash, _expires, _ip = env["inserted"]
    assert email == "z@neill.io"
    assert code_hash == api_auth._hash_code("z@neill.io", sent["code"])
    assert code_hash != sent["code"]


def test_request_503_when_postmark_unconfigured(env, monkeypatch):
    monkeypatch.setattr(api_auth, "postmark_credentials", lambda: None)
    with pytest.raises(HTTPException) as e:
        api_auth.auth_code_request(_request(), api_auth.CodeRequestIn(email="z@neill.io"))
    assert e.value.status_code == 503


def test_request_400_bad_email(env):
    with pytest.raises(HTTPException) as e:
        api_auth.auth_code_request(_request(), api_auth.CodeRequestIn(email="not-an-email"))
    assert e.value.status_code == 400


def test_request_502_on_postmark_failure(env, monkeypatch):
    from src.postmark import PostmarkError

    def _boom(email, code):
        raise PostmarkError("down")

    monkeypatch.setattr(api_auth, "send_login_code", _boom)
    with pytest.raises(HTTPException) as e:
        api_auth.auth_code_request(_request(), api_auth.CodeRequestIn(email="z@neill.io"))
    assert e.value.status_code == 502
    assert env.get("inserted") is not None  # row was committed before the send


# ---------- verify flow -------------------------------------------------------
def _verify(email="z@neill.io", code="AB123XY"):
    return api_auth.auth_code_verify(_request(), api_auth.CodeVerifyIn(email=email, code=code))


def test_verify_success_mints_session(env):
    env["row"] = (1, api_auth._hash_code("z@neill.io", "AB123XY"), 0)
    out = _verify()
    assert out["token"] == "SESSIONTOKEN"
    assert env.get("burned") is True


def test_verify_wrong_code_bumps_attempts_and_401s(env):
    env["row"] = (1, api_auth._hash_code("z@neill.io", "AB123XY"), 0)
    with pytest.raises(HTTPException) as e:
        _verify(code="ZZ999ZZ")
    assert e.value.status_code == 401
    assert env.get("attempt_bumped") is True
    assert env.get("burned") is not True


def test_verify_no_live_code_401s(env):
    env["row"] = None
    with pytest.raises(HTTPException) as e:
        _verify()
    assert e.value.status_code == 401


def test_verify_too_many_attempts_burns_and_401s(env):
    env["row"] = (1, api_auth._hash_code("z@neill.io", "AB123XY"), api_auth.MAX_CODE_ATTEMPTS)
    with pytest.raises(HTTPException) as e:
        _verify()  # even with the correct code
    assert e.value.status_code == 401
    assert env.get("burned_toomany") is True


def test_verify_is_email_scoped(env):
    # A code hash minted for a DIFFERENT email must not verify for this one.
    env["row"] = (1, api_auth._hash_code("someone@else.io", "AB123XY"), 0)
    with pytest.raises(HTTPException) as e:
        _verify(email="z@neill.io", code="AB123XY")
    assert e.value.status_code == 401


def test_verify_loses_single_use_race(env):
    env["row"] = (1, api_auth._hash_code("z@neill.io", "AB123XY"), 0)
    env["burn_wins"] = False  # a concurrent verify already burned it
    with pytest.raises(HTTPException) as e:
        _verify()
    assert e.value.status_code == 401
