"""Stripe supporter webhook (API_REQUIREMENTS.md §4.1).

POST /webhooks/stripe — the only Stripe surface in v1. Two supporter paths
feed the same `accounts.supporter` flag:

    Legacy one-time Payment Link (pay-what-you-want):
        checkout.session.completed (mode=payment) → record the payment
        charge.refunded                           → on FULL refund, mark
                                                     the payment refunded

    Current recurring plan (single fixed price, 30-day free trial):
        checkout.session.completed (mode=subscription) → link account to
                                                           the new subscription
        customer.subscription.created/updated          → status/trial/period
        customer.subscription.deleted                  → canceled

A trialing subscription counts as `supporter` — no payment has moved yet,
but the whole point of a free trial is early access. supporter_since /
supporter_amount_cents (on `accounts`) stay meaningful only for the
one-time path; subscription rows don't have a single "amount".

Signature verification is implemented directly (HMAC-SHA256 over
"{t}.{raw_body}" against the v1 entries of the Stripe-Signature header,
constant-time compare, 5-minute timestamp tolerance) — no Stripe SDK
dependency for one webhook.

Idempotency: stripe_session_id / stripe_subscription_id are UNIQUE;
webhook retries no-op (ON CONFLICT upserts). Unknown event types return
200 so Stripe doesn't retry them forever. A checkout without a usable
client_reference_id also returns 200 (it would never succeed on retry)
— logged loudly instead.

Event ordering for the subscription path isn't guaranteed by Stripe —
customer.subscription.created can arrive before checkout.session.completed.
Only the checkout session carries client_reference_id (our account id), so
subscription events upsert supporter_subscriptions by stripe_subscription_id
with account_id left NULL if the row doesn't exist yet; checkout.session.completed
fills account_id in whichever order it lands. A NULL-account_id row is not
yet supporter-eligible (nothing to recompute).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .pg import connection

log = logging.getLogger(__name__)

router = APIRouter()

_TOLERANCE_S = 300

# Stripe subscription statuses that make the account a supporter. A trial
# is `trialing`; a paying, current subscription is `active`. Everything
# else (past_due, canceled, unpaid, incomplete, incomplete_expired) is not.
_ACTIVE_SUBSCRIPTION_STATUSES = {"trialing", "active"}


def _unix_to_dt(ts: Any) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


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
    """accounts.supporter := any non-refunded one-time payment OR any
    trialing/active subscription. supporter_since is the earliest of the
    two (a trial start counts — it's when access began, not when money
    moved). supporter_amount_cents only ever reflects a one-time payment;
    a subscription doesn't have a single "amount", so it stays NULL for
    subscription-only supporters.
    """
    cur.execute(
        """
        SELECT created_at, amount_cents FROM supporter_payments
        WHERE account_id = %s AND refunded_at IS NULL
        ORDER BY created_at ASC LIMIT 1
        """,
        (account_id,),
    )
    first_live_payment = cur.fetchone()

    cur.execute(
        """
        SELECT created_at FROM supporter_subscriptions
        WHERE account_id = %s AND status = ANY(%s)
        ORDER BY created_at ASC LIMIT 1
        """,
        (account_id, list(_ACTIVE_SUBSCRIPTION_STATUSES)),
    )
    first_active_subscription = cur.fetchone()

    since_candidates = [c[0] for c in (first_live_payment, first_active_subscription) if c is not None]
    is_supporter = bool(since_candidates)
    cur.execute(
        """
        UPDATE accounts SET supporter = %s, supporter_since = %s, supporter_amount_cents = %s
        WHERE id = %s
        """,
        (
            is_supporter,
            min(since_candidates) if since_candidates else None,
            first_live_payment[1] if first_live_payment else None,
            account_id,
        ),
    )
    return is_supporter


def _handle_checkout_completed(obj: dict[str, Any]) -> str:
    """Dispatch on session `mode` — the one-time PWYW path (legacy, still
    honored for anyone who already supported that way) vs. the current
    recurring-subscription path. Both share the session-id and
    client_reference_id guards below."""
    session_id = obj.get("id")
    if not session_id:
        # Never insert a NULL session id — UNIQUE allows multiple NULLs in
        # Postgres, so a missing id would silently break the idempotency
        # guarantee retries depend on.
        log.error("stripe checkout event has no session id: %r", obj)
        return "ignored_missing_session_id"

    ref = obj.get("client_reference_id")
    if not ref or not str(ref).isdigit():
        log.error("stripe checkout %s has unusable client_reference_id=%r",
                  session_id, ref)
        return "ignored_bad_reference"
    account_id = int(ref)

    if obj.get("mode") == "subscription":
        return _handle_subscription_checkout(obj, session_id, account_id)
    return _handle_payment_checkout(obj, session_id, account_id)


def _handle_payment_checkout(obj: dict[str, Any], session_id: str, account_id: int) -> str:
    # Require an EXPLICIT "paid" — completed sessions using async payment
    # methods (e.g. bank debits) can report payment_status="unpaid" at
    # completion time; treating a missing/unrecognized status as paid
    # would record a supporter payment before money has actually moved.
    if obj.get("payment_status") != "paid":
        log.info("stripe checkout %s not paid (%s) — ignoring",
                 session_id, obj.get("payment_status"))
        return "ignored_unpaid"

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM accounts WHERE id = %s", (account_id,))
            if not cur.fetchone():
                log.error("stripe checkout %s references unknown account %d",
                          session_id, account_id)
                return "ignored_unknown_account"
            cur.execute(
                """
                INSERT INTO supporter_payments (
                    account_id, stripe_session_id, stripe_payment_intent, amount_cents
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (stripe_session_id) DO NOTHING
                """,
                (account_id, session_id, obj.get("payment_intent"),
                 obj.get("amount_total")),
            )
            _recompute_supporter(cur, account_id)
        conn.commit()
    log.info("supporter payment recorded: account=%d amount=%s session=%s",
             account_id, obj.get("amount_total"), session_id)
    return "recorded"


def _handle_subscription_checkout(obj: dict[str, Any], session_id: str, account_id: int) -> str:
    """Link the account to the new subscription. Status/trial/period fields
    are NOT set here — they arrive via customer.subscription.created, which
    Stripe may deliver before or after this event; the ON CONFLICT below
    only ever touches account_id/customer_id so it can never clobber a
    status already written by that event."""
    subscription_id = obj.get("subscription")
    customer_id = obj.get("customer")
    if not subscription_id or not customer_id:
        log.error("stripe subscription checkout %s missing subscription/customer "
                  "(subscription=%r customer=%r)", session_id, subscription_id, customer_id)
        return "ignored_missing_subscription"

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM accounts WHERE id = %s", (account_id,))
            if not cur.fetchone():
                log.error("stripe checkout %s references unknown account %d",
                          session_id, account_id)
                return "ignored_unknown_account"
            cur.execute(
                """
                INSERT INTO supporter_subscriptions (
                    account_id, stripe_subscription_id, stripe_customer_id
                ) VALUES (%s, %s, %s)
                ON CONFLICT (stripe_subscription_id) DO UPDATE SET
                    account_id = EXCLUDED.account_id,
                    stripe_customer_id = EXCLUDED.stripe_customer_id,
                    updated_at = NOW()
                """,
                (account_id, subscription_id, customer_id),
            )
            _recompute_supporter(cur, account_id)
        conn.commit()
    log.info("supporter subscription linked: account=%d subscription=%s session=%s",
             account_id, subscription_id, session_id)
    return "subscription_linked"


def _handle_subscription_event(obj: dict[str, Any]) -> str:
    """customer.subscription.created / customer.subscription.updated —
    upsert status/trial_end/current_period_end by stripe_subscription_id.
    account_id is left untouched here (may still be NULL if the linking
    checkout.session.completed hasn't arrived yet)."""
    subscription_id = obj.get("id")
    customer_id = obj.get("customer")
    status = obj.get("status")
    if not subscription_id or not customer_id or not status:
        log.error("stripe subscription event missing required fields: %r", obj)
        return "ignored_incomplete_subscription_event"

    trial_end = _unix_to_dt(obj.get("trial_end"))
    current_period_end = _unix_to_dt(obj.get("current_period_end"))

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO supporter_subscriptions (
                    stripe_subscription_id, stripe_customer_id, status,
                    trial_end, current_period_end
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (stripe_subscription_id) DO UPDATE SET
                    stripe_customer_id = EXCLUDED.stripe_customer_id,
                    status = EXCLUDED.status,
                    trial_end = EXCLUDED.trial_end,
                    current_period_end = EXCLUDED.current_period_end,
                    updated_at = NOW()
                RETURNING account_id
                """,
                (subscription_id, customer_id, status, trial_end, current_period_end),
            )
            row = cur.fetchone()
            account_id = row[0] if row else None
            if account_id is not None:
                _recompute_supporter(cur, account_id)
        conn.commit()
    log.info("stripe subscription %s status=%s account=%s",
             subscription_id, status, account_id)
    return "subscription_updated" if account_id is not None else "subscription_pending_account_link"


def _handle_subscription_deleted(obj: dict[str, Any]) -> str:
    subscription_id = obj.get("id")
    if not subscription_id:
        log.error("stripe subscription.deleted missing id: %r", obj)
        return "ignored_missing_subscription_id"
    status = obj.get("status") or "canceled"

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE supporter_subscriptions
                SET status = %s, canceled_at = NOW(), updated_at = NOW()
                WHERE stripe_subscription_id = %s
                RETURNING account_id
                """,
                (status, subscription_id),
            )
            row = cur.fetchone()
            if not row:
                conn.commit()
                return "ignored_unknown_subscription"
            account_id = row[0]
            still_supporter = _recompute_supporter(cur, account_id) if account_id is not None else None
        conn.commit()
    log.info("stripe subscription canceled: subscription=%s account=%s supporter_now=%s",
             subscription_id, account_id, still_supporter)
    return "subscription_canceled"


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
    elif etype in ("customer.subscription.created", "customer.subscription.updated"):
        outcome = _handle_subscription_event(obj)
    elif etype == "customer.subscription.deleted":
        outcome = _handle_subscription_deleted(obj)
    else:
        outcome = "ignored_event_type"
    return {"received": True, "outcome": outcome}
