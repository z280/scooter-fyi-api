"""Profile endpoints (API_REQUIREMENTS.md §2.4, extended for public
usernames / phone numbers / home-work locations).

    GET  /api/v1/profile                      full profile incl. server-computed fields
    PUT  /api/v1/profile                      partial update of the client-writable fields
    POST /api/v1/profile/username/regenerate  re-roll public_username to a new random pair
    PUT  /api/v1/profile/username             choose a specific adjective and/or emoji

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
    SessionUser,
    assign_public_username,
    choose_public_username,
    normalize_email,
    normalize_phone_number,
    is_valid_phone_number,
    require_session,
)
from .badges import compute_badges
from .pg import connection
from .points import maybe_credit_profile_completion
from .ratelimit import enforce

log = logging.getLogger(__name__)

router = APIRouter()

_RATE_PLANS = ("resident", "visitor", "equity")
_MAX_FAVORITES = 100
_MAX_THEME_LEN = 64
# Shared by both username-mutating endpoints below — they change the same
# field, so one combined cap (not one each) is what actually limits abuse.
_LIMIT_USERNAME_REROLL_PER_ACCOUNT = (10, 3600)


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


class UsernameChoice(BaseModel):
    adjective: str | None = Field(default=None, max_length=32)
    emoji: str | None = Field(default=None, max_length=16)


def _profile_payload(cur, user: SessionUser) -> dict[str, Any]:
    cur.execute(
        """
        SELECT email, phone_number, public_username, show_public_username,
               show_in_leaderboards, rate_plan, theme, favorites,
               home_lat, home_lng, work_lat, work_lng
        FROM accounts WHERE id = %s
        """,
        (user.account_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(401, "account no longer exists")
    (email, phone_number, public_username, show_public_username,
     show_in_leaderboards, rate_plan, theme, favorites,
     home_lat, home_lng, work_lat, work_lng) = row
    return {
        "email": email,
        "phone_number": phone_number,
        "public_username": public_username,
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
                    raise HTTPException(409, "that value is already in use by another account")
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
