"""Profile endpoints (API_REQUIREMENTS.md §2.4).

    GET /api/v1/profile   full profile incl. server-computed fields
    PUT /api/v1/profile   partial update of the client-writable fields

Client-writable: rate_plan, theme, favorites (opaque JSON array — its
shape lands with the favorite-device-types spec, we just store it).
Server-computed, read-only: supporter (Stripe webhook), badges
(recomputed on every read — see src/badges.py).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from .accounts import SessionUser, require_session
from .badges import compute_badges
from .pg import connection

log = logging.getLogger(__name__)

router = APIRouter()

_RATE_PLANS = ("resident", "visitor", "equity")
_MAX_FAVORITES = 100
_MAX_THEME_LEN = 64


class ProfileUpdate(BaseModel):
    rate_plan: str | None = Field(default=None)
    theme: str | None = Field(default=None, max_length=_MAX_THEME_LEN)
    favorites: list[Any] | None = Field(default=None, max_length=_MAX_FAVORITES)


def _profile_payload(cur, user: SessionUser) -> dict[str, Any]:
    cur.execute(
        "SELECT email, rate_plan, theme, favorites, supporter FROM accounts WHERE id = %s",
        (user.account_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(401, "account no longer exists")
    email, rate_plan, theme, favorites, supporter = row
    return {
        "email": email,
        "rate_plan": rate_plan,
        "theme": theme,
        "favorites": favorites if isinstance(favorites, list) else [],
        "supporter": bool(supporter),
        "badges": compute_badges(cur, user.account_id, supporter=bool(supporter)),
    }


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
    fields_set distinguishes the two).
    """
    sets: list[str] = []
    params: list[Any] = []
    provided = payload.model_fields_set

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

    with connection() as conn:
        with conn.cursor() as cur:
            if sets:
                params.append(user.account_id)
                cur.execute(
                    f"UPDATE accounts SET {', '.join(sets)} WHERE id = %s", params
                )
            result = _profile_payload(cur, user)
        conn.commit()
    return result
