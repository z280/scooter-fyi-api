"""GET /api/v1/auth/session's `admin` field.

The field answers "is this session allowed to do admin things?", and the
answer is `is_admin_email` — the check require_admin actually enforces,
which accepts EITHER sign-in door.

It is deliberately NOT `"admin" in scopes`, for two reasons that outlast
this PR. The scope is a MINT-TIME snapshot, so it is stale for every
session issued before an allowlist change — including every session
issued before this PR, when the scope was Google-only and an allowlisted
operator signed in by magic link was admin to every endpoint while the UI
showed no Administrator Mode. And a live field is what makes a REVOKED
admin lose access at once rather than at token expiry.

This PR makes the scope door-agnostic going forward; the tests below
still cover a session that lacks it, because those sessions exist.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_auth
from src.accounts import SessionUser, require_session

_ADMIN_EMAIL = "boss@example.com"
_RIDER_EMAIL = "rider@example.com"


def _user(*, email: str | None, scopes: tuple[str, ...], method: str) -> SessionUser:
    return SessionUser(
        account_id=1, email=email, scopes=scopes,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        sliding=True, method=method, token_sha256="x",
    )


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch):
    monkeypatch.setattr(
        "src.accounts.admin_emails", lambda: frozenset({_ADMIN_EMAIL})
    )


def _session(user: SessionUser) -> dict:
    app = FastAPI()
    app.include_router(api_auth.router)
    app.dependency_overrides[require_session] = lambda: user
    r = TestClient(app).get("/api/v1/auth/session")
    assert r.status_code == 200, r.text
    return r.json()


def test_google_admin_is_admin():
    body = _session(_user(email=_ADMIN_EMAIL, scopes=("rider", "admin"),
                          method="google"))
    assert body["admin"] is True


def test_magic_link_admin_is_admin_even_without_the_scope():
    """The regression this file is named for, and the shape every session
    minted before this PR still has: an allowlisted email whose stored
    scopes carry no `admin`. require_admin would let it through, so the
    session endpoint has to say so rather than reading the stale snapshot."""
    body = _session(_user(email=_ADMIN_EMAIL, scopes=("rider",),
                          method="magic_link"))
    assert body["admin"] is True
    assert "admin" not in body["scopes"]  # the signal really is absent


def test_ordinary_rider_is_not_admin():
    body = _session(_user(email=_RIDER_EMAIL, scopes=("rider",),
                          method="magic_link"))
    assert body["admin"] is False


def test_phone_only_account_is_not_admin():
    """SMS sign-in with no email on file: the allowlist is keyed by email, so
    there is nothing to match — and None must not reach normalize_email."""
    body = _session(_user(email=None, scopes=("rider",), method="sms_code"))
    assert body["admin"] is False


def test_scopes_still_ship_verbatim():
    """A client that wants to know WHICH door was used can still tell; the
    new field answers a different question and replaces nothing."""
    body = _session(_user(email=_ADMIN_EMAIL, scopes=("rider", "admin"),
                          method="google"))
    assert body["scopes"] == ["rider", "admin"]
    assert body["email"] == _ADMIN_EMAIL
    assert "expires" in body
