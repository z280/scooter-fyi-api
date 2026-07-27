"""POST /api/v1/devices/{vehicle_identifier}/recommend (requirement #11;
sql/030_device_recommendations.sql)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from pydantic import BaseModel

from .accounts import SessionUser, require_session
from .pg import connection
from .ratelimit import enforce

router = APIRouter()

_LIMIT_RECOMMEND_PER_ACCOUNT = (30, 3600)
_VEHICLE_IDENTIFIER_RE = r"^[0-9a-f]{16}$"


class RecommendIn(BaseModel):
    recommend: bool


@router.post("/api/v1/devices/{vehicle_identifier}/recommend")
def recommend_device(
    payload: RecommendIn = Body(...),
    vehicle_identifier: str = Path(..., min_length=16, max_length=16, pattern=_VEHICLE_IDENTIFIER_RE),
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    """Only accepted when the account has a completed ride against this
    vehicle_identifier started in the last 24h (checked against
    tracked_rides, sql/027). No points are awarded for this action — it's
    absent from the points list."""
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="device_recommend_account", key=str(user.account_id),
                    limit=_LIMIT_RECOMMEND_PER_ACCOUNT[0],
                    window_seconds=_LIMIT_RECOMMEND_PER_ACCOUNT[1])
            cur.execute(
                """
                SELECT 1 FROM tracked_rides
                WHERE account_id = %s AND vehicle_identifier = %s
                  AND status = 'completed'
                  AND started_at >= NOW() - INTERVAL '24 hours'
                LIMIT 1
                """,
                (user.account_id, vehicle_identifier),
            )
            if cur.fetchone() is None:
                raise HTTPException(403, "no completed ride on this device in the last 24 hours")
            cur.execute(
                """
                INSERT INTO device_recommendations (account_id, vehicle_identifier, recommend)
                VALUES (%s, %s, %s)
                ON CONFLICT (account_id, vehicle_identifier) DO UPDATE SET
                    recommend = EXCLUDED.recommend, updated_at = NOW()
                RETURNING id, created_at, updated_at
                """,
                (user.account_id, vehicle_identifier, payload.recommend),
            )
            row_id, created_at, updated_at = cur.fetchone()
        conn.commit()
    return {
        "id": int(row_id), "vehicle_identifier": vehicle_identifier,
        "recommend": payload.recommend,
        "created_at": created_at.isoformat(), "updated_at": updated_at.isoformat(),
    }
