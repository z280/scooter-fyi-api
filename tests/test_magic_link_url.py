"""Regression: the magic-link email must always carry a real, tokened link.

The reported bug — a sign-in email whose body had the intro and the expiry
disclaimer but a BLANK line where the link should be — traced to
MAGIC_LINK_URL_TEMPLATE being present-but-empty in the environment:
os.environ.get(key, default) returns "" for a set-but-empty key (default
only applies when the key is missing), and "".format(token=...) == "".
"""

from __future__ import annotations

import pytest

from src import api_auth, postmark
from src.postmark import PostmarkError

_TOKEN = "abc123-xyz_TOKEN"


def test_default_when_env_unset(monkeypatch):
    """With no env override, the config.json default supplies the URL."""
    monkeypatch.delenv("MAGIC_LINK_URL_TEMPLATE", raising=False)
    url = api_auth._magic_link_url(_TOKEN)
    assert url == f"https://denver.scooter.fyi/auth?ml={_TOKEN}"


def test_config_value_is_the_default(monkeypatch):
    """The default lives in config.json (non-secret config), not just code."""
    from src.config import load

    monkeypatch.delenv("MAGIC_LINK_URL_TEMPLATE", raising=False)
    assert load().accounts.magic_link_url_template == "https://denver.scooter.fyi/auth?ml={token}"


def test_default_when_env_set_but_empty(monkeypatch):
    """The actual bug: set-but-empty must fall back, not yield ''."""
    monkeypatch.setenv("MAGIC_LINK_URL_TEMPLATE", "")
    url = api_auth._magic_link_url(_TOKEN)
    assert url == f"https://denver.scooter.fyi/auth?ml={_TOKEN}"
    assert _TOKEN in url


def test_default_when_env_whitespace(monkeypatch):
    monkeypatch.setenv("MAGIC_LINK_URL_TEMPLATE", "   ")
    assert api_auth._magic_link_url(_TOKEN).endswith(f"ml={_TOKEN}")


def test_default_when_template_missing_token_placeholder(monkeypatch):
    monkeypatch.setenv("MAGIC_LINK_URL_TEMPLATE", "https://example.com/auth")
    url = api_auth._magic_link_url(_TOKEN)
    assert _TOKEN in url  # fell back to the tokened default


def test_custom_valid_template_is_used(monkeypatch):
    monkeypatch.setenv("MAGIC_LINK_URL_TEMPLATE", "https://staging.scooter.fyi/signin#{token}")
    assert api_auth._magic_link_url(_TOKEN) == f"https://staging.scooter.fyi/signin#{_TOKEN}"


# ---------- send-side guard ---------------------------------------------------
@pytest.fixture
def _creds(monkeypatch):
    monkeypatch.setattr(
        postmark, "postmark_credentials",
        lambda: {"token": "tok", "sender": "signin@scooter.fyi"},
    )
    # Any network attempt is a test failure — the guard must trip first.
    def _boom(*a, **k):
        raise AssertionError("httpx.post must not be reached for a bad link")
    monkeypatch.setattr(postmark.httpx, "post", _boom)


@pytest.mark.parametrize("bad", ["", "   ", "denver.scooter.fyi/auth?ml=x"])
def test_send_refuses_bad_link(_creds, bad):
    with pytest.raises(PostmarkError, match="bad link"):
        postmark.send_magic_link("z@neill.io", bad)


def test_send_accepts_good_link(monkeypatch):
    monkeypatch.setattr(
        postmark, "postmark_credentials",
        lambda: {"token": "tok", "sender": "signin@scooter.fyi"},
    )
    captured = {}

    class _Resp:
        status_code = 200
        text = "{}"

    def _post(url, json, headers, timeout):
        captured["body"] = json
        return _Resp()

    monkeypatch.setattr(postmark.httpx, "post", _post)
    link = "https://denver.scooter.fyi/auth?ml=realtoken"
    postmark.send_magic_link("z@neill.io", link)
    assert link in captured["body"]["TextBody"]
