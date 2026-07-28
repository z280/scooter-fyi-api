"""Rider preference blobs (sql/043_user_preferences.sql).

    GET    /api/v1/profile/map-settings           every saved setting
    GET    /api/v1/profile/map-settings/{name}    one, by name
    PUT    /api/v1/profile/map-settings/{name}    create or replace
    DELETE /api/v1/profile/map-settings/{name}
    GET    /api/v1/profile/find-ride-pref         null when never set
    PUT    /api/v1/profile/find-ride-pref         create or replace
    DELETE /api/v1/profile/find-ride-pref

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
MAX_BLOB_BYTES = 16 * 1024
MAX_NAME_LENGTH = 64  # mirrors user_preferences_name_length

_MAP_KIND = "saved_map_settings"
_FIND_RIDE_KIND = "find_ride_pref"


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
            # Counting and inserting in one transaction still admits two
            # concurrent inserts both seeing count = MAX - 1. Locking the
            # account row serializes them, reusing the FOR UPDATE idiom
            # api_profile.py:put_profile already applies to this same row.
            cur.execute("SELECT 1 FROM accounts WHERE id = %s FOR UPDATE", (user.account_id,))
            if cur.fetchone() is None:
                raise HTTPException(401, "account no longer exists")

            cur.execute(
                """
                SELECT COUNT(*) FROM user_preferences
                WHERE account_id = %s AND kind = %s AND name <> %s
                """,
                (user.account_id, _MAP_KIND, name),
            )
            (others,) = cur.fetchone()
            if others >= MAX_SAVED_MAP_SETTINGS:
                raise HTTPException(
                    409,
                    f"you already have {MAX_SAVED_MAP_SETTINGS} saved map settings — "
                    "delete one before adding another",
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
