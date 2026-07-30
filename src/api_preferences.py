"""Rider preference blobs (sql/043_user_preferences.sql, sql/050_ride_mode_usuals.sql).

    GET    /api/v1/profile/map-settings           every saved setting
    GET    /api/v1/profile/map-settings/{name}    one, by name
    PUT    /api/v1/profile/map-settings/{name}    create or replace
    DELETE /api/v1/profile/map-settings/{name}
    GET    /api/v1/profile/find-ride-pref         null when never set
    PUT    /api/v1/profile/find-ride-pref         create or replace
    DELETE /api/v1/profile/find-ride-pref
    GET    /api/v1/profile/ride-usuals            every saved Usual
    GET    /api/v1/profile/ride-usuals/{name}     one, by name
    PUT    /api/v1/profile/ride-usuals/{name}     create or replace
    DELETE /api/v1/profile/ride-usuals/{name}

`settings` is an opaque, client-owned JSON object. This module never reads
inside it, never merges it, and never validates its shape: it is the
frontend's state, and a backend that understood its contents would become
a second place that has to change whenever the map gains a layer toggle.
PUT REPLACES the blob wholesale for that reason — a partial merge would
require knowing which keys are meaningful.

Everything here is scoped to the caller's own account. There is no
cross-account read of a preference at any visibility, so unlike the
profile's public_username there is no privacy toggle to honour.

Separate from src/api_profile.py despite the shared URL prefix: that
module owns the accounts ROW (one row, many columns, one PUT that
validates them together), and this one owns a child table with its own
cardinality rules. Merging them would put two unrelated transaction
shapes in one handler.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import psycopg
from fastapi import APIRouter, Body, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from .accounts import SessionUser, require_session
from .pg import connection

log = logging.getLogger(__name__)

router = APIRouter()

# Product limits, deliberately here and not in the migration — see
# sql/043's header. A limit change should be a code change.
MAX_SAVED_MAP_SETTINGS = 50
# Ride Mode "Usuals" (sql/050). Ten, not fifty: a Usual is picked from a
# scrolling list on Screen 2.5 mid-wizard, so a rider who can save fifty of
# them has built something slower to search than re-setting eight toggles.
MAX_RIDE_USUALS = 10
MAX_BLOB_BYTES = 16 * 1024
MAX_NAME_LENGTH = 64  # mirrors user_preferences_name_length

_MAP_KIND = "saved_map_settings"
_FIND_RIDE_KIND = "find_ride_pref"
_USUAL_KIND = "ride_mode_usual"


class PreferenceIn(BaseModel):
    settings: dict[str, Any] = Field(
        ...,
        description="Opaque client-owned JSON object, stored and returned verbatim.",
    )


def _serialize(settings: dict[str, Any]) -> str:
    """JSON text for storage, size-checked.

    Measured on the SERIALIZED bytes rather than key count or depth: the
    limit exists to bound what one account can make the database and every
    subsequent response carry, and that is a byte count.
    """
    blob = json.dumps(settings)
    if len(blob.encode("utf-8")) > MAX_BLOB_BYTES:
        raise HTTPException(
            413,
            f"settings blob is larger than the {MAX_BLOB_BYTES // 1024} KB limit",
        )
    return blob


def _row(name: str | None, settings: Any, created_at, updated_at) -> dict[str, Any]:
    return {
        "name": name,
        "settings": settings if isinstance(settings, dict) else {},
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
    }


def _enforce_named_cap(
    cur, *, account_id: int, kind: str, name: str, cap: int, plural: str
) -> None:
    """Serialize against the per-account cap for one named-blob kind.

    Shared by both named kinds (saved map settings and Usuals) rather than
    copied, because a divergence between the two copies would be silent:
    both the row lock and the `name <> %s` are load-bearing subtleties, and
    a Usuals handler that dropped either would still pass a single-request
    test.

    * Counting and inserting in one transaction still admits two concurrent
      inserts both seeing count = cap - 1. Locking the account row
      serializes them, reusing the FOR UPDATE idiom api_profile.py:
      put_profile already applies to this same row.
    * The count EXCLUDES the name being written, so the cap binds only the
      insert path — a rider at the cap can still overwrite something they
      already have, which is the difference between a limit on how much you
      may store and a lock on your own data.
    """
    cur.execute("SELECT 1 FROM accounts WHERE id = %s FOR UPDATE", (account_id,))
    if cur.fetchone() is None:
        raise HTTPException(401, "account no longer exists")

    cur.execute(
        """
        SELECT COUNT(*) FROM user_preferences
        WHERE account_id = %s AND kind = %s AND name <> %s
        """,
        (account_id, kind, name),
    )
    (others,) = cur.fetchone()
    if others >= cap:
        raise HTTPException(
            409,
            f"you already have {cap} {plural} — delete one before adding another",
        )


# ---------------------------------------------------------------------------
# Saved map settings — many per account, addressed by name
# ---------------------------------------------------------------------------
@router.get("/api/v1/profile/map-settings")
def list_map_settings(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, settings, created_at, updated_at
                FROM user_preferences
                WHERE account_id = %s AND kind = %s
                ORDER BY updated_at DESC
                """,
                (user.account_id, _MAP_KIND),
            )
            rows = cur.fetchall()
    return {"map_settings": [_row(*r) for r in rows]}


@router.get("/api/v1/profile/map-settings/{name}")
def get_map_setting(
    name: str = Path(..., min_length=1, max_length=MAX_NAME_LENGTH),
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, settings, created_at, updated_at
                FROM user_preferences
                WHERE account_id = %s AND kind = %s AND name = %s
                """,
                (user.account_id, _MAP_KIND, name),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(404, f"no saved map setting named {name!r}")
    return _row(*row)


@router.put("/api/v1/profile/map-settings/{name}")
def put_map_setting(
    name: str = Path(..., min_length=1, max_length=MAX_NAME_LENGTH),
    user: SessionUser = Depends(require_session),
    payload: PreferenceIn = Body(...),
) -> dict[str, Any]:
    """Create or replace one named setting.

    The per-account cap is checked INSIDE the upsert's transaction and
    only on the insert path — a rider at the cap can still overwrite a
    setting they already have, which is the difference between a limit on
    how much you may store and a lock on your own data.
    """
    blob = _serialize(payload.settings)
    with connection() as conn:
        with conn.cursor() as cur:
            _enforce_named_cap(
                cur,
                account_id=user.account_id,
                kind=_MAP_KIND,
                name=name,
                cap=MAX_SAVED_MAP_SETTINGS,
                plural="saved map settings",
            )

            cur.execute(
                """
                INSERT INTO user_preferences (account_id, kind, name, settings)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (account_id, name) WHERE kind = 'saved_map_settings'
                DO UPDATE SET settings = EXCLUDED.settings, updated_at = NOW()
                RETURNING name, settings, created_at, updated_at
                """,
                (user.account_id, _MAP_KIND, name, blob),
            )
            row = cur.fetchone()
        conn.commit()
    return _row(*row)


@router.delete("/api/v1/profile/map-settings/{name}")
def delete_map_setting(
    name: str = Path(..., min_length=1, max_length=MAX_NAME_LENGTH),
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_preferences "
                "WHERE account_id = %s AND kind = %s AND name = %s",
                (user.account_id, _MAP_KIND, name),
            )
            deleted = cur.rowcount
        conn.commit()
    if not deleted:
        raise HTTPException(404, f"no saved map setting named {name!r}")
    return {"deleted": True, "name": name}


# ---------------------------------------------------------------------------
# Find-ride preference — at most one per account
# ---------------------------------------------------------------------------
@router.get("/api/v1/profile/find-ride-pref")
def get_find_ride_pref(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    """`find_ride_pref: null` means the rider has never set one.

    Deliberately not an empty object: the frontend has to be able to tell
    "no preference expressed" from "expressed, and empty" — see sql/043's
    header on why no account is seeded with a default.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, settings, created_at, updated_at
                FROM user_preferences
                WHERE account_id = %s AND kind = %s
                """,
                (user.account_id, _FIND_RIDE_KIND),
            )
            row = cur.fetchone()
    return {"find_ride_pref": _row(*row) if row else None}


@router.put("/api/v1/profile/find-ride-pref")
def put_find_ride_pref(
    user: SessionUser = Depends(require_session),
    payload: PreferenceIn = Body(...),
) -> dict[str, Any]:
    blob = _serialize(payload.settings)
    with connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO user_preferences (account_id, kind, name, settings)
                    VALUES (%s, %s, NULL, %s::jsonb)
                    ON CONFLICT (account_id) WHERE kind = 'find_ride_pref'
                    DO UPDATE SET settings = EXCLUDED.settings, updated_at = NOW()
                    RETURNING name, settings, created_at, updated_at
                    """,
                    (user.account_id, _FIND_RIDE_KIND, blob),
                )
            except psycopg.errors.ForeignKeyViolation:
                raise HTTPException(401, "account no longer exists")
            row = cur.fetchone()
        conn.commit()
    return {"find_ride_pref": _row(*row)}


@router.delete("/api/v1/profile/find-ride-pref")
def delete_find_ride_pref(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    """Idempotent: deleting an absent preference is not an error.

    Unlike a named map setting — where a 404 tells the caller they got the
    name wrong — there is only one of these, so "it isn't there" is the
    state the caller asked for.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_preferences WHERE account_id = %s AND kind = %s",
                (user.account_id, _FIND_RIDE_KIND),
            )
        conn.commit()
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Ride Mode "Usuals" — many per account, addressed by name (sql/050)
# ---------------------------------------------------------------------------
# A Usual is a saved answer to the ride wizard's Screen 2 options panel:
# the frontend's ride_options object plus a display `label`, applied
# wholesale from Screen 2.5 (RIDE_MODE_OVERHAUL_PLAN §1.2).
#
# Deliberately the map-settings handlers again with a different kind, cap and
# noun — same cardinality (many, one per name), so the same 16 KB blob cap,
# the same 1–64 character names, the same 404-on-a-name-that-isn't-yours, the
# same 409 at the cap that still lets you edit what you already have.
#
# THE BLOB STAYS OPAQUE HERE, even though its shape is known and even though
# api_tracked_rides._serialize_ride_options validates that shape strictly.
# Those are different jobs. That function guards a ride the server will award
# points for, so a truthy string where `save_tracks` belongs would silently
# decide a rider's eligibility. A Usual is a draft of an intention: it awards
# nothing, gates nothing, and is validated the moment it is used to start a
# ride — by the one function that owns that vocabulary. Duplicating the check
# here would put an API deploy in front of every new client-side toggle (the
# cross-repo ordering edge the program plan avoids) and would make a rider's
# already-saved Usual un-editable the day the vocabulary changes.
@router.get("/api/v1/profile/ride-usuals")
def list_ride_usuals(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, settings, created_at, updated_at
                FROM user_preferences
                WHERE account_id = %s AND kind = %s
                ORDER BY updated_at DESC
                """,
                (user.account_id, _USUAL_KIND),
            )
            rows = cur.fetchall()
    return {"ride_usuals": [_row(*r) for r in rows]}


@router.get("/api/v1/profile/ride-usuals/{name}")
def get_ride_usual(
    name: str = Path(..., min_length=1, max_length=MAX_NAME_LENGTH),
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, settings, created_at, updated_at
                FROM user_preferences
                WHERE account_id = %s AND kind = %s AND name = %s
                """,
                (user.account_id, _USUAL_KIND, name),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(404, f"no ride usual named {name!r}")
    return _row(*row)


@router.put("/api/v1/profile/ride-usuals/{name}")
def put_ride_usual(
    name: str = Path(..., min_length=1, max_length=MAX_NAME_LENGTH),
    user: SessionUser = Depends(require_session),
    payload: PreferenceIn = Body(...),
) -> dict[str, Any]:
    """Create or replace one named Usual.

    The kind is what keeps this out of the map-settings namespace: the
    ON CONFLICT predicate names sql/050's partial unique index, so the
    (account, 'commute') row this touches is a different row from the
    saved map setting the same rider may also call 'commute'.
    """
    blob = _serialize(payload.settings)
    with connection() as conn:
        with conn.cursor() as cur:
            _enforce_named_cap(
                cur,
                account_id=user.account_id,
                kind=_USUAL_KIND,
                name=name,
                cap=MAX_RIDE_USUALS,
                plural="saved ride usuals",
            )

            cur.execute(
                """
                INSERT INTO user_preferences (account_id, kind, name, settings)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (account_id, name) WHERE kind = 'ride_mode_usual'
                DO UPDATE SET settings = EXCLUDED.settings, updated_at = NOW()
                RETURNING name, settings, created_at, updated_at
                """,
                (user.account_id, _USUAL_KIND, name, blob),
            )
            row = cur.fetchone()
        conn.commit()
    return _row(*row)


@router.delete("/api/v1/profile/ride-usuals/{name}")
def delete_ride_usual(
    name: str = Path(..., min_length=1, max_length=MAX_NAME_LENGTH),
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_preferences "
                "WHERE account_id = %s AND kind = %s AND name = %s",
                (user.account_id, _USUAL_KIND, name),
            )
            deleted = cur.rowcount
        conn.commit()
    if not deleted:
        raise HTTPException(404, f"no ride usual named {name!r}")
    return {"deleted": True, "name": name}
