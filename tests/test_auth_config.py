"""GET /api/v1/auth/config — public sign-in capabilities for the frontend.

Lets the frontend render the right sign-in doors and initialize Google
Identity Services from one source of truth (the client id is public — it
only names the audience).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException, Request, Response

from src import api_auth


def _dummy_request() -> Request:
    """A minimal ASGI Request — the /auth/google 503 guard fires before it's
    ever touched, so no client info is needed."""
    return Request({"type": "http", "method": "POST", "headers": []})


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


def test_google_force_disabled_hides_google(monkeypatch):
    """GOOGLE_AUTH_ENABLED=false forces Google off even with a client id set,
    and doesn't touch the (independent) magic-link / code doors."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_AUTH_ENABLED", "false")
    monkeypatch.setenv("POSTMARK_TOKEN", "tok")
    monkeypatch.setenv("POSTMARK_FROM", "signin@scooter.fyi")
    out = api_auth.auth_config(Response())
    assert out["google_enabled"] is False
    assert out["google_client_id"] is None
    assert out["magic_link_enabled"] is True
    assert out["code_enabled"] is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "  Off  "])
def test_google_auth_enabled_falsy_values(monkeypatch, value):
    monkeypatch.setenv("GOOGLE_AUTH_ENABLED", value)
    assert api_auth.google_auth_enabled() is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "anything"])
def test_google_auth_enabled_truthy_values(monkeypatch, value):
    monkeypatch.setenv("GOOGLE_AUTH_ENABLED", value)
    assert api_auth.google_auth_enabled() is True


def test_google_auth_enabled_defaults_on_when_unset(monkeypatch):
    monkeypatch.delenv("GOOGLE_AUTH_ENABLED", raising=False)
    assert api_auth.google_auth_enabled() is True


def test_auth_google_endpoint_503_when_force_disabled(monkeypatch):
    """The endpoint refuses (503) before any token work when the switch is
    off, even though a client id is present."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_AUTH_ENABLED", "off")
    with pytest.raises(HTTPException) as exc:
        api_auth.auth_google(_dummy_request(), api_auth.GoogleIn(credential="x" * 40))
    assert exc.value.status_code == 503
