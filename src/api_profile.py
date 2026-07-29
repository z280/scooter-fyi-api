"""Profile endpoints (API_REQUIREMENTS.md §2.4, extended for public
usernames / phone numbers / home-work locations).

    GET  /api/v1/profile                      full profile incl. server-computed fields
    PUT  /api/v1/profile                      partial update of the client-writable fields
    POST /api/v1/profile/username/regenerate  re-roll public_username to a new random pair
    PUT  /api/v1/profile/username             choose a specific adjective and/or emoji
    POST /api/v1/profile/phone/code           text a code to prove your number
    POST /api/v1/profile/phone/verify         type it back → phone_verified

Client-writable via PUT /api/v1/profile: rate_plan, theme, favorites,
email, phone_number, show_public_username, show_in_leaderboards,
home_lat/home_lng, work_lat/work_lng.

Server-computed, read-only: badges
(recomputed on every read — see src/badges.py), public_username (minted
by accounts.assign_public_username at account creation / CLI backfill).
public_username itself is never a field on ProfileUpdate — change it via
the two dedicated endpoints below, not by smuggling it through the
generic profile PUT (that would defeat the curated-word-list/SFW
guarantee those endpoints enforce). Choosing your own adjective/emoji is
open to every rider today; it's a plausible future restricted perk,
so both endpoints are written to make that a one-line `Depends` swap
later.

A profile must carry an email, a phone_number, or both — never neither
(accounts_email_or_phone_required, sql/025). Enforced here in plain
language before it ever reaches the DB's CHECK constraint.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import psycopg
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from .accounts import (
    EMAIL_RE,
    InvalidUsernameChoice,
    PhoneNumberContested,
    PhoneNumberTaken,
    SessionUser,
    assign_public_username,
    choose_public_username,
    claim_verified_phone,
    normalize_email,
    normalize_phone_number,
    normalize_us_phone,
    is_valid_phone_number,
    require_session,
)
# The code machinery (generate / hash / issue / verify, and the SMS send
# settings) is owned by the auth module, because sign-in is what it exists
# for. Verifying your own number is the same machinery pointed at a
# different outcome — a column on your account instead of a session — so it
# imports rather than reimplements. One-directional: api_auth knows nothing
# about profiles.
from .api_auth import (
    SMS_CODE_TTL_MINUTES,
    _issue_code,
    _normalize_code,
    _verify_code,
    enforce_sms_send_budget,
    send_code_sms,
)
from .badges import compute_badges
from .comms import comms_credentials
from .pg import connection
from .points import maybe_credit_profile_completion
from .ratelimit import enforce

log = logging.getLogger(__name__)

router = APIRouter()

_RATE_PLANS = ("resident", "visitor", "equity")
_MAX_FAVORITES = 100
_MAX_THEME_LEN = 64
_MAX_TITLE_LEN = 64
# Mirrors accounts_ruling_alpha_range (sql/044). Duplicated here only so
# the rejection is a 422 naming the field rather than a CheckViolation
# surfacing as a 500 — the DB remains the enforcement point.
_MIN_RULING_ALPHA = 0.10
_MAX_RULING_ALPHA = 1.00
# Shared by both username-mutating endpoints below — they change the same
# field, so one combined cap (not one each) is what actually limits abuse.
_LIMIT_USERNAME_REROLL_PER_ACCOUNT = (10, 3600)
# Guesses against a texted verification code. The code itself is already
# attempt-capped per code (MAX_CODE_ATTEMPTS); this caps how many fresh
# codes an account can grind through.
_LIMIT_PHONE_VERIFY_PER_ACCOUNT = (10, 3600)


class ProfileUpdate(BaseModel):
    rate_plan: str | None = Field(default=None)
    theme: str | None = Field(default=None, max_length=_MAX_THEME_LEN)
    favorites: list[Any] | None = Field(default=None, max_length=_MAX_FAVORITES)
    email: str | None = Field(default=None, max_length=320)
    phone_number: str | None = Field(default=None, max_length=32)
    show_public_username: bool | None = Field(default=None)
    show_in_leaderboards: bool | None = Field(default=None)
    home_lat: float | None = Field(default=None, ge=-90, le=90)
    home_lng: float | None = Field(default=None, ge=-180, le=180)
    work_lat: float | None = Field(default=None, ge=-90, le=90)
    work_lng: float | None = Field(default=None, ge=-180, le=180)
    # sql/044. royalty_title and the two colours are validated by FK
    # against the curated tables rather than by a list in this module —
    # extending the palette is then a migration, not a code change, and
    # the two cannot disagree about what is choosable.
    royalty_title: str | None = Field(default=None, max_length=_MAX_TITLE_LEN)
    ruling_color: str | None = Field(default=None, max_length=7)
    ruling_border_color: str | None = Field(default=None, max_length=7)
    ruling_alpha: float | None = Field(
        default=None, ge=_MIN_RULING_ALPHA, le=_MAX_RULING_ALPHA
    )


class UsernameChoice(BaseModel):
    adjective: str | None = Field(default=None, max_length=32)
    emoji: str | None = Field(default=None, max_length=16)


def _profile_payload(cur, user: SessionUser) -> dict[str, Any]:
    cur.execute(
        """
        SELECT email, phone_number, public_username, show_public_username,
               show_in_leaderboards, rate_plan, theme, favorites,
               home_lat, home_lng, work_lat, work_lng,
               royalty_title, ruling_color, ruling_border_color, ruling_alpha,
               display_name, phone_verified_at, sms_opted_out_at
        FROM accounts WHERE id = %s
        """,
        (user.account_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(401, "account no longer exists")
    (email, phone_number, public_username, show_public_username,
     show_in_leaderboards, rate_plan, theme, favorites,
     home_lat, home_lng, work_lat, work_lng,
     royalty_title, ruling_color, ruling_border_color, ruling_alpha,
     display_name, phone_verified_at, sms_opted_out_at) = row
    return {
        "email": email,
        "phone_number": phone_number,
        # Whether anyone has PROVED they answer that number, by typing back
        # a code texted to it (sql/045). A number typed into the PUT below
        # starts unverified and stays that way: this endpoint writes contact
        # details, and contact details are not proof. Only a verified number
        # can be used to sign in, so the UI needs this to know whether to
        # offer the "verify your number" prompt.
        "phone_verified": phone_verified_at is not None,
        # They texted STOP. Consent is enforced upstream by z280-comms and is
        # global across every application on the shared sender, so this is a
        # local echo for honest UI — not the authority, and not something
        # this API can clear (only an UNSTOP text can).
        "sms_opted_out": sms_opted_out_at is not None,
        "public_username": public_username,
        # Server-computed (sql/044): title + public_username, or just the
        # username when no title is set. Read-only here — it moves by
        # changing royalty_title or the username, never on its own.
        "display_name": display_name,
        "royalty_title": royalty_title,
        "ruling_color": ruling_color,
        "ruling_border_color": ruling_border_color,
        "ruling_alpha": float(ruling_alpha) if ruling_alpha is not None else None,
        "show_public_username": bool(show_public_username),
        "show_in_leaderboards": bool(show_in_leaderboards),
        "rate_plan": rate_plan,
        "theme": theme,
        "favorites": favorites if isinstance(favorites, list) else [],
        "home_lat": home_lat,
        "home_lng": home_lng,
        "work_lat": work_lat,
        "work_lng": work_lng,
        "badges": compute_badges(cur, user.account_id),
    }


def _apply_coord_pair(
    sets: list[str],
    params: list[Any],
    provided: set[str],
    payload: "ProfileUpdate",
    lat_field: str,
    lng_field: str,
) -> None:
    """home_lat/home_lng (and work_lat/work_lng) move together: both given
    as coordinates, both given as null (clear the pin), or neither
    mentioned. A one-sided update would leave a half-set, meaningless pair
    sitting in the row indefinitely."""
    lat_given = lat_field in provided
    lng_given = lng_field in provided
    if lat_given != lng_given:
        raise HTTPException(
            400, f"{lat_field} and {lng_field} must be set together (both, or neither)"
        )
    if not lat_given:
        return
    lat_val = getattr(payload, lat_field)
    lng_val = getattr(payload, lng_field)
    if (lat_val is None) != (lng_val is None):
        raise HTTPException(400, f"{lat_field} and {lng_field} must both be set or both be null")
    sets.append(f"{lat_field} = %s")
    params.append(lat_val)
    sets.append(f"{lng_field} = %s")
    params.append(lng_val)


@router.get("/api/v1/profile")
def get_profile(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            return _profile_payload(cur, user)


@router.put("/api/v1/profile")
def put_profile(
    user: SessionUser = Depends(require_session),
    payload: ProfileUpdate = Body(...),
) -> dict[str, Any]:
    """Partial update: only the fields present in the body change.

    `theme: null` clears the theme; omitting it leaves it alone (pydantic's
    fields_set distinguishes the two) — same idiom for email/phone_number/
    home_lat/home_lng/work_lat/work_lng below.
    """
    sets: list[str] = []
    params: list[Any] = []
    provided = payload.model_fields_set

    with connection() as conn:
        with conn.cursor() as cur:
            # FOR UPDATE: closes the two-concurrent-tabs race where both
            # requests read "email set, phone null" and each independently
            # decides its own half of a null-both-out change is safe.
            cur.execute(
                "SELECT email, phone_number FROM accounts WHERE id = %s FOR UPDATE",
                (user.account_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(401, "account no longer exists")
            current_email, current_phone_number = row

            if "rate_plan" in provided:
                if payload.rate_plan not in _RATE_PLANS:
                    raise HTTPException(400, f"rate_plan must be one of {_RATE_PLANS}")
                sets.append("rate_plan = %s")
                params.append(payload.rate_plan)
            if "theme" in provided:
                sets.append("theme = %s")
                params.append(payload.theme)
            if "favorites" in provided:
                if payload.favorites is None:
                    raise HTTPException(400, "favorites must be an array (use [] to clear)")
                sets.append("favorites = %s::jsonb")
                params.append(json.dumps(payload.favorites))

            new_email = current_email
            if "email" in provided:
                if payload.email is None:
                    new_email = None
                else:
                    new_email = normalize_email(payload.email)
                    if not EMAIL_RE.match(new_email):
                        raise HTTPException(400, "email must be a valid email address")
                sets.append("email = %s")
                params.append(new_email)

            new_phone_number = current_phone_number
            if "phone_number" in provided:
                if payload.phone_number is None:
                    new_phone_number = None
                else:
                    new_phone_number = normalize_phone_number(payload.phone_number)
                    if not is_valid_phone_number(new_phone_number):
                        raise HTTPException(
                            400,
                            "phone_number must be in E.164 format, e.g. +13035551234",
                        )
                sets.append("phone_number = %s")
                params.append(new_phone_number)
                # Verification belongs to a NUMBER, not to an account, so
                # writing a different number here must drop it — otherwise
                # "verified" would transfer from the number somebody proved
                # to one nobody has, and that unverified number would then
                # be a working sign-in key for this account. Unconditional
                # rather than only-when-changed: re-writing the same number
                # loses nothing but a re-verification, and getting the
                # comparison subtly wrong loses the whole guarantee.
                sets.append("phone_verified_at = NULL")

            if new_email is None and new_phone_number is None:
                raise HTTPException(
                    400,
                    "a profile needs an email address or a phone number "
                    "(or both) — add one before removing the other",
                )

            if "show_public_username" in provided:
                if payload.show_public_username is None:
                    raise HTTPException(400, "show_public_username must be true or false")
                sets.append("show_public_username = %s")
                params.append(payload.show_public_username)
            if "show_in_leaderboards" in provided:
                if payload.show_in_leaderboards is None:
                    raise HTTPException(400, "show_in_leaderboards must be true or false")
                sets.append("show_in_leaderboards = %s")
                params.append(payload.show_in_leaderboards)

            _apply_coord_pair(sets, params, provided, payload, "home_lat", "home_lng")
            _apply_coord_pair(sets, params, provided, payload, "work_lat", "work_lng")

            if "royalty_title" in provided:
                # NULL clears the title; display_name falls back to the bare
                # username. Membership is the FK's job (see below).
                sets.append("royalty_title = %s")
                params.append(payload.royalty_title)

            # Fill and border move together for the same reason home_lat and
            # home_lng do — accounts_ruling_colors_coherent rejects a half-set
            # pair, so allowing a one-sided update would just turn a rider's
            # save into a 500 from a constraint they can't see. Both-null
            # clears the pair and gives up the claim.
            colour_fields = {"ruling_color", "ruling_border_color"} & provided
            if colour_fields:
                if len(colour_fields) != 2:
                    raise HTTPException(
                        400,
                        "ruling_color and ruling_border_color must be set together "
                        "(both, or both null to clear)",
                    )
                if (payload.ruling_color is None) != (payload.ruling_border_color is None):
                    raise HTTPException(
                        400,
                        "ruling_color and ruling_border_color must both be set or both be null",
                    )
                if (payload.ruling_color is not None
                        and payload.ruling_color == payload.ruling_border_color):
                    raise HTTPException(
                        400,
                        "the border colour must differ from the fill colour — "
                        "a border in the fill's own colour isn't visible",
                    )
                sets.append("ruling_color = %s")
                params.append(payload.ruling_color)
                sets.append("ruling_border_color = %s")
                params.append(payload.ruling_border_color)

            if "ruling_alpha" in provided:
                if payload.ruling_alpha is None:
                    raise HTTPException(
                        400,
                        f"ruling_alpha must be a number between {_MIN_RULING_ALPHA} "
                        f"and {_MAX_RULING_ALPHA}",
                    )
                sets.append("ruling_alpha = %s")
                params.append(payload.ruling_alpha)

            if sets:
                params.append(user.account_id)
                try:
                    cur.execute(
                        f"UPDATE accounts SET {', '.join(sets)} WHERE id = %s", params
                    )
                except psycopg.errors.UniqueViolation as e:
                    constraint = getattr(e.diag, "constraint_name", None) or ""
                    if constraint == "accounts_phone_number_key":
                        raise HTTPException(
                            409, "that phone number is already in use by another account"
                        )
                    if constraint == "accounts_email_key":
                        raise HTTPException(
                            409, "that email address is already in use by another account"
                        )
                    if constraint == "accounts_ruling_pair_key":
                        raise HTTPException(
                            409,
                            "that fill and border colour combination is already "
                            "claimed — pick a different border, or a different fill",
                        )
                    raise HTTPException(409, "that value is already in use by another account")
                except psycopg.errors.ForeignKeyViolation as e:
                    # The curated lists are the single source of truth for
                    # what's choosable (sql/044), so an unknown value is
                    # caught by the FK rather than by a second copy of the
                    # list in this module that could fall out of date.
                    constraint = getattr(e.diag, "constraint_name", None) or ""
                    if "royalty_title" in constraint:
                        raise HTTPException(
                            400,
                            "that isn't one of the available titles — "
                            "see GET /api/v1/royalty-titles",
                        )
                    if "ruling_color" in constraint or "ruling_border_color" in constraint:
                        raise HTTPException(
                            400,
                            "that isn't one of the available colours — "
                            "see GET /api/v1/ruling-colors",
                        )
                    raise
                # Requirement #10's "complete missing profile information"
                # bonus — checked after any successful accounts write since
                # this update could be the one that newly satisfies it.
                # Idempotent (no-op if already awarded), so unconditional.
                maybe_credit_profile_completion(cur, user.account_id)
            result = _profile_payload(cur, user)
        conn.commit()
    return result


@router.post("/api/v1/profile/username/regenerate")
def regenerate_public_username(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    """Re-roll the caller's public_username to a new random
    adjective+emoji pair. See PUT /api/v1/profile/username to choose a
    specific pair instead of a random one."""
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(
                cur, bucket="profile_username_reroll_account", key=str(user.account_id),
                limit=_LIMIT_USERNAME_REROLL_PER_ACCOUNT[0],
                window_seconds=_LIMIT_USERNAME_REROLL_PER_ACCOUNT[1],
            )
            new_username = assign_public_username(cur, user.account_id)
        conn.commit()
    return {"public_username": new_username}


@router.put("/api/v1/profile/username")
def set_public_username(
    user: SessionUser = Depends(require_session),
    payload: UsernameChoice = Body(...),
) -> dict[str, Any]:
    """Choose a specific adjective and/or emoji (each validated against
    the curated sfw_adjectives/emoji_nouns lists — never free text).
    Either field may be omitted to keep the account's current value for
    that half; at least one must be provided."""
    if payload.adjective is None and payload.emoji is None:
        raise HTTPException(400, "provide at least one of adjective or emoji")
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(
                cur, bucket="profile_username_reroll_account", key=str(user.account_id),
                limit=_LIMIT_USERNAME_REROLL_PER_ACCOUNT[0],
                window_seconds=_LIMIT_USERNAME_REROLL_PER_ACCOUNT[1],
            )
            try:
                new_username = choose_public_username(
                    cur, user.account_id, adjective=payload.adjective, emoji=payload.emoji
                )
            except InvalidUsernameChoice as e:
                raise HTTPException(400, str(e))
            except psycopg.errors.UniqueViolation:
                raise HTTPException(
                    409, "that username is already taken — try a different word or emoji"
                )
        conn.commit()
    return {"public_username": new_username}


# ---------------------------------------------------------------------------
# POST /api/v1/profile/phone/code  +  POST /api/v1/profile/phone/verify
# ---------------------------------------------------------------------------
# Proving the number on YOUR OWN account, from inside a session you already
# have. Without this pair, phone_verified_at could only ever be set by
# accounts.upsert_account_by_phone — which creates a NEW account — so a rider
# who typed their number into their profile and then used SMS sign-in would
# quietly end up with two accounts and wonder where their rides went. Here
# the proof lands on the account they are already signed into.
#
# The message says "verify your number", not "login": a rider who gets a text
# they did not ask for should be able to tell from the text itself what it
# would do if they read it out to someone.
#
# No site name here either — comms prefixes "scooter.fyi: " server-side.
# Delivered as: "scooter.fyi: Use code AB123XY to verify your number."
SMS_VERIFY_TEMPLATE = "Use code {code} to verify your number."


class PhoneCodeRequest(BaseModel):
    # Optional: default to whatever is already on the profile, so the common
    # case ("verify the number you can see on screen") needs no body at all.
    phone_number: str | None = Field(default=None, max_length=32)


class PhoneCodeVerify(BaseModel):
    phone_number: str = Field(..., min_length=7, max_length=32)
    code: str = Field(..., min_length=1, max_length=32)


def _resolve_phone_to_verify(cur, user: SessionUser, supplied: str | None) -> str:
    raw = supplied
    if raw is None:
        cur.execute("SELECT phone_number FROM accounts WHERE id = %s", (user.account_id,))
        row = cur.fetchone()
        raw = row[0] if row else None
        if not raw:
            raise HTTPException(400, "no phone number on file — add one first")
    phone = normalize_us_phone(raw)
    if not phone:
        raise HTTPException(400, "enter a US phone number, like (303) 555-1212")
    return phone


@router.post("/api/v1/profile/phone/code", status_code=202)
def request_phone_verification_code(
    user: SessionUser = Depends(require_session),
    payload: PhoneCodeRequest = Body(default=PhoneCodeRequest()),
) -> dict[str, Any]:
    """Text a code to the number you want to prove you answer."""
    if not comms_credentials():
        raise HTTPException(503, "SMS is not configured")

    with connection() as conn:
        with conn.cursor() as cur:
            phone = _resolve_phone_to_verify(cur, user, payload.phone_number)
            # Deliberately the SAME budget the sign-in door draws on: one
            # handset, one plan. Keyed on the phone and (here) the account
            # rather than an IP, since this door always has a session.
            enforce_sms_send_budget(cur, phone=phone, ip=str(user.account_id))
            code, code_id = _issue_code(
                cur, column="phone_number", destination=phone,
                ttl_minutes=SMS_CODE_TTL_MINUTES, ip=None,
            )
        conn.commit()

    # After commit, exactly as the auth doors do: a comms outage must not
    # roll back the rate-limit events into a free retry loop.
    send_code_sms(
        phone,
        SMS_VERIFY_TEMPLATE.format(code=code),
        idempotency_key=f"login-code-{code_id}",
        purpose="phone_verification",
    )
    return {"sent": True, "phone_number": phone}


@router.post("/api/v1/profile/phone/verify")
def verify_phone_number(
    user: SessionUser = Depends(require_session),
    payload: PhoneCodeVerify = Body(...),
) -> dict[str, Any]:
    """Type the code back to attach the verified number to this account.

    Same login_codes row, same attempt cap and single-use burn as sign-in —
    the code is keyed to the NUMBER, and this endpoint differs only in what
    success buys: a verified column on the session's account instead of a
    new session.
    """
    phone = normalize_us_phone(payload.phone_number)
    if not phone:
        raise HTTPException(400, "enter a US phone number, like (303) 555-1212")
    code = _normalize_code(payload.code)

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(
                cur, bucket="phone_verify_account", key=str(user.account_id),
                limit=_LIMIT_PHONE_VERIFY_PER_ACCOUNT[0],
                window_seconds=_LIMIT_PHONE_VERIFY_PER_ACCOUNT[1],
            )
            _verify_code(conn, cur, column="phone_number", destination=phone, code=code)
            try:
                claim_verified_phone(cur, user.account_id, phone)
            except PhoneNumberTaken:
                conn.commit()
                raise HTTPException(
                    409, "that number is already verified on another account"
                )
            except PhoneNumberContested:
                conn.commit()
                raise HTTPException(
                    409,
                    "that number is attached to another account that can't be "
                    "released automatically — email support to sort it out",
                )
        conn.commit()
    return {"phone_number": phone, "phone_verified": True}
