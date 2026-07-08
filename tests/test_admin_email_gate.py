"""require_admin gates on ADMIN_EMAILS membership via EITHER sign-in door.

Magic-link operators must reach /api/v1/private/* — the gate is email
membership, not the Google-only `admin` scope.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from src import accounts
from src.accounts import SessionUser, is_admin_email, require_admin

_ADMINS = frozenset({"z@neill.io"})


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch):
    monkeypatch.setattr(accounts, "admin_emails", lambda: _ADMINS)


def _user(email: str, method: str = "magic_link", scopes=("rider",)) -> SessionUser:
    return SessionUser(
        account_id=1, email=email, scopes=scopes, supporter=False,
        expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc), sliding=True,
        method=method, token_sha256="x" * 64,
    )


# ---------- is_admin_email ----------------------------------------------------
def test_is_admin_email_membership():
    assert is_admin_email(_user("z@neill.io")) is True
    assert is_admin_email(_user("Z@Neill.IO")) is True          # case-insensitive
    assert is_admin_email(_user("rider@example.com")) is False


def test_is_admin_email_ignores_scope():
    """A magic-link session has no admin scope but is still admin by email."""
    u = _user("z@neill.io", method="magic_link", scopes=("rider",))
    assert "admin" not in u.scopes
    assert is_admin_email(u) is True


# ---------- require_admin -----------------------------------------------------
def _patch_session(monkeypatch, user):
    monkeypatch.setattr(accounts, "require_session", lambda request: user)


def test_require_admin_allows_magic_link_admin(monkeypatch):
    _patch_session(monkeypatch, _user("z@neill.io", method="magic_link"))
    assert require_admin(request=None).email == "z@neill.io"  # no raise


def test_require_admin_allows_google_admin(monkeypatch):
    _patch_session(monkeypatch, _user("z@neill.io", method="google", scopes=("rider", "admin")))
    assert require_admin(request=None).email == "z@neill.io"


def test_require_admin_rejects_non_admin(monkeypatch):
    _patch_session(monkeypatch, _user("rider@example.com"))
    with pytest.raises(HTTPException) as e:
        require_admin(request=None)
    assert e.value.status_code == 403
