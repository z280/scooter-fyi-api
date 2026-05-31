"""real_client_ip() header preference order.

The production deploy is behind Cloudflare Tunnel, so `request.client.host`
is the cloudflared sidecar's loopback — not the real reporter. The helper
must prefer CF-Connecting-IP, then X-Forwarded-For, then fall back.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.client_ip import real_client_ip


@dataclass
class _Client:
    host: str | None


class _FakeRequest:
    """Minimal stand-in for Starlette's Request — only the attributes the
    helper touches. Lets us unit-test without spinning up FastAPI."""

    def __init__(self, headers: dict[str, str] | None = None, client_host: str | None = "10.0.0.1"):
        self.headers = headers or {}
        self.client = _Client(host=client_host) if client_host else None


def test_prefers_cf_connecting_ip_above_all():
    req = _FakeRequest(
        headers={
            "cf-connecting-ip": "203.0.113.42",
            "x-forwarded-for": "198.51.100.1, 198.51.100.2",
        },
        client_host="10.0.0.1",
    )
    assert real_client_ip(req) == "203.0.113.42"


def test_falls_back_to_xff_first_value():
    """XFF is 'client, proxy1, proxy2' — leftmost is the original client."""
    req = _FakeRequest(
        headers={"x-forwarded-for": "198.51.100.1, 198.51.100.2"},
        client_host="10.0.0.1",
    )
    assert real_client_ip(req) == "198.51.100.1"


def test_xff_with_single_value():
    req = _FakeRequest(headers={"x-forwarded-for": "198.51.100.7"}, client_host="10.0.0.1")
    assert real_client_ip(req) == "198.51.100.7"


def test_falls_back_to_client_host_when_no_headers():
    req = _FakeRequest(headers={}, client_host="10.0.0.1")
    assert real_client_ip(req) == "10.0.0.1"


def test_returns_none_when_no_signal_at_all():
    req = _FakeRequest(headers={}, client_host=None)
    assert real_client_ip(req) is None


def test_strips_whitespace_in_cf_header():
    req = _FakeRequest(headers={"cf-connecting-ip": "  203.0.113.42  "}, client_host="10.0.0.1")
    assert real_client_ip(req) == "203.0.113.42"


def test_empty_xff_is_ignored_falls_through_to_client():
    req = _FakeRequest(headers={"x-forwarded-for": " , "}, client_host="10.0.0.1")
    assert real_client_ip(req) == "10.0.0.1"
