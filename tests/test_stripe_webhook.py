"""Stripe-Signature verification — the §4.1 trust boundary."""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from src.stripe_webhook import (
    StripeSignatureError,
    _handle_checkout_completed,
    verify_stripe_signature,
)

SECRET = "whsec_test_secret"
BODY = b'{"type":"checkout.session.completed"}'


def _sign(body: bytes, secret: str = SECRET, t: int | None = None) -> str:
    t = t if t is not None else int(time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256)
    return f"t={t},v1={mac.hexdigest()}"


def test_valid_signature_passes():
    verify_stripe_signature(BODY, _sign(BODY), SECRET)


def test_multiple_v1_entries_any_match_passes():
    good = _sign(BODY)
    t, v1 = good.split(",")
    header = f"{t},v1={'0' * 64},{v1}"
    verify_stripe_signature(BODY, header, SECRET)


def test_wrong_secret_rejected():
    with pytest.raises(StripeSignatureError):
        verify_stripe_signature(BODY, _sign(BODY, secret="whsec_other"), SECRET)


def test_tampered_body_rejected():
    with pytest.raises(StripeSignatureError):
        verify_stripe_signature(b'{"type":"evil"}', _sign(BODY), SECRET)


def test_stale_timestamp_rejected():
    stale = int(time.time()) - 3600
    with pytest.raises(StripeSignatureError):
        verify_stripe_signature(BODY, _sign(BODY, t=stale), SECRET)


def test_future_timestamp_rejected():
    future = int(time.time()) + 3600
    with pytest.raises(StripeSignatureError):
        verify_stripe_signature(BODY, _sign(BODY, t=future), SECRET)


def test_missing_header_rejected():
    with pytest.raises(StripeSignatureError):
        verify_stripe_signature(BODY, None, SECRET)


def test_malformed_header_rejected():
    with pytest.raises(StripeSignatureError):
        verify_stripe_signature(BODY, "v1=deadbeef", SECRET)
    with pytest.raises(StripeSignatureError):
        verify_stripe_signature(BODY, "t=notanumber,v1=deadbeef", SECRET)


# ---------- _handle_checkout_completed early-exit guards --------------------
# Both cases below return before touching the database, so no connection
# fixture is needed — they exercise the review-flagged guards directly.
def test_checkout_missing_session_id_is_ignored_without_db_write():
    out = _handle_checkout_completed({"client_reference_id": "1", "payment_status": "paid"})
    assert out == "ignored_missing_session_id"


def test_checkout_unpaid_status_is_ignored_without_db_write():
    out = _handle_checkout_completed({
        "id": "cs_test_123", "client_reference_id": "1", "payment_status": "unpaid",
    })
    assert out == "ignored_unpaid"


def test_checkout_missing_payment_status_is_ignored_without_db_write():
    """A missing/unrecognized status must NOT be treated as paid."""
    out = _handle_checkout_completed({"id": "cs_test_123", "client_reference_id": "1"})
    assert out == "ignored_unpaid"
