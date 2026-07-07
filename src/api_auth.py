"""Account auth routes (API_REQUIREMENTS.md §2.1–§2.3).

    POST /api/v1/auth/google       Google ID token → session
    POST /api/v1/auth/magic-link   email → Postmark magic link (always 202)
    POST /api/v1/auth/redeem       magic-link token → session
    POST /api/v1/auth/refresh      rotate the presented token
    GET  /api/v1/auth/session      session introspection for UI state
    POST /api/v1/auth/signout      revoke the presented token

Session-minting responses are exactly `{token, expires}` — the shape the
frontend's map-auth plumbing already stores. Everything else about the
session is readable from GET /api/v1/auth/session.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .accounts import (
    SessionUser,
    hash_token,
    mint_session,
    normalize_email,
    require_session,
    upsert_account,
)
from .client_ip import real_client_ip
from .config import load
from .google_auth import GoogleAuthError, verify_google_id_token
from .pg import connection
from .postmark import PostmarkError, postmark_credentials, send_magic_link
from .ratelimit import enforce

log = logging.getLogger(__name__)

router = APIRouter()

MAGIC_LINK_TTL_MINUTES = 15

# §2.3 limits, plus a modest per-IP bucket on the other POST doors (§5).
_LIMIT_MAGIC_PER_EMAIL = (3, 3600)    # 3/hour per email
_LIMIT_MAGIC_PER_IP = (10, 3600)      # 10/hour per IP
_LIMIT_GOOGLE_PER_IP = (30, 3600)
_LIMIT_REDEEM_PER_IP = (30, 3600)
_LIMIT_REFRESH_PER_ACCOUNT = (60, 3600)
_LIMIT_SIGNOUT_PER_IP = (60, 3600)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


_DEFAULT_MAGIC_LINK_TEMPLATE = "https://denver.scooter.fyi/auth?ml={token}"


def _magic_link_url(token: str) -> str:
    # Precedence: a non-empty MAGIC_LINK_URL_TEMPLATE env override (staging),
    # then the config.json default, then the hardcoded fallback.
    #
    # `os.environ.get(key, default)` returns "" when the key is present but
    # empty (the default only applies to a MISSING key), and "".format(...)
    # → "" — which silently shipped a sign-in email with a blank link. So an
    # empty/whitespace env var is treated as unset, and a template missing
    # the {token} placeholder (which would email a tokenless, useless URL)
    # falls back to the default.
    env = (os.environ.get("MAGIC_LINK_URL_TEMPLATE") or "").strip()
    template = env or (load().accounts.magic_link_url_template or "").strip()
    if "{token}" not in template:
        if template:
            log.error(
                "magic-link URL template has no {token} placeholder (%r); using default",
                template,
            )
        template = _DEFAULT_MAGIC_LINK_TEMPLATE
    return template.format(token=token)


def google_client_id() -> str | None:
    return os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or None


def _session_response(token: str, expires: datetime) -> dict[str, Any]:
    return {"token": token, "expires": expires.isoformat()}


# ---------------------------------------------------------------------------
# POST /api/v1/auth/google
# ---------------------------------------------------------------------------
class GoogleIn(BaseModel):
    credential: str = Field(..., min_length=20, max_length=4096)


@router.post("/api/v1/auth/google")
def auth_google(request: Request, payload: GoogleIn = Body(...)) -> dict[str, Any]:
    client_id = google_client_id()
    if not client_id:
        raise HTTPException(503, "google sign-in not configured")

    ip = real_client_ip(request)
    ua = request.headers.get("user-agent")

    try:
        claims = verify_google_id_token(payload.credential, client_id)
    except GoogleAuthError as e:
        raise HTTPException(401, str(e))

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="auth_google_ip", key=ip or "?",
                    limit=_LIMIT_GOOGLE_PER_IP[0], window_seconds=_LIMIT_GOOGLE_PER_IP[1])
            account_id = upsert_account(cur, claims["email"])
            token, expires = mint_session(
                cur, account_id=account_id, email=claims["email"],
                method="google", issued_ip=ip, user_agent=ua,
            )
        conn.commit()
    return _session_response(token, expires)


# ---------------------------------------------------------------------------
# POST /api/v1/auth/magic-link
# ---------------------------------------------------------------------------
class MagicLinkIn(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)


@router.post("/api/v1/auth/magic-link", status_code=202)
def auth_magic_link(request: Request, payload: MagicLinkIn = Body(...)) -> dict[str, Any]:
    """Issue and email a single-use sign-in link.

    Always 202 on success-shaped input — the response never reveals whether
    an account exists (accounts are upserted at redeem time anyway).
    """
    if not postmark_credentials():
        raise HTTPException(503, "magic-link sign-in not configured")
    email = normalize_email(payload.email)
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "not an email address")

    ip = real_client_ip(request)
    raw = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=MAGIC_LINK_TTL_MINUTES)

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="magic_link_ip", key=ip or "?",
                    limit=_LIMIT_MAGIC_PER_IP[0], window_seconds=_LIMIT_MAGIC_PER_IP[1])
            enforce(cur, bucket="magic_link_email", key=email,
                    limit=_LIMIT_MAGIC_PER_EMAIL[0], window_seconds=_LIMIT_MAGIC_PER_EMAIL[1])
            # Opportunistic prune of long-dead tokens (used or >1 day old).
            cur.execute(
                "DELETE FROM magic_link_tokens WHERE expires_at < NOW() - INTERVAL '1 day'"
            )
            cur.execute(
                """
                INSERT INTO magic_link_tokens (token_sha256, email, expires_at, request_ip)
                VALUES (%s, %s, %s, %s)
                """,
                (hash_token(raw), email, expires_at, ip),
            )
        conn.commit()

    # Send AFTER commit: a Postmark failure must not roll back the rate-limit
    # events (otherwise a broken sender becomes an unmetered retry loop).
    try:
        send_magic_link(email, _magic_link_url(raw))
    except PostmarkError:
        log.exception("magic-link send failed for %s", email)
        raise HTTPException(502, "couldn't send the sign-in email — try again in a minute")

    return {"sent": True}


# ---------------------------------------------------------------------------
# POST /api/v1/auth/redeem
# ---------------------------------------------------------------------------
class RedeemIn(BaseModel):
    token: str = Field(..., min_length=20, max_length=128)


@router.post("/api/v1/auth/redeem")
def auth_redeem(request: Request, payload: RedeemIn = Body(...)) -> dict[str, Any]:
    """Burn a magic-link token, upsert the account, mint a session.

    Magic-link sessions never carry the admin scope — enforced in
    accounts.session_scopes(), not here, so there's exactly one place the
    trust decision lives.
    """
    ip = real_client_ip(request)
    ua = request.headers.get("user-agent")
    digest = hash_token(payload.token)

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="auth_redeem_ip", key=ip or "?",
                    limit=_LIMIT_REDEEM_PER_IP[0], window_seconds=_LIMIT_REDEEM_PER_IP[1])
            # Single-use enforcement: the UPDATE only wins if used_at is
            # still NULL, so two concurrent redeems can't both succeed.
            cur.execute(
                """
                UPDATE magic_link_tokens SET used_at = NOW()
                WHERE token_sha256 = %s AND used_at IS NULL AND expires_at >= NOW()
                RETURNING email
                """,
                (digest,),
            )
            row = cur.fetchone()
            if not row:
                conn.commit()  # keep the rate-limit event
                raise HTTPException(401, "link is invalid, expired, or already used")
            email = row[0]
            account_id = upsert_account(cur, email)
            token, expires = mint_session(
                cur, account_id=account_id, email=email,
                method="magic_link", issued_ip=ip, user_agent=ua,
            )
        conn.commit()
    return _session_response(token, expires)


# ---------------------------------------------------------------------------
# POST /api/v1/auth/refresh
# ---------------------------------------------------------------------------
@router.post("/api/v1/auth/refresh")
def auth_refresh(request: Request, user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    """Rotate the presented token.

    Rider (sliding) sessions get a fresh 30-day expiry; admin (fixed)
    sessions keep their original expiry — rotation without extension.
    """
    now = datetime.now(timezone.utc)
    new_expires = now + timedelta(days=30) if user.sliding else user.expires_at
    raw = secrets.token_urlsafe(32)

    stored_scopes = [s for s in user.scopes if s != "supporter"]
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="auth_refresh_account", key=str(user.account_id),
                    limit=_LIMIT_REFRESH_PER_ACCOUNT[0], window_seconds=_LIMIT_REFRESH_PER_ACCOUNT[1])
            cur.execute(
                """
                INSERT INTO auth_sessions (
                    token_sha256, account_id, scopes, method, sliding,
                    expires_at, issued_ip, user_agent
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (hash_token(raw), user.account_id, stored_scopes, user.method,
                 user.sliding, new_expires, real_client_ip(request),
                 request.headers.get("user-agent")),
            )
            cur.execute(
                "UPDATE auth_sessions SET revoked_at = NOW() WHERE token_sha256 = %s",
                (user.token_sha256,),
            )
            # Opportunistic prune: revoked/expired rows older than 30 days.
            cur.execute(
                """
                DELETE FROM auth_sessions
                WHERE (revoked_at IS NOT NULL OR expires_at < NOW())
                  AND created_at < NOW() - INTERVAL '30 days'
                """
            )
        conn.commit()
    return _session_response(raw, new_expires)


# ---------------------------------------------------------------------------
# GET /api/v1/auth/session
# ---------------------------------------------------------------------------
@router.get("/api/v1/auth/session")
def auth_session(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    return {
        "email": user.email,
        "scopes": list(user.scopes),
        "supporter": user.supporter,
        "expires": user.expires_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# POST /api/v1/auth/signout
# ---------------------------------------------------------------------------
@router.post("/api/v1/auth/signout")
def auth_signout(request: Request) -> dict[str, Any]:
    """Revoke the presented token. Idempotent — an already-dead token
    still returns {revoked: true} (signout must never fail the client)."""
    authz = request.headers.get("Authorization") or ""
    if not authz.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    digest = hash_token(authz.split(" ", 1)[1].strip())
    ip = real_client_ip(request)
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="auth_signout_ip", key=ip or "?",
                    limit=_LIMIT_SIGNOUT_PER_IP[0], window_seconds=_LIMIT_SIGNOUT_PER_IP[1])
            cur.execute(
                "UPDATE auth_sessions SET revoked_at = NOW() "
                "WHERE token_sha256 = %s AND revoked_at IS NULL",
                (digest,),
            )
        conn.commit()
    return {"revoked": True}
