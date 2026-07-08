"""Account + session core (API_REQUIREMENTS.md §2.1).

Session model
-------------
Opaque bearer tokens: 32 bytes of urandom (256 bits), handed to the client
once, stored only as sha256 hex in auth_sessions. Scopes:

    rider      — every session; gates profile + report attribution
    admin      — a Google-only SIGNAL scope (Google sign-in AND email on
                 ADMIN_EMAILS). It no longer gates access: admin
                 authorization is ADMIN_EMAILS membership via either door
                 (see is_admin_email / require_admin), so an allowlisted
                 operator can use magic-link too. The scope is still handy
                 for the frontend to know a Google admin door was used.
    supporter  — never stored; derived from accounts.supporter at read
                 time so a Stripe webhook flip applies to live sessions.

Expiry policy:
    rider sessions  — 30 days, sliding: POST /api/v1/auth/refresh rotates
                      the token and re-extends 30 days from now.
    admin sessions  — 24 h fixed: refresh rotates the token but keeps the
                      original expiry.

The FastAPI dependencies live here too: require_session (any valid
session), require_admin (session whose email is on ADMIN_EMAILS — either
door; this gates the /api/v1/private/* endpoints that the retired GitHub
map-auth bearer flow used to gate, per API_REQUIREMENTS.md §2.5), and
require_supporter.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from fastapi import HTTPException, Request

from .pg import connection

log = logging.getLogger(__name__)

RIDER_SESSION_DAYS = 30
ADMIN_SESSION_HOURS = 24


def admin_emails() -> frozenset[str]:
    """The admin allowlist (normalized, lowercased).

    Source of truth is the `admin_allowlist` table, managed from the
    GitHub-gated admin portal (/admin/admins) and `python -m src.cli admin`.
    Empty when the table is empty. (Replaced the ADMIN_EMAILS env var — see
    sql/021_admin_allowlist.sql.)
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT email FROM admin_allowlist")
            return frozenset(r[0] for r in cur.fetchall())


def list_admins() -> list[dict[str, Any]]:
    """Full allowlist rows for the admin portal, newest first."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT email, added_by, added_at FROM admin_allowlist "
                "ORDER BY added_at DESC, email"
            )
            return [
                {
                    "email": r[0],
                    "added_by": r[1],
                    "added_at": r[2].isoformat() if r[2] else None,
                }
                for r in cur.fetchall()
            ]


def add_admin(email: str, added_by: str | None) -> bool:
    """Add an email to the allowlist (stored normalized). Idempotent —
    returns True if newly inserted, False if it was already present.
    Raises ValueError for a non-email string."""
    norm = normalize_email(email)
    if not norm or "@" not in norm or norm.startswith("@") or norm.endswith("@"):
        raise ValueError(f"not an email address: {email!r}")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO admin_allowlist (email, added_by) VALUES (%s, %s) "
                "ON CONFLICT (email) DO NOTHING",
                (norm, added_by),
            )
            added = cur.rowcount == 1
        conn.commit()
    return added


def remove_admin(email: str) -> bool:
    """Remove an email from the allowlist. Returns True if a row was
    removed, False if it wasn't present."""
    norm = normalize_email(email)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admin_allowlist WHERE email = %s", (norm,))
            removed = cur.rowcount == 1
        conn.commit()
    return removed


def normalize_email(email: str) -> str:
    return email.strip().lower()


def is_admin_email(user: "SessionUser") -> bool:
    """Whether a session's email is on the ADMIN_EMAILS allowlist.

    This — NOT the `admin` scope — is the admin authorization check for the
    /api/v1/private/* endpoints and the /api/v1/user plate fields, so an
    allowlisted operator can use EITHER sign-in door (magic-link or Google).
    Both doors prove ownership of the email. The `admin` scope stays a
    Google-only signal (see session_scopes) but no longer gates access.
    """
    return normalize_email(user.email) in admin_emails()


def hash_token(raw: str) -> str:
    return sha256(raw.encode("utf-8")).hexdigest()


def session_scopes(*, method: str, email: str) -> list[str]:
    """Stored scopes for a new session. The `admin` scope is a Google-only
    signal (it does NOT gate access — require_admin uses is_admin_email)."""
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


def optional_session(request: Request) -> SessionUser | None:
    """None when no Authorization header is presented; 401 when one is
    presented but invalid (silently demoting a bad token to anonymous
    would misattribute writes the client believes are signed-in)."""
    if not (request.headers.get("Authorization") or "").strip():
        return None
    return require_session(request)


def require_admin(request: Request) -> SessionUser:
    """Gate on ADMIN_EMAILS membership, so an allowlisted operator reaches
    the /api/v1/private/* endpoints via EITHER sign-in door (magic-link or
    Google) — not the Google-only `admin` scope."""
    user = require_session(request)
    if not is_admin_email(user):
        raise HTTPException(403, "admin access required (email not on ADMIN_EMAILS)")
    return user


def require_supporter(request: Request) -> SessionUser:
    user = require_session(request)
    if not user.supporter:
        raise HTTPException(403, "supporter required — see denver.scooter.fyi/support")
    return user
