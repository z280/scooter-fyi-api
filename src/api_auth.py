"""Account auth routes (API_REQUIREMENTS.md §2.1–§2.3).

    POST /api/v1/auth/google       Google ID token → session
    POST /api/v1/auth/magic-link   email → Postmark magic link (always 202)
    POST /api/v1/auth/redeem       magic-link token → session
    POST /api/v1/auth/code         email → Postmark AA000AA code (always 202)
    POST /api/v1/auth/code/verify  email + code → session
    POST /api/v1/auth/sms/code     phone → comms AA000AA code
    POST /api/v1/auth/sms/code/verify  phone + code → session
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
    PhoneNumberContested,
    SessionUser,
    hash_token,
    mint_session,
    normalize_email,
    normalize_us_phone,
    require_session,
    upsert_account,
    upsert_account_by_phone,
)
from .client_ip import real_client_ip
from .comms import (
    CommsError,
    OptedOut,
    QuotaExceeded,
    UnusableRecipient,
    comms_credentials,
    send_sms,
)
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

# SMS sign-in (z280-comms). Same AA000AA code and the same TTL as the email
# door — a rider who has used both types the same shape of thing either way,
# and the entropy is identical, so the code needs no format-driven
# compensation. What differs is what the LIMITS protect: every email is free,
# every text costs a real message on one physical handset shared with other
# applications, so these are tighter and there is a global ceiling.
SMS_CODE_TTL_MINUTES = 10
_LIMIT_SMS_CODE_PER_PHONE = (3, 3600)     # 3 texts/hour to one number
_LIMIT_SMS_CODE_PER_IP = (5, 3600)        # 5 texts/hour from one IP
_LIMIT_SMS_CODE_GLOBAL = (250, 86400)     # 250 texts/day, everyone
_LIMIT_SMS_VERIFY_PER_PHONE = (10, 3600)  # 10 guesses/hour against a number
_LIMIT_SMS_VERIFY_PER_IP = (30, 3600)

# Comms prefixes "scooter.fyi: " server-side — one phone number serves
# several applications, and that prefix is what tells a recipient which one
# is texting them. So the body must NOT name the site again: the rider would
# read "scooter.fyi: Use code AB123XY to login at denver.scooter.fyi".
# Delivered in full as: "scooter.fyi: Use code AB123XY to login."
# 26 characters here, 39 delivered — one GSM-7 segment either way, so it
# can't split into two billed messages.
SMS_CODE_TEMPLATE = "Use code {code} to login."

# How long comms may spend trying to hand this to the network before
# dropping it — NOT how long the code is valid (SMS_CODE_TTL_MINUTES).
SMS_SEND_TTL_SECONDS = 120

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


def _hash_code(destination: str, code: str) -> str:
    """HMAC-SHA256(server secret, "destination:CODE"). Keyed so a leaked
    login_codes table can't be brute-forced offline, and bound to the
    destination so a code is only ever valid for the address — or phone
    number — it was sent to.

    Still strips and lowercases the destination itself, exactly as it did
    when it took an email. That keeps every previously-issued hash valid
    across this deploy, and it stays a defence rather than a formality: a
    future caller that forgets to normalize would otherwise write a code
    that can never be verified. It is a no-op for the SMS door, whose
    destinations are already E.164 and have no case to fold.
    """
    msg = f"{destination.strip().lower()}:{code}".encode("utf-8")
    return hmac.new(session_secret().encode("utf-8"), msg, sha256).hexdigest()


# The two destination columns login_codes supports (sql/045). Interpolated
# into SQL below, so it is a closed set of our own literals — never a value
# that came off a request.
_DESTINATION_COLUMNS = ("email", "phone_number")


def _issue_code(cur, *, column: str, destination: str, ttl_minutes: int,
                ip: str | None) -> tuple[str, int]:
    """Burn any live code for this destination, insert a fresh one.

    Returns (plaintext code, login_codes row id). The row id is what the
    SMS path uses as its comms idempotency key: it names THIS issuance,
    which is the thing being communicated, so a retried send can't text
    somebody a second copy — while a genuinely new code gets a new key and
    is allowed through.
    """
    assert column in _DESTINATION_COLUMNS, column
    code = _generate_code()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)

    # Only the newest code per destination stays live — issuing a new one
    # burns any prior unused code so there's a single guess target.
    cur.execute(
        f"UPDATE login_codes SET used_at = NOW() "
        f"WHERE {column} = %s AND used_at IS NULL",
        (destination,),
    )
    # Opportunistic prune of long-dead rows (used or >1 day expired).
    cur.execute("DELETE FROM login_codes WHERE expires_at < NOW() - INTERVAL '1 day'")
    cur.execute(
        f"""
        INSERT INTO login_codes ({column}, code_hash, expires_at, request_ip)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (destination, _hash_code(destination, code), expires_at, ip),
    )
    return code, int(cur.fetchone()[0])


def _verify_code(conn, cur, *, column: str, destination: str, code: str) -> None:
    """Check a typed code against the live one for this destination and burn
    it. Returns None on success; raises HTTPException(401) otherwise.

    Commits before every raise so the rate-limit event the caller already
    recorded survives — a failed guess must still cost the guesser a slot,
    which a rollback would refund.
    """
    assert column in _DESTINATION_COLUMNS, column

    cur.execute(
        f"""
        SELECT id, code_hash FROM login_codes
        WHERE {column} = %s AND used_at IS NULL AND expires_at >= NOW()
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (destination,),
    )
    row = cur.fetchone()
    if not row:
        conn.commit()  # keep the rate-limit event
        raise HTTPException(401, "code is invalid or expired")
    code_id, code_hash = row

    # Claim an attempt ATOMICALLY before comparing: the row-locked UPDATE
    # serializes concurrent verifies, so N simultaneous guesses can't all
    # pass a stale `attempts` snapshot and blow past the cap (a TOCTOU the
    # read-then-check version had). No row returned ⇒ the code was
    # used/burned between the SELECT and here.
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

    if not hmac.compare_digest(_hash_code(destination, code), code_hash):
        conn.commit()  # the attempt was already counted above
        raise HTTPException(401, "code is invalid or expired")

    # Success — burn single-use atomically (a concurrent verify that already
    # won leaves used_at NOT NULL, so this returns no row).
    cur.execute(
        "UPDATE login_codes SET used_at = NOW() "
        "WHERE id = %s AND used_at IS NULL RETURNING id",
        (code_id,),
    )
    if not cur.fetchone():
        conn.commit()
        raise HTTPException(401, "code is invalid or expired")


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


def google_auth_enabled() -> bool:
    """Master switch for the Google door. Default ON (backwards compatible),
    so a configured GOOGLE_OAUTH_CLIENT_ID keeps working. Set
    GOOGLE_AUTH_ENABLED to a falsy value (0/false/no/off, or blank) to force
    Google OFF regardless of the client id — the product "Google off for now"
    decision, enforced server-side rather than by unsetting the client id."""
    raw = os.environ.get("GOOGLE_AUTH_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def google_client_id() -> str | None:
    """The GIS client id, or None when Google sign-in is unavailable — either
    force-disabled via GOOGLE_AUTH_ENABLED or simply unconfigured. Both the
    /auth/google endpoint (503) and /auth/config (google_enabled=false) key
    off this one function, so the switch governs everything in one place."""
    if not google_auth_enabled():
        return None
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
        # The SMS door needs a z280-comms token; mirrors the 503 on
        # /api/v1/auth/sms/code.
        "sms_enabled": comms_credentials() is not None,
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

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="login_code_ip", key=ip or "?",
                    limit=_LIMIT_CODE_PER_IP[0], window_seconds=_LIMIT_CODE_PER_IP[1])
            enforce(cur, bucket="login_code_email", key=email,
                    limit=_LIMIT_CODE_PER_EMAIL[0], window_seconds=_LIMIT_CODE_PER_EMAIL[1])
            code, _ = _issue_code(cur, column="email", destination=email,
                                  ttl_minutes=CODE_TTL_MINUTES, ip=ip)
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

            _verify_code(conn, cur, column="email", destination=email, code=code)

            account_id = upsert_account(cur, email)
            token, expires = mint_session(
                cur, account_id=account_id, email=email,
                method="email_code", issued_ip=ip, user_agent=ua,
            )
        conn.commit()
    return _session_response(token, expires)


# ---------------------------------------------------------------------------
# POST /api/v1/auth/sms/code  +  POST /api/v1/auth/sms/code/verify
# ---------------------------------------------------------------------------
class SmsCodeRequestIn(BaseModel):
    phone_number: str = Field(..., min_length=7, max_length=32)


class SmsCodeVerifyIn(BaseModel):
    phone_number: str = Field(..., min_length=7, max_length=32)
    code: str = Field(..., min_length=1, max_length=32)


def _require_us_phone(raw: str) -> str:
    phone = normalize_us_phone(raw)
    if not phone:
        raise HTTPException(400, "enter a US phone number, like (303) 555-1212")
    return phone


def enforce_sms_send_budget(cur, *, phone: str, ip: str | None) -> None:
    """The three buckets every outbound sign-in-ish text must clear.

    One function so that every door which can cause a text to be sent draws
    on the SAME budget: they share one physical handset, on a number shared
    with other applications, so a second door with its own limits would
    silently double what the operator's device can be made to send. Callers
    must not add their own per-phone bucket alongside this.
    """
    enforce(cur, bucket="sms_code_ip", key=ip or "?",
            limit=_LIMIT_SMS_CODE_PER_IP[0], window_seconds=_LIMIT_SMS_CODE_PER_IP[1])
    enforce(cur, bucket="sms_code_phone", key=phone,
            limit=_LIMIT_SMS_CODE_PER_PHONE[0], window_seconds=_LIMIT_SMS_CODE_PER_PHONE[1])
    # A distributed attempt spread thin enough to clear both per-key buckets
    # still can't drain a day's sending in an afternoon.
    enforce(cur, bucket="sms_code_global", key="all",
            limit=_LIMIT_SMS_CODE_GLOBAL[0], window_seconds=_LIMIT_SMS_CODE_GLOBAL[1])


def _note_opt_out(phone: str) -> None:
    """Record locally that this number has blocked texts.

    Comms is authoritative on consent and enforces it for us; this is only
    so our own UI can be honest before trying again. Best-effort by design —
    failing to write a note must not change what the rider is told about
    their sign-in, which is the far more important half of this response.
    """
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE accounts SET sms_opted_out_at = NOW() "
                    "WHERE phone_number = %s AND sms_opted_out_at IS NULL",
                    (phone,),
                )
            conn.commit()
    except Exception:  # noqa: BLE001
        log.exception("recording the SMS opt-out for a sign-in attempt failed")


def send_code_sms(phone: str, body: str, *, idempotency_key: str, purpose: str) -> None:
    """Hand a code text to comms and translate its answers into ours.

    The idempotency key must name the THING being communicated (a specific
    issued code), never the attempt — see comms.send_sms.
    """
    try:
        send_sms(
            phone,
            body,
            idempotency_key=idempotency_key,
            # A code that surfaces after the rider has given up is worse
            # than one that never arrives: the first is a confused user and
            # a support ticket, the second is a retry that works. So comms
            # drops it rather than queueing it when the handset is
            # unreachable, and `urgent` skips the inter-send delays. The
            # code itself stays valid for SMS_CODE_TTL_MINUTES — this only
            # bounds how long we'll wait to hand it to the network.
            ttl_seconds=SMS_SEND_TTL_SECONDS,
            urgent=True,
            metadata={"purpose": purpose},
        )
    except OptedOut as e:
        _note_opt_out(phone)
        # Verbatim, not paraphrased — it names the exact keyword and number
        # that unblock, and a reworded version of it doesn't work.
        raise HTTPException(409, str(e))
    except UnusableRecipient:
        raise HTTPException(400, "that number can't receive texts — check it and try again")
    except QuotaExceeded:
        raise HTTPException(429, "too many texts sent right now — try again later")
    except CommsError:
        log.exception("SMS code send failed (%s)", purpose)
        raise HTTPException(502, "couldn't send the code — try again in a minute")


@router.post("/api/v1/auth/sms/code", status_code=202)
def auth_sms_code_request(request: Request, payload: SmsCodeRequestIn = Body(...)) -> dict[str, Any]:
    """Text a short AA000AA sign-in code (typed back at
    /api/v1/auth/sms/code/verify). Requires z280-comms (503 if not).

    Unlike the email door this does NOT always 202. A `409` (the recipient
    has blocked texts) has to reach the rider: the alternative is a
    permanently silent "check your phone" for a message that will never be
    sent, and the 409 body names the exact keyword and number that undo it.
    That leaks nothing about accounts — it is a fact about the phone
    number's relationship to the shared sender, and the rider asking is
    holding the phone.
    """
    if not comms_credentials():
        raise HTTPException(503, "SMS sign-in not configured")
    phone = _require_us_phone(payload.phone_number)
    ip = real_client_ip(request)

    with connection() as conn:
        with conn.cursor() as cur:
            # Each send costs a real message on one physical handset, so
            # these protect the operator's device and plan — not the code,
            # whose entropy is identical to the email door's.
            enforce_sms_send_budget(cur, phone=phone, ip=ip)
            code, code_id = _issue_code(cur, column="phone_number", destination=phone,
                                        ttl_minutes=SMS_CODE_TTL_MINUTES, ip=ip)
        conn.commit()

    # Send AFTER commit so a comms outage can't roll back the rate-limit
    # events (a broken sender must not become a free retry loop).
    send_code_sms(
        phone,
        SMS_CODE_TEMPLATE.format(code=code),
        idempotency_key=f"login-code-{code_id}",
        purpose="sign_in",
    )
    return {"sent": True}


@router.post("/api/v1/auth/sms/code/verify")
def auth_sms_code_verify(request: Request, payload: SmsCodeVerifyIn = Body(...)) -> dict[str, Any]:
    """Verify a texted code, upsert the account by phone, mint a session.

    Typing the code back IS the proof that the rider answers this number,
    which is why this is the only path that sets phone_verified_at (see
    accounts.upsert_account_by_phone). Sessions mint as 'sms_code' and never
    carry the admin scope — session_scopes restricts that to Google.
    """
    ip = real_client_ip(request)
    ua = request.headers.get("user-agent")
    phone = _require_us_phone(payload.phone_number)
    code = _normalize_code(payload.code)

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="sms_code_verify_ip", key=ip or "?",
                    limit=_LIMIT_SMS_VERIFY_PER_IP[0],
                    window_seconds=_LIMIT_SMS_VERIFY_PER_IP[1])
            # Per-phone too, which the email door doesn't do per-address: a
            # code sent to a phone is guessable by anyone who knows the
            # number, without needing access to any inbox.
            enforce(cur, bucket="sms_code_verify_phone", key=phone,
                    limit=_LIMIT_SMS_VERIFY_PER_PHONE[0],
                    window_seconds=_LIMIT_SMS_VERIFY_PER_PHONE[1])

            _verify_code(conn, cur, column="phone_number", destination=phone, code=code)

            try:
                account_id = upsert_account_by_phone(cur, phone)
            except PhoneNumberContested:
                conn.commit()
                log.warning("SMS sign-in blocked: %s is contested", phone)
                raise HTTPException(
                    409,
                    "that number is attached to another account that can't be "
                    "released automatically — email support to sort it out",
                )
            token, expires = mint_session(
                cur, account_id=account_id, email=None,
                method="sms_code", issued_ip=ip, user_agent=ua,
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

    stored_scopes = list(user.scopes)
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
