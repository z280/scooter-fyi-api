"""Account auth routes (API_REQUIREMENTS.md §2.1–§2.3).

    POST /api/v1/auth/google       Google ID token → session
    POST /api/v1/auth/magic-link   email → Postmark magic link (always 202)
    POST /api/v1/auth/redeem       magic-link token → session
    POST /api/v1/auth/code         email → Postmark AA000AA code (always 202)
    POST /api/v1/auth/code/verify  email + code → session
    POST /api/v1/auth/refresh      rotate the presented token
    GET  /api/v1/auth/session      session introspection for UI state
    POST /api/v1/auth/signout      revoke the presented token

Session-minting responses are exactly `{token, expires}` — the shape the
frontend's map-auth plumbing already stores. Everything else about the
session is readable from GET /api/v1/auth/session.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
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
from .config import load, session_secret
from .google_auth import GoogleAuthError, verify_google_id_token
from .pg import connection
from .postmark import (
    PostmarkError,
    postmark_credentials,
    send_login_code,
    send_magic_link,
)
from .ratelimit import enforce

log = logging.getLogger(__name__)

router = APIRouter()

MAGIC_LINK_TTL_MINUTES = 15

# Email code (type-a-code) sign-in. The code is low-entropy (AA000AA ≈
# 24^4·10^3 ≈ 3.3e8), so it leans on: email-scoped verification, a short
# TTL, a per-code attempt cap, and per-IP/per-email rate limits.
CODE_TTL_MINUTES = 10
MAX_CODE_ATTEMPTS = 5
# AA000AA = 2 letters, 3 digits, 2 letters. Letters exclude I/O (look like
# 1/0); digit positions keep the full 0-9.
_CODE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_CODE_DIGITS = "0123456789"
_CODE_RE = re.compile(r"^[A-Z]{2}[0-9]{3}[A-Z]{2}$")

# §2.3 limits, plus a modest per-IP bucket on the other POST doors (§5).
_LIMIT_MAGIC_PER_EMAIL = (3, 3600)    # 3/hour per email
_LIMIT_MAGIC_PER_IP = (10, 3600)      # 10/hour per IP
_LIMIT_GOOGLE_PER_IP = (30, 3600)
_LIMIT_REDEEM_PER_IP = (30, 3600)
_LIMIT_REFRESH_PER_ACCOUNT = (60, 3600)
_LIMIT_SIGNOUT_PER_IP = (60, 3600)
_LIMIT_CODE_PER_EMAIL = (3, 3600)     # 3 code requests/hour per email
_LIMIT_CODE_PER_IP = (10, 3600)       # 10 code requests/hour per IP
_LIMIT_CODE_VERIFY_PER_IP = (30, 3600)  # 30 verify attempts/hour per IP

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _generate_code() -> str:
    """A fresh AA000AA code."""
    ch = [
        secrets.choice(_CODE_LETTERS),
        secrets.choice(_CODE_LETTERS),
        secrets.choice(_CODE_DIGITS),
        secrets.choice(_CODE_DIGITS),
        secrets.choice(_CODE_DIGITS),
        secrets.choice(_CODE_LETTERS),
        secrets.choice(_CODE_LETTERS),
    ]
    return "".join(ch)


def _normalize_code(raw: str) -> str:
    """Uppercase and drop spaces/hyphens the user may have typed."""
    return re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()


def _hash_code(email: str, code: str) -> str:
    """HMAC-SHA256(server secret, "email:CODE"). Keyed so a leaked
    login_codes table can't be brute-forced offline, and bound to the email
    so a code is only ever valid for the address it was sent to."""
    msg = f"{normalize_email(email)}:{code}".encode("utf-8")
    return hmac.new(session_secret().encode("utf-8"), msg, sha256).hexdigest()


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
# GET /api/v1/auth/config
# ---------------------------------------------------------------------------
@router.get("/api/v1/auth/config")
def auth_config(response: Response) -> dict[str, Any]:
    """Public sign-in capabilities for the frontend.

    One source of truth for which sign-in doors to render and the Google
    Identity Services client id to initialize with — so the frontend doesn't
    hardcode the client id in a second place and can hide the Google option
    when the server can't verify a token anyway.

    The Google OAuth client id is NOT a secret: it's designed to be embedded
    in the browser (it only names the audience; token exchange needs the
    Google-held client secret, which never leaves the server). `*_enabled`
    mirror the 503 conditions on the corresponding endpoints.
    """
    response.headers["Cache-Control"] = "public, max-age=300"
    client_id = google_client_id()
    postmark_ready = postmark_credentials() is not None
    return {
        "google_client_id": client_id,
        "google_enabled": client_id is not None,
        "magic_link_enabled": postmark_ready,
        # The typed-code door uses the same Postmark transport as magic-link.
        "code_enabled": postmark_ready,
    }


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
# POST /api/v1/auth/code  +  POST /api/v1/auth/code/verify
# ---------------------------------------------------------------------------
class CodeRequestIn(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)


class CodeVerifyIn(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    code: str = Field(..., min_length=1, max_length=32)


@router.post("/api/v1/auth/code", status_code=202)
def auth_code_request(request: Request, payload: CodeRequestIn = Body(...)) -> dict[str, Any]:
    """Email a short AA000AA sign-in code (the user types it back at
    /api/v1/auth/code/verify). Always 202 on success-shaped input — never
    reveals whether an account exists. Requires Postmark (503 if not)."""
    if not postmark_credentials():
        raise HTTPException(503, "code sign-in not configured")
    email = normalize_email(payload.email)
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "not an email address")

    ip = real_client_ip(request)
    code = _generate_code()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=CODE_TTL_MINUTES)

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="login_code_ip", key=ip or "?",
                    limit=_LIMIT_CODE_PER_IP[0], window_seconds=_LIMIT_CODE_PER_IP[1])
            enforce(cur, bucket="login_code_email", key=email,
                    limit=_LIMIT_CODE_PER_EMAIL[0], window_seconds=_LIMIT_CODE_PER_EMAIL[1])
            # Only the newest code per email stays live — issuing a new one
            # burns any prior unused code so there's a single guess target.
            cur.execute(
                "UPDATE login_codes SET used_at = NOW() "
                "WHERE email = %s AND used_at IS NULL",
                (email,),
            )
            # Opportunistic prune of long-dead rows (used or >1 day expired).
            cur.execute(
                "DELETE FROM login_codes WHERE expires_at < NOW() - INTERVAL '1 day'"
            )
            cur.execute(
                """
                INSERT INTO login_codes (email, code_hash, expires_at, request_ip)
                VALUES (%s, %s, %s, %s)
                """,
                (email, _hash_code(email, code), expires_at, ip),
            )
        conn.commit()

    # Send AFTER commit so a Postmark failure doesn't roll back the
    # rate-limit events (a broken sender must not become a free retry loop).
    try:
        send_login_code(email, code)
    except PostmarkError:
        log.exception("login-code send failed for %s", email)
        raise HTTPException(502, "couldn't send the code email — try again in a minute")

    return {"sent": True}


@router.post("/api/v1/auth/code/verify")
def auth_code_verify(request: Request, payload: CodeVerifyIn = Body(...)) -> dict[str, Any]:
    """Verify an emailed code, upsert the account, mint a session.

    The code is low-entropy, so verification is email-scoped, attempt-capped
    (MAX_CODE_ATTEMPTS wrong tries burns the code), and per-IP rate limited.
    Like magic-link, a code session never carries the admin scope (enforced
    in accounts.session_scopes — method is 'email_code'). Returns the same
    `{token, expires}` the frontend stores to start the session in the tab.
    """
    ip = real_client_ip(request)
    ua = request.headers.get("user-agent")
    email = normalize_email(payload.email)
    code = _normalize_code(payload.code)

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="login_code_verify_ip", key=ip or "?",
                    limit=_LIMIT_CODE_VERIFY_PER_IP[0],
                    window_seconds=_LIMIT_CODE_VERIFY_PER_IP[1])

            cur.execute(
                """
                SELECT id, code_hash FROM login_codes
                WHERE email = %s AND used_at IS NULL AND expires_at >= NOW()
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (email,),
            )
            row = cur.fetchone()
            if not row:
                conn.commit()  # keep the rate-limit event
                raise HTTPException(401, "code is invalid or expired")
            code_id, code_hash = row

            # Claim an attempt ATOMICALLY before comparing: the row-locked
            # UPDATE serializes concurrent verifies, so N simultaneous
            # guesses can't all pass a stale `attempts` snapshot and blow
            # past the cap (a TOCTOU the read-then-check version had). No row
            # returned ⇒ the code was used/burned between the SELECT and here.
            cur.execute(
                "UPDATE login_codes SET attempts = attempts + 1 "
                "WHERE id = %s AND used_at IS NULL RETURNING attempts",
                (code_id,),
            )
            claimed = cur.fetchone()
            if not claimed:
                conn.commit()
                raise HTTPException(401, "code is invalid or expired")
            if claimed[0] > MAX_CODE_ATTEMPTS:
                cur.execute("UPDATE login_codes SET used_at = NOW() WHERE id = %s", (code_id,))
                conn.commit()
                raise HTTPException(401, "too many attempts — request a new code")

            if not hmac.compare_digest(_hash_code(email, code), code_hash):
                conn.commit()  # the attempt was already counted above
                raise HTTPException(401, "code is invalid or expired")

            # Success — burn single-use atomically (a concurrent verify that
            # already won leaves used_at NOT NULL, so this returns no row).
            cur.execute(
                "UPDATE login_codes SET used_at = NOW() "
                "WHERE id = %s AND used_at IS NULL RETURNING id",
                (code_id,),
            )
            if not cur.fetchone():
                conn.commit()
                raise HTTPException(401, "code is invalid or expired")

            account_id = upsert_account(cur, email)
            token, expires = mint_session(
                cur, account_id=account_id, email=email,
                method="email_code", issued_ip=ip, user_agent=ua,
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
