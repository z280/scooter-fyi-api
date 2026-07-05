"""Supporter ride history (API_REQUIREMENTS.md §4.2).

    POST   /api/v1/rides           log a ride (supporter required)
    GET    /api/v1/rides           owner-only list, newest first, paginated
    GET    /api/v1/rides/export    owner-only, ?format=geojson|csv
    DELETE /api/v1/rides/{id}      HARD delete one ride
    DELETE /api/v1/rides           HARD delete everything

Privacy stance (stronger than most of this codebase): route polylines are
the most sensitive data the system holds. Deletes are immediate hard
DELETEs — no soft-delete column, no tombstone — and no other module may
query the rides table for analytics. Both commitments are stated publicly
in /api/v1/meta/privacy; breaking either is a breach of that page.

Logging a ride requires supporter; reading and deleting your own rides
only requires the session — a lapsed supporter can always export and
delete what they already logged.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from .accounts import SessionUser, require_session, require_supporter
from .pg import connection
from .polyline import PolylineError, decode as decode_polyline
from .ratelimit import enforce

log = logging.getLogger(__name__)

router = APIRouter()

_LIMIT_RIDES_PER_ACCOUNT = (120, 86400)
_MAX_POLYLINE_CHARS = 100_000


class RideIn(BaseModel):
    started_at: datetime
    ended_at: datetime
    duration_s: int = Field(..., ge=0, le=86_400)
    distance_m: int = Field(..., ge=0, le=200_000)
    est_cost_cents: int | None = Field(default=None, ge=0, le=100_000)
    rate_plan: str = Field(..., pattern="^(resident|visitor|equity)$")
    started_in_zone: bool
    ended_in_zone: bool
    polyline: str = Field(..., min_length=1, max_length=_MAX_POLYLINE_CHARS)


def _row_to_ride(r) -> dict[str, Any]:
    return {
        "id": str(r[0]),
        "created_at": r[1].isoformat(),
        "started_at": r[2].isoformat(),
        "ended_at": r[3].isoformat(),
        "duration_s": int(r[4]),
        "distance_m": int(r[5]),
        "est_cost_cents": int(r[6]) if r[6] is not None else None,
        "rate_plan": r[7],
        "started_in_zone": bool(r[8]),
        "ended_in_zone": bool(r[9]),
        "polyline": r[10],
    }


_RIDE_COLS = (
    "id, created_at, started_at, ended_at, duration_s, distance_m, "
    "est_cost_cents, rate_plan, started_in_zone, ended_in_zone, polyline"
)


@router.post("/api/v1/rides")
def create_ride(
    user: SessionUser = Depends(require_supporter),
    payload: RideIn = Body(...),
) -> dict[str, Any]:
    if payload.started_at.tzinfo is None or payload.ended_at.tzinfo is None:
        raise HTTPException(400, "started_at/ended_at must include a UTC offset")
    if payload.ended_at < payload.started_at:
        raise HTTPException(400, "ended_at < started_at")
    try:
        decode_polyline(payload.polyline)
    except PolylineError as e:
        raise HTTPException(400, f"polyline won't decode: {e}")

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="rides_account", key=str(user.account_id),
                    limit=_LIMIT_RIDES_PER_ACCOUNT[0],
                    window_seconds=_LIMIT_RIDES_PER_ACCOUNT[1])
            cur.execute(
                f"""
                INSERT INTO rides (
                    account_id, started_at, ended_at, duration_s, distance_m,
                    est_cost_cents, rate_plan, started_in_zone, ended_in_zone, polyline
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_RIDE_COLS}
                """,
                (user.account_id, payload.started_at, payload.ended_at,
                 payload.duration_s, payload.distance_m, payload.est_cost_cents,
                 payload.rate_plan, payload.started_in_zone, payload.ended_in_zone,
                 payload.polyline),
            )
            row = cur.fetchone()
        conn.commit()
    return _row_to_ride(row)


@router.get("/api/v1/rides")
def list_rides(
    user: SessionUser = Depends(require_session),
    limit: int = Query(50, ge=1, le=500),
    before: str | None = Query(None, description="ISO timestamp — return rides started before this"),
) -> dict[str, Any]:
    where = ["account_id = %s"]
    params: list[Any] = [user.account_id]
    if before:
        try:
            parsed = datetime.fromisoformat(before.replace("Z", "+00:00"))
        except ValueError as e:
            raise HTTPException(400, f"bad before timestamp: {e}")
        if parsed.tzinfo is None:
            # A naive datetime compared against TIMESTAMPTZ is ambiguous —
            # psycopg would assume the server's local timezone, which is
            # not necessarily what the client meant. Require an explicit
            # offset (Z or +HH:MM) instead of silently guessing.
            raise HTTPException(400, "before must include a timezone (e.g. trailing Z)")
        params.append(parsed)
        where.append("started_at < %s")
    params.append(limit)

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_RIDE_COLS} FROM rides
                WHERE {' AND '.join(where)}
                ORDER BY started_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
    rides = [_row_to_ride(r) for r in rows]
    return {"count": len(rides), "rides": rides}


@router.get("/api/v1/rides/export")
def export_rides(
    user: SessionUser = Depends(require_session),
    format: str = Query(..., pattern="^(geojson|csv)$"),
) -> Response:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_RIDE_COLS} FROM rides WHERE account_id = %s "
                "ORDER BY started_at ASC",
                (user.account_id,),
            )
            rows = cur.fetchall()
    rides = [_row_to_ride(r) for r in rows]

    if format == "csv":
        buf = io.StringIO()
        cols = ["id", "started_at", "ended_at", "duration_s", "distance_m",
                "est_cost_cents", "rate_plan", "started_in_zone", "ended_in_zone",
                "polyline"]
        w = csv.writer(buf)
        w.writerow(cols)
        for ride in rides:
            w.writerow([ride[c] for c in cols])
        return Response(
            content=buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="rides.csv"'},
        )

    features = []
    for ride in rides:
        try:
            coords = [[lon, lat] for lat, lon in decode_polyline(ride["polyline"])]
        except PolylineError:
            coords = []  # validated at ingest; belt-and-suspenders
        props = {k: v for k, v in ride.items() if k != "polyline"}
        features.append({
            "type": "Feature",
            "id": ride["id"],
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": props,
        })
    import json as _json
    return Response(
        content=_json.dumps({"type": "FeatureCollection", "features": features}),
        media_type="application/geo+json",
        headers={"Content-Disposition": 'attachment; filename="rides.geojson"'},
    )


@router.delete("/api/v1/rides/{ride_id}")
def delete_ride(
    ride_id: str,
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    """Hard delete. 404 for both 'not yours' and 'doesn't exist' — no
    existence oracle across accounts."""
    try:
        rid = UUID(ride_id)
    except ValueError:
        raise HTTPException(400, "ride id must be a UUID")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM rides WHERE id = %s AND account_id = %s",
                (rid, user.account_id),
            )
            deleted = cur.rowcount
        conn.commit()
    if not deleted:
        raise HTTPException(404, "no such ride")
    return {"deleted": True}


@router.delete("/api/v1/rides")
def delete_all_rides(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    """Hard delete every ride the account owns. Immediate and final."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM rides WHERE account_id = %s", (user.account_id,))
            deleted = cur.rowcount
        conn.commit()
    log.info("rides wiped: account=%d count=%d", user.account_id, deleted)
    return {"deleted_count": int(deleted)}
