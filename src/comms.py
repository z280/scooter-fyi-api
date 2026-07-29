"""z280-comms — outbound SMS with reply routing.

The integration contract lives in z280/comms:docs/INTEGRATION.md. This
module is the whole of our side of it. Shaped deliberately like
src/postmark.py, because it plays the same role for SMS that Postmark
plays for email: credentials from the environment only, a `*_credentials()`
probe that returns None when unconfigured (so the route 503s and
/auth/config reports the door as off), and one exception type the route
maps to an HTTP status.

    COMMS_TOKEN     — bearer token, issued by the operator, one per
                      application. It IS our identity: it decides which
                      replies we receive. Never shared with another app.
    COMMS_BASE_URL  — override for the tailnet base URL (default below).

What comms does for us that a bare handset gateway did not:

  * **Consent.** A recipient who texted STOP is refused with 409 before a
    message is sent, across every application on the shared number — so we
    get 409s for people we personally have never messaged.
  * **Brand prefix.** Applied server-side. One phone number serves several
    applications, so a recipient sees the same sender for a KDF notice and
    a scooter.fyi code; the prefix is what tells them apart. We must NOT
    add our own — that would double it.
  * **Quota + fallback.** Hourly/daily caps (429), and a fallback to the
    handset when the primary transport is down (`fell_back: true`).

Delivery is never guaranteed. A 202 means accepted, not delivered, and
when `fell_back` is true no delivery confirmation will ever follow. Every
caller here has to be safe under "the message silently never arrived" —
for sign-in codes that's fine, the rider just asks for another.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://ovh3.kudu-squeaker.ts.net/comms"

# Comms is reached over the tailnet and normally answers in well under a
# second, but it may itself be waiting on a handset. 30s matches the
# timeout the integration doc's own reference client ships with.
_TIMEOUT_SECONDS = 30.0


class CommsError(Exception):
    """Any failure to hand a message to comms. Routes map this to 502."""


class OptedOut(CommsError):
    """Recipient has blocked communications (HTTP 409).

    NOT retryable, and not really an error on our side — a fact to record.
    `str(e)` is comms' own human-facing sentence, which names the exact
    keyword and number that undo the block; surface it verbatim rather
    than paraphrasing it, or the recipient is told to do something that
    doesn't work.
    """


class UnusableRecipient(CommsError):
    """Comms can't route to that number at all (HTTP 422)."""


class QuotaExceeded(CommsError):
    """We're over our hourly or daily send quota (HTTP 429)."""


def comms_credentials() -> dict[str, str] | None:
    """Token + base URL, or None when SMS is unconfigured.

    Only the token is required: the base URL has a working default, and an
    operator who sets a blank COMMS_BASE_URL means "use the default", not
    "post to the empty string".
    """
    token = os.environ.get("COMMS_TOKEN")
    if not token or not token.strip():
        return None
    base = (os.environ.get("COMMS_BASE_URL") or "").strip() or DEFAULT_BASE_URL
    return {"token": token.strip(), "base_url": base.rstrip("/")}


def _request(method: str, path: str, *, json: dict[str, Any] | None = None) -> httpx.Response:
    creds = comms_credentials()
    if not creds:
        raise CommsError("comms not configured (COMMS_TOKEN)")
    try:
        return httpx.request(
            method,
            f"{creds['base_url']}{path}",
            json=json,
            headers={
                "Authorization": f"Bearer {creds['token']}",
                "Accept": "application/json",
            },
            timeout=_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        raise CommsError(f"comms request failed: {e}") from e


def _opted_out_detail(response: httpx.Response) -> str:
    """The human-facing sentence out of a 409 body.

    Documented shape is {"detail": {"error": ..., "detail": "<sentence>"}}.
    Defensive because this string is shown to a rider: if the body isn't
    what we expect, say something true and generic rather than rendering
    a fragment of JSON at them.
    """
    fallback = "That number has blocked our texts."
    try:
        detail = response.json().get("detail")
    except ValueError:
        return fallback
    if isinstance(detail, dict):
        inner = detail.get("detail")
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
    elif isinstance(detail, str) and detail.strip():
        return detail.strip()
    return fallback


def send_sms(
    to: str,
    body: str,
    *,
    idempotency_key: str,
    ttl_seconds: int | None = None,
    urgent: bool = False,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST /v1/messages. Returns the accepted-message body on 202.

    `idempotency_key` is required here even though comms treats it as
    optional. Retries are the most common way to text somebody twice and
    nothing else in the chain dedupes, so making it a keyword-only
    positional-required argument means a caller cannot forget it. It must
    name the THING being communicated (e.g. the login code row), not the
    attempt — a fresh UUID per call defends against nothing.

    Raises OptedOut / UnusableRecipient / QuotaExceeded / CommsError; the
    body is NOT brand-prefixed here (comms does that server-side).
    """
    if not body or not body.strip():
        raise CommsError("refusing to send an empty message")

    payload: dict[str, Any] = {
        "to": to,
        "body": body,
        "channel": "sms",
        "urgent": urgent,
        "idempotency_key": idempotency_key,
        "metadata": metadata or {},
    }
    if ttl_seconds is not None:
        payload["ttl_seconds"] = ttl_seconds

    r = _request("POST", "/v1/messages", json=payload)

    if r.status_code == 409:
        raise OptedOut(_opted_out_detail(r))
    if r.status_code == 422:
        raise UnusableRecipient("comms can't deliver to that number")
    if r.status_code == 429:
        raise QuotaExceeded("over the SMS quota — try again later")
    if r.status_code == 403:
        # We're not provisioned for the channel. An operator config change,
        # not something a retry or a different number fixes.
        log.error("comms rejected the channel for this token: %s", r.text[:500])
        raise CommsError("this application isn't configured to send SMS")
    if r.status_code >= 400:
        log.error("comms send failed: HTTP %d %s", r.status_code, r.text[:500])
        raise CommsError(f"comms rejected the send (HTTP {r.status_code})")

    try:
        data = r.json()
    except ValueError as e:
        raise CommsError("comms returned a non-JSON success body") from e
    if not isinstance(data, dict):
        raise CommsError("comms returned an unexpected success body")

    if data.get("fell_back"):
        # The primary transport was down and the handset was used directly.
        # The message went out, but no delivery confirmation will ever
        # follow — worth a log line when we're chasing "did it arrive?".
        log.info("comms fell back to the handset for message %s", data.get("id"))
    return data


def poll_replies(limit: int = 50) -> list[dict[str, Any]]:
    """GET /v1/replies. **Polling claims what it returns** — a reply handed
    to us is not handed to anyone else (or to us again), whether or not we
    then manage to do anything with it. So the caller must be prepared to
    have consumed a reply it never processed; that's what ack is for."""
    r = _request("GET", f"/v1/replies?limit={int(limit)}")
    if r.status_code >= 400:
        log.error("comms reply poll failed: HTTP %d %s", r.status_code, r.text[:500])
        raise CommsError(f"comms rejected the poll (HTTP {r.status_code})")
    try:
        data = r.json()
    except ValueError as e:
        raise CommsError("comms returned a non-JSON reply body") from e
    replies = data.get("replies") if isinstance(data, dict) else None
    if not isinstance(replies, list):
        raise CommsError("comms returned no replies list")
    return [x for x in replies if isinstance(x, dict)]


def ack_reply(reply_id: str) -> None:
    """POST /v1/replies/{id}/ack — "we genuinely dealt with this one".

    Acking is bookkeeping, not flow control: an un-acked reply is not
    redelivered. It only preserves the difference between "processed" and
    "collected and dropped on the floor", which is the difference a human
    debugging a missing reply needs.
    """
    r = _request("POST", f"/v1/replies/{reply_id}/ack")
    if r.status_code >= 400:
        log.error("comms ack failed for %s: HTTP %d %s", reply_id, r.status_code, r.text[:500])
        raise CommsError(f"comms rejected the ack (HTTP {r.status_code})")
