"""GET /api/v1/auth/config — public sign-in capabilities for the frontend.

Lets the frontend render the right sign-in doors and initialize Google
Identity Services from one source of truth (the client id is public — it
only names the audience).
"""

from __future__ import annotations

from fastapi import Response

from src import api_auth


def test_config_reports_google_and_magic_enabled(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "abc.apps.googleusercontent.com")
    monkeypatch.setenv("POSTMARK_TOKEN", "tok")
    monkeypatch.setenv("POSTMARK_FROM", "signin@scooter.fyi")
    out = api_auth.auth_config(Response())
    assert out["google_client_id"] == "abc.apps.googleusercontent.com"
    assert out["google_enabled"] is True
    assert out["magic_link_enabled"] is True


def test_config_reports_disabled_when_unconfigured(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("POSTMARK_TOKEN", raising=False)
    monkeypatch.delenv("POSTMARK_FROM", raising=False)
    out = api_auth.auth_config(Response())
    assert out["google_client_id"] is None
    assert out["google_enabled"] is False
    assert out["magic_link_enabled"] is False


def test_config_partial_google_only(monkeypatch):
    """Google configured, magic-link not — the doors are independent."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.delenv("POSTMARK_TOKEN", raising=False)
    monkeypatch.delenv("POSTMARK_FROM", raising=False)
    out = api_auth.auth_config(Response())
    assert out["google_enabled"] is True
    assert out["magic_link_enabled"] is False


def test_config_sets_cache_header(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    resp = Response()
    api_auth.auth_config(resp)
    assert resp.headers["cache-control"] == "public, max-age=300"
