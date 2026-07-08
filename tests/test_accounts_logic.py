"""Session-model rules: scope derivation, expiry policy, allowlist parsing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import accounts
from src.accounts import (
    hash_token,
    normalize_email,
    session_expiry,
    session_scopes,
)

_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


# ---------- scope derivation -------------------------------------------------
# The admin allowlist now lives in Postgres (admin_allowlist table); the CRUD
# + membership query are covered in test_admin_allowlist.py. Here we only
# exercise the pure scope-derivation logic, stubbing the allowlist.
def _allow(monkeypatch, *emails):
    monkeypatch.setattr(accounts, "admin_emails", lambda: frozenset(emails))


def test_google_allowlisted_email_gets_admin(monkeypatch):
    _allow(monkeypatch, "zneill@gmail.com")
    assert session_scopes(method="google", email="ZNeill@gmail.com") == ["rider", "admin"]


def test_google_other_email_is_rider_only(monkeypatch):
    _allow(monkeypatch, "zneill@gmail.com")
    assert session_scopes(method="google", email="rando@example.com") == ["rider"]


def test_magic_link_never_gets_admin_scope_even_for_allowlisted_email(monkeypatch):
    """The `admin` *scope* is still Google-only (signal only — access is
    gated by ADMIN_EMAILS membership in require_admin, not the scope)."""
    _allow(monkeypatch, "zneill@gmail.com")
    assert session_scopes(method="magic_link", email="zneill@gmail.com") == ["rider"]


# ---------- expiry policy ----------------------------------------------------
def test_rider_session_is_30d_sliding():
    expires, sliding = session_expiry(scopes=["rider"], now=_NOW)
    assert expires == _NOW + timedelta(days=30)
    assert sliding is True


def test_admin_session_is_24h_fixed():
    expires, sliding = session_expiry(scopes=["rider", "admin"], now=_NOW)
    assert expires == _NOW + timedelta(hours=24)
    assert sliding is False


# ---------- token hashing ----------------------------------------------------
def test_hash_token_is_sha256_hex():
    d = hash_token("abc")
    assert len(d) == 64 and d == hash_token("abc") and d != hash_token("abd")


def test_normalize_email():
    assert normalize_email("  Foo@Bar.COM ") == "foo@bar.com"
