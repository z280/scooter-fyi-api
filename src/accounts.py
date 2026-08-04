"""Account + session core (API_REQUIREMENTS.md §2.1).

Session model
-------------
Opaque bearer tokens: 32 bytes of urandom (256 bits), handed to the client
once, stored only as sha256 hex in auth_sessions. Scopes:

    rider      — every session; gates profile + report attribution
    admin      — a SIGNAL scope: the session's email was on the allowlist
                 when the session was minted, via ANY door. It does not
                 gate access — authorization is allowlist membership,
                 evaluated live (see is_admin_email / require_admin). The
                 scope was once Google-only, which made it disagree with
                 the check that actually authorizes; an allowlisted
                 operator signed in by magic link had full access and a UI
                 that said otherwise.

Expiry policy:
    rider sessions  — 30 days, sliding: POST /api/v1/auth/refresh rotates
                      the token and re-extends 30 days from now.
    admin sessions  — 24 h fixed: refresh rotates the token but keeps the
                      original expiry.

The FastAPI dependencies live here too: require_session (any valid
session), require_admin (session whose email is on ADMIN_EMAILS — either
door; this gates the /api/v1/private/* endpoints that the retired GitHub
map-auth bearer flow used to gate, per API_REQUIREMENTS.md §2.5).

Signed-in or admin are the ONLY gates in this system. There is no paid
tier — see sql/036_decommercialize.sql.
"""

from __future__ import annotations

import logging
import re
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


def admin_emails(cur=None) -> frozenset[str]:
    """The admin allowlist (normalized, lowercased).

    Source of truth is the `admin_allowlist` table, managed from the
    GitHub-gated admin portal (/admin/admins) and `python -m src.cli admin`.
    Empty when the table is empty. (Replaced the ADMIN_EMAILS env var — see
    sql/021_admin_allowlist.sql.)

    Pass `cur` when you already hold one. session_scopes does, because it
    runs inside mint_session's transaction: checking out a SECOND pooled
    connection while holding one is a deadlock vector on a pool this small
    (max 8), and sign-in is not the place to take that risk. Every other
    caller is a plain request handler holding nothing, and passes nothing.
    """
    # Normalize on read too: add_admin normalizes before insert, but a
    # manual/backfilled row with mixed case or whitespace shouldn't silently
    # fail the is_admin_email() check.
    if cur is not None:
        cur.execute("SELECT email FROM admin_allowlist")
        return frozenset(normalize_email(r[0]) for r in cur.fetchall())
    with connection() as conn:
        with conn.cursor() as cur2:
            cur2.execute("SELECT email FROM admin_allowlist")
            return frozenset(normalize_email(r[0]) for r in cur2.fetchall())


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


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_PHONE_STRIP_RE = re.compile(r"[\s\-.()]")
PHONE_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_phone_number(raw: str) -> str:
    """Strip common formatting characters (spaces, hyphens, dots, parens).
    Does NOT add or guess a country code — the client is expected to send
    E.164 (+countrycode...); see PHONE_E164_RE. Mirrors normalize_email's
    philosophy: normalize deterministically in one place, no citext, plain
    TEXT column + UNIQUE index (see sql/025_public_profile_fields.sql)."""
    return _PHONE_STRIP_RE.sub("", raw.strip())


def is_valid_phone_number(phone: str) -> bool:
    return bool(PHONE_E164_RE.match(phone))


# --- US numbers, for the SMS sign-in door -----------------------------------
#
# normalize_phone_number above stays the general E.164 normalizer for the
# profile column (it deliberately guesses nothing). SMS sign-in needs the
# opposite: a rider types "(303) 555-1212" into a phone field and expects it
# to work, so we DO supply the country code — but only because we only send
# to one country. Widen this, not normalize_phone_number, if that changes.

_DIGITS_RE = re.compile(r"\D")
# +1, then a 3-digit area code and 3-digit exchange, each starting 2-9, then
# the 4-digit subscriber number.
_NANP_RE = re.compile(r"^\+1([2-9]\d{2})([2-9]\d{2})\d{4}$")


def normalize_us_phone(raw: str) -> str | None:
    """Coerce a US number a human typed into E.164, or None if it can't be.

    Accepts the forms a rider actually types — `(303) 555-1212`,
    `303-555-1212`, `3035551212`, `1 303 555 1212`, `+13035551212` — by
    reducing to digits and re-adding the +1. Returns None rather than
    raising: every caller has a 400 to return and nothing useful to add.
    """
    if not raw:
        return None
    digits = _DIGITS_RE.sub("", raw)
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        return None
    candidate = "+" + digits
    return candidate if is_valid_us_phone(candidate) else None


def is_valid_us_phone(phone: str) -> bool:
    """Structural NANP validity for an E.164 string.

    Rejects N11 in both the area code and the exchange (211/311/…/911 are
    service codes, never assigned to a subscriber), which is the cheap check
    that catches most typos and all of the obviously-fake numbers.

    Note this is deliberately looser than the `[2-9][0-8]\\d` area-code
    pattern that gets copy-pasted around: that rule predates area codes with
    a middle digit of 9, and today would reject real numbers in 929 (New
    York), 934, 959 and 984. Rejecting a rider's actual phone number is a
    worse failure than accepting an unassigned one — comms answers 422 for
    a number it genuinely cannot route to, so an unroutable number is
    already handled downstream, whereas a false rejection here is a dead end
    with no recourse.
    """
    m = _NANP_RE.match(phone)
    if not m:
        return False
    area, exchange = m.group(1), m.group(2)
    return not (area.endswith("11") or exchange.endswith("11"))


def is_admin_email(user: "SessionUser") -> bool:
    """Whether a session's email is on the ADMIN_EMAILS allowlist.

    A phone-only account (SMS sign-in, no email on file — see
    upsert_account_by_phone) has `email = None` and is never an admin: the
    allowlist is keyed by email, so there is nothing to match against. It
    has to be checked rather than assumed, or the None reaches
    normalize_email and a would-be 403 becomes a 500.

    This — NOT the `admin` scope — is the admin authorization check for the
    /api/v1/private/* endpoints and the /api/v1/user plate fields, so an
    allowlisted operator can use EITHER sign-in door (magic-link or Google).
    Both doors prove ownership of the email.

    The `admin` scope is now stamped from this same allowlist for any door
    (see session_scopes), so the two agree. It still does not GATE anything
    — this function is the gate — but it is no longer a different answer to
    the same question. Note the scope is fixed at mint time while this is
    evaluated live, so adding someone to the allowlist grants access on
    their very next request without a new sign-in.
    """
    if not user.email:
        return False
    return normalize_email(user.email) in admin_emails()


def hash_token(raw: str) -> str:
    return sha256(raw.encode("utf-8")).hexdigest()


def session_scopes(*, method: str, email: str | None, cur=None) -> list[str]:
    """Stored scopes for a new session.

    THE `admin` SCOPE IS AGNOSTIC TO THE SIGN-IN METHOD: any door, same
    allowlist, same answer. It was Google-only, which made it disagree with
    is_admin_email — the check that actually authorizes /private/* and the
    plate fields, and which has always accepted either door because both
    prove ownership of the same allowlisted email. One address was admin or
    not depending on which button they pressed, and only the *signal*
    disagreed, so the effect was an operator with full access whose UI
    insisted they had none.

    `method` is kept in the signature: it still records which door was used
    (auth_sessions.method), and a future rule that genuinely depends on the
    door belongs here rather than being re-derived elsewhere.

    `email` is None for an SMS session on a phone-only account — the
    allowlist is keyed by email, so there is nothing to match and no lookup
    to make.

    Consequence worth naming: session_expiry keys the 24h non-sliding
    admin lifetime off this scope, so an email-door admin now gets that
    lifetime too, rather than a 30-day rider session. That is the point of
    making it agnostic — an admin session is an admin session.
    """
    scopes = ["rider"]
    if email and normalize_email(email) in admin_emails(cur):
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
    # None for an account whose only identity is a verified phone number
    # (SMS sign-in). Anything that formats or matches on this must handle
    # the None — see is_admin_email.
    email: str | None
    scopes: tuple[str, ...]
    expires_at: datetime
    sliding: bool
    method: str
    token_sha256: str


class InvalidUsernameChoice(Exception):
    """Raised when a rider-chosen adjective/emoji isn't in the curated
    lists (sfw_adjectives/emoji_nouns). Safe for a 400 detail."""


_USERNAME_MAX_ATTEMPTS = 25


def generate_public_username(cur) -> tuple[str, str]:
    """One random (adjective, emoji) pair, e.g. ('brave', '🦉'). Reads from
    sfw_adjectives/emoji_nouns (sql/025) rather than a hardcoded Python
    list, so the seed data stays the single source of truth. ORDER BY
    random() is fine at this table size (a few hundred rows apiece)."""
    cur.execute("SELECT word FROM sfw_adjectives ORDER BY random() LIMIT 1")
    adjective = cur.fetchone()[0]
    cur.execute("SELECT emoji FROM emoji_nouns ORDER BY random() LIMIT 1")
    emoji = cur.fetchone()[0]
    return adjective, emoji


def _username_taken(cur, candidate: str) -> bool:
    cur.execute("SELECT 1 FROM accounts WHERE public_username = %s", (candidate,))
    return cur.fetchone() is not None


def assign_public_username(cur, account_id: int, *, max_attempts: int = _USERNAME_MAX_ATTEMPTS) -> str:
    """Generate-and-persist a globally-unique RANDOM public_username.
    Shared by upsert_account() (brand-new accounts) and the
    `backfill_public_usernames` CLI command (existing accounts) — the ONE
    place that knows how to mint a username.

    Deliberately pre-checks for a collision under an advisory lock rather
    than attempting the UPDATE and catching UniqueViolation-and-retrying:
    a failed statement aborts the whole enclosing Postgres transaction
    (see src/pg.py:run_migrations for the one place this codebase already
    handles that, via an explicit conn.rollback()) — fine there, but NOT
    safe here, since assign_public_username can run inside a larger
    transaction with earlier uncommitted work it must not blow away (e.g.
    upsert_account's own INSERT ... ON CONFLICT just above it in the same
    transaction). Never raising in the first place sidesteps the issue.
    """
    for _ in range(max_attempts):
        adjective, emoji = generate_public_username(cur)
        candidate = f"{adjective}{emoji}"
        # Serializes two concurrent callers who happen to draw the exact
        # same pair — same technique src/ratelimit.py uses for its own
        # check-then-act race. Auto-releases at COMMIT/ROLLBACK.
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"public_username:{candidate}",),
        )
        if _username_taken(cur, candidate):
            continue
        cur.execute(
            "UPDATE accounts SET username_adjective = %s, username_emoji = %s WHERE id = %s",
            (adjective, emoji, account_id),
        )
        return candidate
    raise RuntimeError(
        f"assign_public_username: no free word pair found for account "
        f"{account_id} after {max_attempts} attempts"
    )


def choose_public_username(
    cur, account_id: int, *, adjective: str | None, emoji: str | None
) -> str:
    """Explicit rider choice for one or both halves of the public
    username (PUT /api/v1/profile/username). A missing half keeps the
    account's current value for that half.

    Raises InvalidUsernameChoice (400-safe) if a supplied word isn't in
    the curated lists. Does NOT itself handle the "already taken" case —
    unlike assign_public_username's random-retry loop, a single explicit
    choice has nowhere to retry to, so the caller lets
    psycopg.errors.UniqueViolation on accounts_public_username_key
    propagate and maps it to 409 (mirrors PUT /api/v1/profile's handling
    of the phone/email unique constraints in src/api_profile.py) — safe
    here specifically because it's the last statement this function runs.
    """
    cur.execute(
        "SELECT username_adjective, username_emoji FROM accounts WHERE id = %s",
        (account_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"no such account: {account_id}")
    current_adjective, current_emoji = row
    new_adjective = adjective if adjective is not None else current_adjective
    new_emoji = emoji if emoji is not None else current_emoji
    if new_adjective is None or new_emoji is None:
        raise InvalidUsernameChoice(
            "no existing username on file — provide both adjective and emoji"
        )

    if adjective is not None:
        cur.execute("SELECT 1 FROM sfw_adjectives WHERE word = %s", (adjective,))
        if cur.fetchone() is None:
            raise InvalidUsernameChoice(f"{adjective!r} is not in the adjective list")
    if emoji is not None:
        cur.execute("SELECT 1 FROM emoji_nouns WHERE emoji = %s", (emoji,))
        if cur.fetchone() is None:
            raise InvalidUsernameChoice(f"{emoji!r} is not in the emoji list")

    cur.execute(
        "UPDATE accounts SET username_adjective = %s, username_emoji = %s WHERE id = %s",
        (new_adjective, new_emoji, account_id),
    )
    return f"{new_adjective}{new_emoji}"


def upsert_account(cur, email: str) -> int:
    """Create-or-touch an account by (lowercased) email; returns id.

    Brand-new rows get a public_username immediately (assign_public_
    username); an existing row's last_login_at is bumped and its username
    is left alone. `(xmax = 0)` is the standard Postgres idiom for telling
    apart the INSERT and DO UPDATE arms of one ON CONFLICT statement
    (stable since ON CONFLICT shipped in 9.5 — see the Postgres UPSERT
    wiki page).
    """
    cur.execute(
        """
        INSERT INTO accounts (email, last_login_at) VALUES (%s, NOW())
        ON CONFLICT (email) DO UPDATE SET last_login_at = NOW()
        RETURNING id, (xmax = 0) AS inserted
        """,
        (normalize_email(email),),
    )
    account_id, inserted = cur.fetchone()
    account_id = int(account_id)
    if inserted:
        assign_public_username(cur, account_id)
    return account_id


class PhoneNumberTaken(Exception):
    """Another account has already PROVED this number. Proof beats proof
    only by recency of nothing at all — we simply refuse. The route maps
    this to a 409."""


class PhoneNumberContested(Exception):
    """The number is held by an account that never proved it AND has no
    other way to sign in, so releasing it would strand that account and
    adopting it would be an account takeover. Needs an operator; the route
    maps this to a 409."""


def _lock_phone(cur, phone: str) -> None:
    """Serialize everyone racing to claim one number. Same advisory-lock
    idiom as assign_public_username; auto-released at COMMIT/ROLLBACK."""
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"account_phone:{phone}",),
    )


def _release_unverified_holder(cur, phone: str, *, keep_account_id: int | None = None) -> None:
    """Take `phone` away from an account that never proved it.

    PUT /api/v1/profile writes phone_number with no proof whatsoever, so an
    existing holder is an ASSERTION, and this whole module's rule is that
    proof beats assertion. Callers hold the phone lock and have already
    established that the caller proved the number.

    Raises PhoneNumberTaken if the holder DID prove it (nobody's proof is
    better than anyone else's — a human has to sort that out), and
    PhoneNumberContested if releasing would leave the holder with neither
    an email nor a phone: sql/025 forbids that row, and even if it didn't,
    it would be an account with no door left to sign in through.
    """
    cur.execute(
        "SELECT id, email, phone_verified_at FROM accounts WHERE phone_number = %s",
        (phone,),
    )
    row = cur.fetchone()
    if not row:
        return
    holder_id, holder_email, verified_at = int(row[0]), row[1], row[2]
    if keep_account_id is not None and holder_id == keep_account_id:
        return
    if verified_at is not None:
        raise PhoneNumberTaken(f"account {holder_id} has already verified {phone}")
    if not holder_email:
        raise PhoneNumberContested(
            f"account {holder_id} holds {phone} unverified and has no email"
        )
    log.warning(
        "releasing unverified phone number from account %d — it was never "
        "proved and someone has now proved it",
        holder_id,
    )
    cur.execute("UPDATE accounts SET phone_number = NULL WHERE id = %s", (holder_id,))


def phone_is_verified(cur, phone: str) -> bool:
    """Has anyone proved they answer this number?

    Read-only, and used for exactly one thing: deciding whether a send is a
    returning owner (who may have no other way in) or an unknown number.
    Never an authorization check on its own.
    """
    cur.execute(
        "SELECT 1 FROM accounts WHERE phone_number = %s AND phone_verified_at IS NOT NULL",
        (phone,),
    )
    return cur.fetchone() is not None


def claim_verified_phone(cur, account_id: int, phone: str) -> None:
    """Attach a just-proved number to an EXISTING account.

    This is the bridge that keeps SMS sign-in from quietly forking a
    rider's identity: without it, a rider who types their number into their
    profile and then signs in by SMS gets a brand-new empty account,
    because sign-in refuses to resolve an unverified number
    (upsert_account_by_phone). Verifying from inside the session they
    already have attaches the proof to the account they already own.
    """
    _lock_phone(cur, phone)
    _release_unverified_holder(cur, phone, keep_account_id=account_id)
    cur.execute(
        "UPDATE accounts SET phone_number = %s, phone_verified_at = NOW() WHERE id = %s",
        (phone, account_id),
    )


def upsert_account_by_phone(cur, phone: str) -> int:
    """Create-or-touch an account by VERIFIED phone number; returns id.

    Called only after a code sent to `phone` was typed back correctly, so
    reaching here IS the proof of ownership — which is why this is the only
    function in the codebase that sets phone_verified_at.

    The interesting case is a number already sitting in some account's
    profile. PUT /api/v1/profile writes that column with no proof
    whatsoever, so "someone else's row already has this number" must NOT
    mean "sign them into that row" — that is precisely the takeover
    sql/045 exists to prevent (claim a stranger's number, wait for them to
    sign in, receive their account). Proof beats assertion: an UNVERIFIED
    holder loses the number, and a fresh account is created for whoever
    actually answered the text.

    The one case we refuse outright: an unverified holder with no email.
    Releasing the number would leave that account with neither identity
    (accounts_email_or_phone_required, sql/025) and no door left to sign in
    through; adopting it would be the takeover. Rare enough — it takes a
    profile edit that removes the email — to be worth a human's attention
    rather than a guess.
    """
    _lock_phone(cur, phone)

    cur.execute(
        "SELECT id FROM accounts WHERE phone_number = %s AND phone_verified_at IS NOT NULL",
        (phone,),
    )
    row = cur.fetchone()
    if row:
        # The proven owner is signing in again.
        account_id = int(row[0])
        cur.execute(
            "UPDATE accounts SET last_login_at = NOW() WHERE id = %s", (account_id,)
        )
        return account_id

    # Nobody has proved it. An unverified holder loses it (or we refuse —
    # see _release_unverified_holder); PhoneNumberTaken is unreachable from
    # here, since a verified holder was just handled above.
    _release_unverified_holder(cur, phone)

    # No proven owner: this is a new account, whose only identity is the
    # number it just proved. sql/025 made email nullable for exactly this.
    cur.execute(
        """
        INSERT INTO accounts (phone_number, phone_verified_at, last_login_at)
        VALUES (%s, NOW(), NOW())
        RETURNING id
        """,
        (phone,),
    )
    account_id = int(cur.fetchone()[0])
    assign_public_username(cur, account_id)
    return account_id


def mint_session(
    cur,
    *,
    account_id: int,
    email: str | None,
    method: str,
    issued_ip: str | None,
    user_agent: str | None,
) -> tuple[str, datetime]:
    """Insert a session row; returns (raw_token, expires_at).

    The raw token exists only in this return value and the HTTP response —
    never logged, never stored.
    """
    now = datetime.now(timezone.utc)
    scopes = session_scopes(method=method, email=email, cur=cur)
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
                       s.sliding, s.method, s.revoked_at
                FROM auth_sessions s
                JOIN accounts a ON a.id = s.account_id
                WHERE s.token_sha256 = %s
                """,
                (digest,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(401, "invalid token")
            account_id, email, scopes, expires_at, sliding, method, revoked_at = row
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

    return SessionUser(
        account_id=int(account_id),
        email=email,
        scopes=tuple(scopes or []),
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
    """Gate on admin-allowlist membership, so an allowlisted operator reaches
    the /api/v1/private/* endpoints via ANY sign-in door. Evaluated live
    against the table rather than read off the session's scopes, so adding or
    removing an admin takes effect on the next request."""
    user = require_session(request)
    if not is_admin_email(user):
        raise HTTPException(403, "admin access required (email not on ADMIN_EMAILS)")
    return user

