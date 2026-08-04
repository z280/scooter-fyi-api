"""GET /api/v1/auth/session's `admin` field.

The field answers "is this session allowed to do admin things?", and the
answer is `is_admin_email` — the check require_admin actually enforces,
which accepts EITHER sign-in door. It is deliberately NOT `"admin" in
scopes`: that scope is a Google-only signal (accounts.session_scopes) and
stopped gating access when is_admin_email became the authorization check.

The bug these tests exist for: an allowlisted operator signed in by magic
link was admin to every endpoint while the frontend, which had only the
scope to go on, showed no Administrator Mode and left the proximity-gated
map buttons blocked. Authorization said yes; the UI said no.
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
    """The regression this file is named for. Same allowlisted email, other
    door: no `admin` scope is ever stamped, but require_admin would let this
    session through — so the session endpoint has to say so."""
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
    body = _session(_user(email=None, scopes=("rider",), method="sms"))
    assert body["admin"] is False


def test_scopes_still_ship_verbatim():
    """A client that wants to know WHICH door was used can still tell; the
    new field answers a different question and replaces nothing."""
    body = _session(_user(email=_ADMIN_EMAIL, scopes=("rider", "admin"),
                          method="google"))
    assert body["scopes"] == ["rider", "admin"]
    assert body["email"] == _ADMIN_EMAIL
    assert "expires" in body
