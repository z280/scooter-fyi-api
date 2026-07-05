"""Stripe-Signature verification — the §4.1 trust boundary."""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from src.stripe_webhook import (
    StripeSignatureError,
    _handle_checkout_completed,
    _handle_subscription_event,
    _unix_to_dt,
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


# ---------- subscription (trial) path early-exit guards ---------------------
def test_subscription_checkout_missing_ids_is_ignored_without_db_write():
    """mode=subscription is routed away from the one-time payment_status
    check entirely — a trial checkout has payment_status=no_payment_required,
    which must never fall through to the paid/unpaid branch."""
    out = _handle_checkout_completed({
        "id": "cs_test_456", "client_reference_id": "1", "mode": "subscription",
        "payment_status": "no_payment_required",
    })
    assert out == "ignored_missing_subscription"


def test_subscription_checkout_bad_reference_is_ignored_without_db_write():
    out = _handle_checkout_completed({
        "id": "cs_test_456", "client_reference_id": "not-a-digit", "mode": "subscription",
        "subscription": "sub_123", "customer": "cus_123",
    })
    assert out == "ignored_bad_reference"


def test_subscription_event_missing_status_is_ignored_without_db_write():
    out = _handle_subscription_event({"id": "sub_123", "customer": "cus_123"})
    assert out == "ignored_incomplete_subscription_event"


def test_subscription_event_missing_customer_is_ignored_without_db_write():
    out = _handle_subscription_event({"id": "sub_123", "status": "trialing"})
    assert out == "ignored_incomplete_subscription_event"


def test_unix_to_dt_converts():
    dt = _unix_to_dt(1735689600)
    assert dt is not None and dt.tzinfo is not None


def test_unix_to_dt_none_passthrough():
    assert _unix_to_dt(None) is None


def test_unix_to_dt_garbage_is_none():
    assert _unix_to_dt("not-a-timestamp") is None
