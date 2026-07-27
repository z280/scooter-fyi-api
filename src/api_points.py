"""GET /api/v1/points — the caller's points ledger + running total
(requirement #10)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from .accounts import SessionUser, require_session
from .pg import connection

router = APIRouter()


@router.get("/api/v1/points")
def my_points(
    user: SessionUser = Depends(require_session),
    limit: int = Query(100, ge=1, le=1000),
    before: str | None = Query(None, description="ISO timestamp — entries created before this"),
) -> dict[str, Any]:
    where = ["account_id = %s"]
    params: list[Any] = [user.account_id]
    if before:
        try:
            parsed = datetime.fromisoformat(before.replace("Z", "+00:00"))
        except ValueError as e:
            raise HTTPException(400, f"bad before timestamp: {e}")
        if parsed.tzinfo is None:
            raise HTTPException(400, "before must include a timezone (e.g. trailing Z)")
        params.append(parsed)
        where.append("created_at < %s")
    params.append(limit)

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(points), 0) FROM user_points "
                "WHERE account_id = %s AND status = 'confirmed'",
                (user.account_id,),
            )
            (total,) = cur.fetchone()
            cur.execute(
                f"""
                SELECT id, created_at, action, points, vehicle_identifier, status
                FROM user_points
                WHERE {' AND '.join(where)}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
    return {
        "total_points": int(total),
        "entries": [
            {"id": int(r[0]), "created_at": r[1].isoformat(), "action": r[2],
             "points": int(r[3]), "vehicle_identifier": r[4], "status": r[5]}
            for r in rows
        ],
    }
