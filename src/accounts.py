"""Account + session core (API_REQUIREMENTS.md §2.1).

Session model
-------------
Opaque bearer tokens: 32 bytes of urandom (256 bits), handed to the client
once, stored only as sha256 hex in auth_sessions. Scopes:

    rider      — every session; gates profile + report attribution
    admin      — Google sign-in AND email on the ADMIN_EMAILS allowlist.
                 Magic-link sessions NEVER get admin, even for allowlisted
                 emails (one trust decision, enforced here server-side).
    supporter  — never stored; derived from accounts.supporter at read
                 time so a Stripe webhook flip applies to live sessions.

Expiry policy:
    rider sessions  — 30 days, sliding: POST /api/v1/auth/refresh rotates
                      the token and re-extends 30 days from now.
    admin sessions  — 24 h fixed: refresh rotates the token but keeps the
                      original expiry.

The FastAPI dependencies live here too (require_session / require_admin),
mirroring the map_auth_dep pattern.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from fastapi import HTTPException, Request

from .pg import connection

log = logging.getLogger(__name__)

RIDER_SESSION_DAYS = 30
ADMIN_SESSION_HOURS = 24


def admin_emails() -> frozenset[str]:
    """The ADMIN_EMAILS env allowlist, lowercased. Empty when unset."""
    raw = os.environ.get("ADMIN_EMAILS", "")
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_token(raw: str) -> str:
    return sha256(raw.encode("utf-8")).hexdigest()


def session_scopes(*, method: str, email: str) -> list[str]:
    """Stored scopes for a new session. Admin requires the Google door."""
    scopes = ["rider"]
    if method == "google" and normalize_email(email) in admin_emails():
        scopes.append("admin")
    return scopes


def session_expiry(*, scopes: list[str], now: datetime) -> tuple[datetime, bool]:
    """(expires_at, sliding) for a new session with the given scopes."""
    if "admin" in scopes:
        return now + timedelta(hours=ADMIN_SESSION_HOURS), False
    return now + timedelta(days=RIDER_SESSION_DAYS), True


@dataclass(frozen=True)
class SessionUser:
    account_id: int
    email: str
    scopes: tuple[str, ...]   # stored scopes + derived 'supporter'
    supporter: bool
    expires_at: datetime
    sliding: bool
    method: str
    token_sha256: str


def upsert_account(cur, email: str) -> int:
    """Create-or-touch an account by (lowercased) email; returns id."""
    cur.execute(
        """
        INSERT INTO accounts (email, last_login_at) VALUES (%s, NOW())
        ON CONFLICT (email) DO UPDATE SET last_login_at = NOW()
        RETURNING id
        """,
        (normalize_email(email),),
    )
    return int(cur.fetchone()[0])


def mint_session(
    cur,
    *,
    account_id: int,
    email: str,
    method: str,
    issued_ip: str | None,
    user_agent: str | None,
) -> tuple[str, datetime]:
    """Insert a session row; returns (raw_token, expires_at).

    The raw token exists only in this return value and the HTTP response —
    never logged, never stored.
    """
    now = datetime.now(timezone.utc)
    scopes = session_scopes(method=method, email=email)
    expires_at, sliding = session_expiry(scopes=scopes, now=now)
    raw = secrets.token_urlsafe(32)
    cur.execute(
        """
        INSERT INTO auth_sessions (
            token_sha256, account_id, scopes, method, sliding,
            expires_at, issued_ip, user_agent
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (hash_token(raw), account_id, scopes, method, sliding,
         expires_at, issued_ip, user_agent),
    )
    log.info(
        "session minted: account=%d method=%s scopes=%s expires=%s",
        account_id, method, scopes, expires_at.isoformat(),
    )
    return raw, expires_at


def _bearer_token(request: Request) -> str:
    authz = request.headers.get("Authorization") or ""
    if not authz.lower().startswith("bearer "):
        raise HTTPException(
            401,
            "missing or malformed Authorization header (expected: Bearer <token>)",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raw = authz.split(" ", 1)[1].strip()
    if not raw:
        raise HTTPException(401, "empty bearer token")
    return raw


def _load_session(digest: str) -> SessionUser:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.account_id, a.email, s.scopes, s.expires_at,
                       s.sliding, s.method, s.revoked_at, a.supporter
                FROM auth_sessions s
                JOIN accounts a ON a.id = s.account_id
                WHERE s.token_sha256 = %s
                """,
                (digest,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(401, "invalid token")
            account_id, email, scopes, expires_at, sliding, method, revoked_at, supporter = row
            if revoked_at is not None:
                raise HTTPException(401, "token revoked")
            if expires_at < datetime.now(timezone.utc):
                raise HTTPException(401, "token expired")

            # Best-effort instrumentation — never fails the request.
            try:
                cur.execute(
                    "UPDATE auth_sessions SET last_used_at = NOW() WHERE token_sha256 = %s",
                    (digest,),
                )
                conn.commit()
            except Exception:  # noqa: BLE001
                log.exception("touching auth_sessions.last_used_at failed")

    effective = list(scopes or [])
    if supporter and "supporter" not in effective:
        effective.append("supporter")
    return SessionUser(
        account_id=int(account_id),
        email=email,
        scopes=tuple(effective),
        supporter=bool(supporter),
        expires_at=expires_at,
        sliding=bool(sliding),
        method=method,
        token_sha256=digest,
    )


def require_session(request: Request) -> SessionUser:
    """FastAPI dependency: any valid session (every session has `rider`)."""
    return _load_session(hash_token(_bearer_token(request)))


def require_admin(request: Request) -> SessionUser:
    user = require_session(request)
    if "admin" not in user.scopes:
        raise HTTPException(403, "admin scope required")
    return user


def require_supporter(request: Request) -> SessionUser:
    user = require_session(request)
    if not user.supporter:
        raise HTTPException(403, "supporter required — see denver.scooter.fyi/support")
    return user
