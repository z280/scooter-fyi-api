"""Stripe supporter webhook (API_REQUIREMENTS.md §4.1).

POST /webhooks/stripe — the only Stripe surface in v1. The Payment Link
lives entirely on Stripe's side; we consume two event families:

    checkout.session.completed  → record the payment, set supporter
    charge.refunded             → on FULL refund, mark the payment
                                  refunded; supporter stays TRUE only
                                  while >= 1 non-refunded payment exists

Signature verification is implemented directly (HMAC-SHA256 over
"{t}.{raw_body}" against the v1 entries of the Stripe-Signature header,
constant-time compare, 5-minute timestamp tolerance) — no Stripe SDK
dependency for one webhook.

Idempotency: stripe_session_id is UNIQUE; webhook retries no-op.
Unknown event types return 200 so Stripe doesn't retry them forever.
A checkout without a usable client_reference_id also returns 200 (it
would never succeed on retry) — logged loudly instead.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .pg import connection

log = logging.getLogger(__name__)

router = APIRouter()

_TOLERANCE_S = 300


class StripeSignatureError(Exception):
    pass


def verify_stripe_signature(
    payload: bytes, header: str | None, secret: str, *, now: float | None = None
) -> None:
    """Raise StripeSignatureError unless a v1 signature matches within
    tolerance. Header format: `t=1712345678,v1=abc...,v1=def...`"""
    if not header:
        raise StripeSignatureError("missing Stripe-Signature header")

    timestamp: str | None = None
    candidates: list[str] = []
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            timestamp = v
        elif k == "v1":
            candidates.append(v)
    if not timestamp or not timestamp.isdigit() or not candidates:
        raise StripeSignatureError("malformed Stripe-Signature header")

    now = now if now is not None else time.time()
    if abs(now - int(timestamp)) > _TOLERANCE_S:
        raise StripeSignatureError("timestamp outside tolerance")

    signed = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, c) for c in candidates):
        raise StripeSignatureError("no matching v1 signature")


def _recompute_supporter(cur, account_id: int) -> bool:
    """accounts.supporter := any non-refunded payment exists. Also keeps
    supporter_since/amount pointing at the earliest live payment."""
    cur.execute(
        """
        SELECT created_at, amount_cents FROM supporter_payments
        WHERE account_id = %s AND refunded_at IS NULL
        ORDER BY created_at ASC LIMIT 1
        """,
        (account_id,),
    )
    first_live = cur.fetchone()
    cur.execute(
        """
        UPDATE accounts SET supporter = %s, supporter_since = %s, supporter_amount_cents = %s
        WHERE id = %s
        """,
        (
            first_live is not None,
            first_live[0] if first_live else None,
            first_live[1] if first_live else None,
            account_id,
        ),
    )
    return first_live is not None


def _handle_checkout_completed(obj: dict[str, Any]) -> str:
    ref = obj.get("client_reference_id")
    if not ref or not str(ref).isdigit():
        log.error("stripe checkout %s has unusable client_reference_id=%r",
                  obj.get("id"), ref)
        return "ignored_bad_reference"
    account_id = int(ref)
    if obj.get("payment_status") not in (None, "paid"):
        log.info("stripe checkout %s not paid (%s) — ignoring",
                 obj.get("id"), obj.get("payment_status"))
        return "ignored_unpaid"

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM accounts WHERE id = %s", (account_id,))
            if not cur.fetchone():
                log.error("stripe checkout %s references unknown account %d",
                          obj.get("id"), account_id)
                return "ignored_unknown_account"
            cur.execute(
                """
                INSERT INTO supporter_payments (
                    account_id, stripe_session_id, stripe_payment_intent, amount_cents
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (stripe_session_id) DO NOTHING
                """,
                (account_id, obj.get("id"), obj.get("payment_intent"),
                 obj.get("amount_total")),
            )
            _recompute_supporter(cur, account_id)
        conn.commit()
    log.info("supporter payment recorded: account=%d amount=%s session=%s",
             account_id, obj.get("amount_total"), obj.get("id"))
    return "recorded"


def _handle_charge_refunded(obj: dict[str, Any]) -> str:
    # Clear the flag only on FULL refund (§4.1). Stripe sets charge.refunded
    # true only when amount_refunded == amount.
    if not obj.get("refunded"):
        return "ignored_partial_refund"
    intent = obj.get("payment_intent")
    if not intent:
        return "ignored_no_intent"

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE supporter_payments SET refunded_at = NOW()
                WHERE stripe_payment_intent = %s AND refunded_at IS NULL
                RETURNING account_id
                """,
                (intent,),
            )
            row = cur.fetchone()
            if not row:
                conn.commit()
                return "ignored_unknown_payment"
            still_supporter = _recompute_supporter(cur, int(row[0]))
        conn.commit()
    log.info("stripe full refund: intent=%s account=%d supporter_now=%s",
             intent, row[0], still_supporter)
    return "refunded"


@router.post("/webhooks/stripe", include_in_schema=False)
async def stripe_webhook(request: Request) -> dict[str, Any]:
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(503, "stripe webhook not configured")

    payload = await request.body()
    try:
        verify_stripe_signature(payload, request.headers.get("Stripe-Signature"), secret)
    except StripeSignatureError as e:
        raise HTTPException(400, f"signature verification failed: {e}")

    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(400, "body is not JSON")

    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if etype == "checkout.session.completed":
        outcome = _handle_checkout_completed(obj)
    elif etype == "charge.refunded":
        outcome = _handle_charge_refunded(obj)
    else:
        outcome = "ignored_event_type"
    return {"received": True, "outcome": outcome}
