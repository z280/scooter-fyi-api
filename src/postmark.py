"""Postmark transactional email — magic-link delivery (§2.3).

Thin httpx wrapper around POST https://api.postmarkapp.com/email using the
transactional ("outbound") message stream. Credentials are env-only, per
the config convention:

    POSTMARK_TOKEN  — server token from the Postmark account
    POSTMARK_FROM   — verified sender signature (e.g. signin@scooter.fyi)

Both unset → magic-link sign-in is unconfigured (the endpoint 503s).
Send failures raise PostmarkError; the auth route maps that to a 502 with
a friendly detail, per the requirements doc.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

_API_URL = "https://api.postmarkapp.com/email"


class PostmarkError(Exception):
    pass


def postmark_credentials() -> dict[str, str] | None:
    token = os.environ.get("POSTMARK_TOKEN")
    sender = os.environ.get("POSTMARK_FROM")
    if not token or not sender:
        return None
    return {"token": token, "sender": sender}


def send_magic_link(email: str, link: str) -> None:
    """Send the sign-in email. Raises PostmarkError on any failure."""
    creds = postmark_credentials()
    if not creds:
        raise PostmarkError("postmark not configured (POSTMARK_TOKEN / POSTMARK_FROM)")

    body = {
        "From": creds["sender"],
        "To": email,
        "Subject": "Sign in to denver.scooter.fyi",
        "TextBody": (
            "Tap to sign in to denver.scooter.fyi:\n\n"
            f"    {link}\n\n"
            "The link works once and expires in 15 minutes. If you didn't "
            "request it, ignore this email — nobody can sign in without it.\n"
        ),
        "MessageStream": "outbound",
    }
    try:
        r = httpx.post(
            _API_URL,
            json=body,
            headers={"X-Postmark-Server-Token": creds["token"], "Accept": "application/json"},
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        raise PostmarkError(f"postmark request failed: {e}") from e
    if r.status_code >= 400:
        # Postmark error bodies are JSON {ErrorCode, Message} — log the
        # detail, surface only a generic message to the caller.
        log.error("postmark send failed: HTTP %d %s", r.status_code, r.text[:500])
        raise PostmarkError(f"postmark rejected the send (HTTP {r.status_code})")
