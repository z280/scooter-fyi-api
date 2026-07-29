"""The z280-comms client: request shape, and one branch per documented
status code. The statuses are the whole contract — getting 409 wrong means
retrying at somebody who asked us to stop.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src import comms


class _Resp:
    """Minimal httpx.Response stand-in (status + body)."""

    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("COMMS_TOKEN", "tok-123")
    monkeypatch.delenv("COMMS_BASE_URL", raising=False)


def _capture(monkeypatch, resp):
    seen = {}

    def fake_request(method, url, **kw):
        seen["method"] = method
        seen["url"] = url
        seen["json"] = kw.get("json")
        seen["headers"] = kw.get("headers")
        return resp

    monkeypatch.setattr(comms.httpx, "request", fake_request)
    return seen


# ---------- credentials -------------------------------------------------------
def test_unconfigured_without_token(monkeypatch):
    monkeypatch.delenv("COMMS_TOKEN", raising=False)
    assert comms.comms_credentials() is None


def test_blank_token_is_unconfigured(monkeypatch):
    monkeypatch.setenv("COMMS_TOKEN", "   ")
    assert comms.comms_credentials() is None


def test_blank_base_url_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("COMMS_TOKEN", "tok")
    monkeypatch.setenv("COMMS_BASE_URL", "  ")
    assert comms.comms_credentials()["base_url"] == comms.DEFAULT_BASE_URL


def test_base_url_trailing_slash_stripped(monkeypatch):
    monkeypatch.setenv("COMMS_TOKEN", "tok")
    monkeypatch.setenv("COMMS_BASE_URL", "https://x.example/comms/")
    assert comms.comms_credentials()["base_url"] == "https://x.example/comms"


def test_send_requires_configuration(monkeypatch):
    monkeypatch.delenv("COMMS_TOKEN", raising=False)
    with pytest.raises(comms.CommsError):
        comms.send_sms("+13035551212", "hi", idempotency_key="k")


# ---------- request shape -----------------------------------------------------
def test_send_posts_documented_shape(configured, monkeypatch):
    seen = _capture(monkeypatch, _Resp(202, {"id": "m1", "transport": "t", "fell_back": False}))
    out = comms.send_sms(
        "+13035551212", "Use code AB123XY to login at denver.scooter.fyi",
        idempotency_key="login-code-7", ttl_seconds=120, urgent=True,
        metadata={"purpose": "sign_in"},
    )
    assert seen["method"] == "POST"
    assert seen["url"] == f"{comms.DEFAULT_BASE_URL}/v1/messages"
    assert seen["headers"]["Authorization"] == "Bearer tok-123"
    assert seen["json"] == {
        "to": "+13035551212",
        "body": "Use code AB123XY to login at denver.scooter.fyi",
        "channel": "sms",
        "urgent": True,
        "idempotency_key": "login-code-7",
        "metadata": {"purpose": "sign_in"},
        "ttl_seconds": 120,
    }
    assert out["id"] == "m1"


def test_ttl_omitted_when_none(configured, monkeypatch):
    seen = _capture(monkeypatch, _Resp(202, {"id": "m1"}))
    comms.send_sms("+13035551212", "hi", idempotency_key="k")
    assert "ttl_seconds" not in seen["json"]


def test_empty_body_refused_before_the_network(configured, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("must not reach the network")

    monkeypatch.setattr(comms.httpx, "request", explode)
    with pytest.raises(comms.CommsError):
        comms.send_sms("+13035551212", "   ", idempotency_key="k")


# ---------- status mapping ----------------------------------------------------
def test_409_raises_opted_out_with_the_verbatim_sentence(configured, monkeypatch):
    sentence = "Recipient blocked communications, text UNSTOP to +17202803332 to unblock."
    _capture(monkeypatch, _Resp(409, {"detail": {"error": "recipient_opted_out",
                                                 "detail": sentence}}))
    with pytest.raises(comms.OptedOut) as e:
        comms.send_sms("+13035551212", "hi", idempotency_key="k")
    # Verbatim: it names the exact keyword and number that actually unblock.
    assert str(e.value) == sentence


def test_409_with_an_unexpected_body_still_says_something_true(configured, monkeypatch):
    _capture(monkeypatch, _Resp(409, {"detail": []}))
    with pytest.raises(comms.OptedOut) as e:
        comms.send_sms("+13035551212", "hi", idempotency_key="k")
    assert "blocked" in str(e.value).lower()
    assert "{" not in str(e.value)  # never render raw JSON at a rider


@pytest.mark.parametrize(
    "status,exc",
    [
        (422, comms.UnusableRecipient),
        (429, comms.QuotaExceeded),
        (403, comms.CommsError),
        (502, comms.CommsError),
        (500, comms.CommsError),
    ],
)
def test_error_statuses_map_to_their_exceptions(configured, monkeypatch, status, exc):
    _capture(monkeypatch, _Resp(status, text="nope"))
    with pytest.raises(exc):
        comms.send_sms("+13035551212", "hi", idempotency_key="k")


def test_opted_out_is_a_comms_error_subclass():
    # Callers that only care about "the text didn't go" can catch the base.
    assert issubclass(comms.OptedOut, comms.CommsError)


def test_transport_failure_becomes_comms_error(configured, monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("no route to tailnet")

    monkeypatch.setattr(comms.httpx, "request", boom)
    with pytest.raises(comms.CommsError):
        comms.send_sms("+13035551212", "hi", idempotency_key="k")


def test_fell_back_is_still_a_success(configured, monkeypatch):
    # The message went out on the handset; delivery status is UNKNOWN, not
    # failed, so this must not raise.
    _capture(monkeypatch, _Resp(202, {"id": "m2", "fell_back": True}))
    assert comms.send_sms("+13035551212", "hi", idempotency_key="k")["fell_back"] is True


# ---------- replies -----------------------------------------------------------
def test_poll_returns_the_replies_list(configured, monkeypatch):
    seen = _capture(monkeypatch, _Resp(200, {"replies": [{"id": "r1"}, {"id": "r2"}]}))
    assert [r["id"] for r in comms.poll_replies(limit=25)] == ["r1", "r2"]
    assert seen["url"].endswith("/v1/replies?limit=25")


def test_poll_rejects_a_body_with_no_replies_list(configured, monkeypatch):
    _capture(monkeypatch, _Resp(200, {"nope": 1}))
    with pytest.raises(comms.CommsError):
        comms.poll_replies()


def test_ack_posts_to_the_reply(configured, monkeypatch):
    seen = _capture(monkeypatch, _Resp(200, {}))
    comms.ack_reply("r1")
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/v1/replies/r1/ack")
