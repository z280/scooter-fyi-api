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
    # admin_emails takes an optional cursor now (session_scopes passes the one
    # it is already inside), so the stub has to accept it.
    monkeypatch.setattr(accounts, "admin_emails", lambda cur=None: frozenset(emails))


def test_google_allowlisted_email_gets_admin(monkeypatch):
    _allow(monkeypatch, "zneill@gmail.com")
    assert session_scopes(method="google", email="ZNeill@gmail.com") == ["rider", "admin"]


def test_google_other_email_is_rider_only(monkeypatch):
    _allow(monkeypatch, "zneill@gmail.com")
    assert session_scopes(method="google", email="rando@example.com") == ["rider"]


def test_every_door_gets_admin_for_an_allowlisted_email(monkeypatch):
    """The scope is AGNOSTIC to the door. It was Google-only, which made it
    disagree with is_admin_email — the check that actually authorizes, and
    which has always accepted either door because both prove ownership of
    the same allowlisted address. One address, one answer."""
    _allow(monkeypatch, "zneill@gmail.com")
    for method in ("google", "magic_link", "email_code", "sms"):
        assert session_scopes(method=method, email="zneill@gmail.com") == \
            ["rider", "admin"], method


def test_non_allowlisted_email_is_rider_only_on_every_door(monkeypatch):
    _allow(monkeypatch, "zneill@gmail.com")
    for method in ("google", "magic_link", "email_code", "sms"):
        assert session_scopes(method=method, email="rando@example.com") == \
            ["rider"], method


def test_phone_only_session_has_no_email_to_match(monkeypatch):
    """SMS sign-in on a phone-only account: the allowlist is keyed by email,
    so there is nothing to look up and None must not reach normalize_email."""
    _allow(monkeypatch, "zneill@gmail.com")
    assert session_scopes(method="sms", email=None) == ["rider"]


def test_the_allowlist_lookup_uses_a_cursor_it_was_handed(monkeypatch):
    """session_scopes runs inside mint_session's transaction. Checking out a
    second pooled connection while holding one is a deadlock vector on a pool
    of 8, so the cursor has to be threaded through rather than ignored."""
    seen = {}

    def _fake(cur=None):
        seen["cur"] = cur
        return frozenset({"zneill@gmail.com"})

    monkeypatch.setattr(accounts, "admin_emails", _fake)
    sentinel = object()
    session_scopes(method="google", email="zneill@gmail.com", cur=sentinel)
    assert seen["cur"] is sentinel


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
