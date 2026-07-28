"""Off-feed rides — rides on vehicles the audit does not track.

    POST   /api/v1/rides/start              begin a ride, stream to it
    GET    /api/v1/rides/active             the caller's one active ride
    POST   /api/v1/rides/{id}/waypoints     append a GPS fix
    GET    /api/v1/rides/{id}/waypoints     owner-only, paginated
    PATCH  /api/v1/rides/{id}/end           report the end (single-shot)

    POST   /api/v1/rides                    one-shot log of a finished ride
    GET    /api/v1/rides                    owner-only list, newest first
    GET    /api/v1/rides/export             owner-only, ?format=geojson|csv
    DELETE /api/v1/rides/{id}               HARD delete one ride
    DELETE /api/v1/rides                    HARD delete everything

A vehicle here has no vehicle_identifier by definition: it isn't in the
GBFS feed. A personal scooter, a competitor's rental, a friend's e-bike.
The rider describes it (vehicle_kind + free-text operator) instead.
Contrast src/api_tracked_rides.py, which is GBFS-detected against a
specific Veo vehicle and is the mechanism to use when there IS one.

Two ways in, because riders arrive with two different situations:

  - The lifecycle (start -> waypoints -> end) for a ride happening now.
    Distance is measured server-side from the track.
  - The one-shot POST for a ride already over, where the client computed
    everything. Distance is whatever the client says (distance_source
    'client') — we have no way to check it.

NO POINTS ARE AWARDED ANYWHERE IN THIS MODULE, deliberately. Points exist
to reward data about the public fleet, and every fact here is
rider-asserted about a vehicle we cannot see. Tracked rides can pay for
waypoints because they are anchored to a real vehicle_identifier and
corroborated against the GBFS feed; an off-feed ride is anchored to
nothing, so paying per waypoint would be an unbounded, unfalsifiable
points faucet. See src/points.py.

Privacy stance (stronger than most of this codebase, inherited from
sql/014 and unchanged by the repurposing): route polylines are the most
sensitive data the system holds. Deletes are immediate hard DELETEs — no
soft-delete column, no tombstone — the waypoint rows cascade with them,
and no other module may query these tables for analytics. Both commitments
are stated publicly in /api/v1/meta/privacy; breaking either is a breach
of that page.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from psycopg import errors as pg_errors

from .accounts import SessionUser, require_session
from .geo import distance_meters, path_length_meters
from .pg import connection
from .polyline import PolylineError, decode as decode_polyline, encode as encode_polyline
from .ratelimit import enforce

log = logging.getLogger(__name__)

router = APIRouter()

_LIMIT_RIDES_PER_ACCOUNT = (120, 86400)
_LIMIT_START_PER_ACCOUNT = (20, 3600)
# Matches tracked rides: ~1 fix every 6 seconds sustained. Clients should
# buffer and flush rather than posting every GPS callback.
_LIMIT_WAYPOINT_PER_ACCOUNT = (600, 3600)
_MAX_POLYLINE_CHARS = 100_000
_VEHICLE_KINDS = "^(scooter|bicycle|other)$"


class RideIn(BaseModel):
    """A finished ride, computed client-side. `polyline` is required here —
    a one-shot log with no route is just a row of numbers."""
    started_at: datetime
    ended_at: datetime
    duration_s: int = Field(..., ge=0, le=86_400)
    distance_m: int = Field(..., ge=0, le=200_000)
    est_cost_cents: int | None = Field(default=None, ge=0, le=100_000)
    rate_plan: str | None = Field(default=None, pattern="^(resident|visitor|equity)$")
    started_in_zone: bool
    ended_in_zone: bool
    polyline: str = Field(..., min_length=1, max_length=_MAX_POLYLINE_CHARS)
    vehicle_kind: str | None = Field(default=None, pattern=_VEHICLE_KINDS)
    operator: str | None = Field(default=None, max_length=64)


class RideStartIn(BaseModel):
    start_lat: float = Field(..., ge=-90, le=90)
    start_lon: float = Field(..., ge=-180, le=180)
    vehicle_kind: str | None = Field(default=None, pattern=_VEHICLE_KINDS)
    operator: str | None = Field(default=None, max_length=64)
    # Optional so a client that noticed late can backdate the start rather
    # than silently losing the first minutes of the ride.
    started_at: datetime | None = Field(default=None)


class RideEndIn(BaseModel):
    ended_at: datetime
    end_lat: float = Field(..., ge=-90, le=90)
    end_lon: float = Field(..., ge=-180, le=180)
    est_cost_cents: int | None = Field(default=None, ge=0, le=100_000)
    rate_plan: str | None = Field(default=None, pattern="^(resident|visitor|equity)$")
    started_in_zone: bool | None = Field(default=None)
    ended_in_zone: bool | None = Field(default=None)


class WaypointIn(BaseModel):
    waypoint_at: datetime
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    metadata: dict[str, Any] | None = Field(default=None)


_RIDE_COLS = (
    "id, created_at, started_at, ended_at, duration_s, distance_m, "
    "est_cost_cents, rate_plan, started_in_zone, ended_in_zone, polyline, "
    "status, vehicle_kind, operator, start_lat, start_lon, end_lat, end_lon, "
    "distance_source"
)


def _row_to_ride(r) -> dict[str, Any]:
    return {
        "id": str(r[0]),
        "created_at": r[1].isoformat(),
        "started_at": r[2].isoformat(),
        "ended_at": r[3].isoformat() if r[3] is not None else None,
        "duration_s": int(r[4]) if r[4] is not None else None,
        "distance_m": int(r[5]) if r[5] is not None else None,
        "est_cost_cents": int(r[6]) if r[6] is not None else None,
        "rate_plan": r[7],
        "started_in_zone": bool(r[8]) if r[8] is not None else None,
        "ended_in_zone": bool(r[9]) if r[9] is not None else None,
        "polyline": r[10],
        "status": r[11],
        "vehicle_kind": r[12],
        "operator": r[13],
        "start_lat": r[14],
        "start_lon": r[15],
        "end_lat": r[16],
        "end_lon": r[17],
        "distance_source": r[18],
    }


def _parse_ride_id(ride_id: str) -> UUID:
    try:
        return UUID(ride_id)
    except ValueError:
        raise HTTPException(400, "ride id must be a UUID")


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None:
        raise HTTPException(400, f"{field} must include a UTC offset")


def _parse_before(before: str | None, field: str) -> datetime | None:
    if not before:
        return None
    try:
        parsed = datetime.fromisoformat(before.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(400, f"bad before timestamp: {e}")
    if parsed.tzinfo is None:
        # A naive datetime compared against TIMESTAMPTZ is ambiguous —
        # psycopg would assume the server's local timezone, which is not
        # necessarily what the client meant. Require an explicit offset.
        raise HTTPException(400, "before must include a timezone (e.g. trailing Z)")
    return parsed


def _rebuild_track(cur, rid: UUID) -> None:
    """Recompute polyline + distance from the ride's full ordered waypoint
    set. Reads every row rather than appending incrementally because
    waypoints can arrive out of order (client retry, offline buffer), and
    an incremental append would silently corrupt both.

    The ride's start point leads the path. The rider was already moving
    between where they started and wherever their first GPS fix landed —
    dropping that leg undercounts every ride by the length of the first
    sampling gap. A client whose first waypoint IS the start contributes a
    zero-length leg, which costs nothing.
    """
    cur.execute("SELECT start_lat, start_lon FROM rides WHERE id = %s", (str(rid),))
    row = cur.fetchone()
    head = [(row[0], row[1])] if row and row[0] is not None and row[1] is not None else []
    cur.execute(
        "SELECT lat, lon FROM off_feed_ride_waypoints WHERE ride_id = %s "
        "ORDER BY waypoint_at ASC, id ASC",
        (str(rid),),
    )
    points = head + cur.fetchall()
    cur.execute(
        "UPDATE rides SET polyline = %s, distance_m = %s, "
        "distance_source = 'waypoints' WHERE id = %s",
        (encode_polyline(points), round(path_length_meters(points)), str(rid)),
    )


# ---------------------------------------------------------------------------
# Lifecycle. Registered before the /{ride_id} routes below — Starlette
# matches path-shaped routes in registration order, so 'start' and 'active'
# would otherwise be swallowed as ride ids.
# ---------------------------------------------------------------------------

@router.post("/api/v1/rides/start")
def start_ride(
    user: SessionUser = Depends(require_session),
    payload: RideStartIn = Body(...),
) -> dict[str, Any]:
    if payload.started_at is not None:
        _require_utc(payload.started_at, "started_at")

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="off_feed_ride_start_account", key=str(user.account_id),
                    limit=_LIMIT_START_PER_ACCOUNT[0],
                    window_seconds=_LIMIT_START_PER_ACCOUNT[1])
            try:
                cur.execute(
                    f"""
                    INSERT INTO rides (
                        account_id, started_at, status, vehicle_kind, operator,
                        start_lat, start_lon
                    ) VALUES (%s, COALESCE(%s, NOW()), 'active', %s, %s, %s, %s)
                    RETURNING {_RIDE_COLS}
                    """,
                    (user.account_id, payload.started_at, payload.vehicle_kind,
                     payload.operator, payload.start_lat, payload.start_lon),
                )
                row = cur.fetchone()
            except pg_errors.UniqueViolation:
                # idx_rides_one_active_per_account (sql/035). The database
                # is the arbiter, so two simultaneous starts can't both win.
                raise HTTPException(409, "an active off-feed ride already exists")
        conn.commit()
    return _row_to_ride(row)


@router.get("/api/v1/rides/active")
def active_ride(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_RIDE_COLS} FROM rides "
                "WHERE account_id = %s AND status = 'active'",
                (user.account_id,),
            )
            row = cur.fetchone()
    if row is None:
        return {"active": None}
    return {"active": _row_to_ride(row)}


@router.post("/api/v1/rides/{ride_id}/waypoints")
def add_waypoint(
    ride_id: str,
    user: SessionUser = Depends(require_session),
    payload: WaypointIn = Body(...),
) -> dict[str, Any]:
    rid = _parse_ride_id(ride_id)
    _require_utc(payload.waypoint_at, "waypoint_at")

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="off_feed_ride_waypoint_account", key=str(user.account_id),
                    limit=_LIMIT_WAYPOINT_PER_ACCOUNT[0],
                    window_seconds=_LIMIT_WAYPOINT_PER_ACCOUNT[1])
            cur.execute(
                "SELECT status FROM rides WHERE id = %s AND account_id = %s",
                (str(rid), user.account_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "no such ride")
            if row[0] != "active":
                raise HTTPException(409, {
                    "error": "ride_not_active",
                    "detail": "cannot add waypoints to a ride that isn't active",
                })

            cur.execute(
                """
                INSERT INTO off_feed_ride_waypoints (
                    ride_id, account_id, waypoint_at, lat, lon, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id, created_at
                """,
                (str(rid), user.account_id, payload.waypoint_at, payload.lat,
                 payload.lon, json.dumps(payload.metadata or {})),
            )
            new_id, created_at = cur.fetchone()
            _rebuild_track(cur, rid)
        conn.commit()
    return {
        "id": int(new_id), "ride_id": str(rid),
        "waypoint_at": payload.waypoint_at.isoformat(),
        "lat": payload.lat, "lon": payload.lon,
        "metadata": payload.metadata or {},
        "created_at": created_at.isoformat(),
    }


@router.get("/api/v1/rides/{ride_id}/waypoints")
def list_waypoints(
    ride_id: str,
    user: SessionUser = Depends(require_session),
    limit: int = Query(500, ge=1, le=5000),
    before: str | None = Query(None, description="ISO timestamp — waypoints recorded before this"),
) -> dict[str, Any]:
    rid = _parse_ride_id(ride_id)
    where = ["ride_id = %s", "account_id = %s"]
    params: list[Any] = [str(rid), user.account_id]
    parsed = _parse_before(before, "before")
    if parsed is not None:
        params.append(parsed)
        where.append("waypoint_at < %s")
    params.append(limit)

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM rides WHERE id = %s AND account_id = %s",
                (str(rid), user.account_id),
            )
            if cur.fetchone() is None:
                raise HTTPException(404, "no such ride")
            cur.execute(
                f"""
                SELECT id, waypoint_at, lat, lon, metadata, created_at
                FROM off_feed_ride_waypoints
                WHERE {' AND '.join(where)}
                ORDER BY waypoint_at ASC, id ASC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
    waypoints = [
        {"id": int(r[0]), "waypoint_at": r[1].isoformat(), "lat": r[2], "lon": r[3],
         "metadata": r[4] if isinstance(r[4], dict) else {}, "created_at": r[5].isoformat()}
        for r in rows
    ]
    return {"count": len(waypoints), "waypoints": waypoints}


@router.patch("/api/v1/rides/{ride_id}/end")
def end_ride(
    ride_id: str,
    user: SessionUser = Depends(require_session),
    payload: RideEndIn = Body(...),
) -> dict[str, Any]:
    rid = _parse_ride_id(ride_id)
    _require_utc(payload.ended_at, "ended_at")

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, started_at, start_lat, start_lon, distance_source "
                "FROM rides WHERE id = %s AND account_id = %s FOR UPDATE",
                (str(rid), user.account_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "no such ride")
            status, started_at, start_lat, start_lon, distance_source = row
            if status != "active":
                raise HTTPException(409, "this ride has already ended")
            if payload.ended_at < started_at:
                raise HTTPException(400, "ended_at < started_at")

            # A tracked path beats the crow-flies fallback, so a ride with
            # waypoints keeps what _rebuild_track already measured. Only a
            # ride with no track at all falls back to start->end, which
            # undercounts any route that isn't a straight line — hence
            # recording which one produced the number (sql/035).
            if distance_source == "waypoints":
                # distance_m already holds the tracked length — leave it be.
                distance_sql = "distance_source = 'waypoints'"
                distance_params: tuple = ()
            else:
                straight = round(distance_meters(
                    start_lat, start_lon, payload.end_lat, payload.end_lon))
                distance_sql = "distance_m = %s, distance_source = 'straight_line'"
                distance_params = (straight,)

            cur.execute(
                f"""
                UPDATE rides SET
                    status = 'completed',
                    ended_at = %s,
                    end_lat = %s,
                    end_lon = %s,
                    duration_s = GREATEST(0, EXTRACT(EPOCH FROM (%s - started_at))::int),
                    est_cost_cents = %s,
                    rate_plan = COALESCE(%s, rate_plan),
                    started_in_zone = COALESCE(%s, started_in_zone, FALSE),
                    ended_in_zone = COALESCE(%s, ended_in_zone, FALSE),
                    polyline = COALESCE(polyline, ''),
                    {distance_sql}
                WHERE id = %s
                RETURNING {_RIDE_COLS}
                """,
                (payload.ended_at, payload.end_lat, payload.end_lon, payload.ended_at,
                 payload.est_cost_cents, payload.rate_plan, payload.started_in_zone,
                 payload.ended_in_zone, *distance_params, str(rid)),
            )
            ride = _row_to_ride(cur.fetchone())
        conn.commit()
    return ride


# ---------------------------------------------------------------------------
# One-shot log + history
# ---------------------------------------------------------------------------

@router.post("/api/v1/rides")
def create_ride(
    user: SessionUser = Depends(require_session),
    payload: RideIn = Body(...),
) -> dict[str, Any]:
    _require_utc(payload.started_at, "started_at")
    _require_utc(payload.ended_at, "ended_at")
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
                    est_cost_cents, rate_plan, started_in_zone, ended_in_zone,
                    polyline, status, vehicle_kind, operator, distance_source
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          'completed', %s, %s, 'client')
                RETURNING {_RIDE_COLS}
                """,
                (user.account_id, payload.started_at, payload.ended_at,
                 payload.duration_s, payload.distance_m, payload.est_cost_cents,
                 payload.rate_plan, payload.started_in_zone, payload.ended_in_zone,
                 payload.polyline, payload.vehicle_kind, payload.operator),
            )
            row = cur.fetchone()
        conn.commit()
    return _row_to_ride(row)


@router.get("/api/v1/rides")
def list_rides(
    user: SessionUser = Depends(require_session),
    limit: int = Query(50, ge=1, le=500),
    before: str | None = Query(None, description="ISO timestamp — return rides started before this"),
    status: str | None = Query(None, pattern="^(active|completed)$"),
) -> dict[str, Any]:
    where = ["account_id = %s"]
    params: list[Any] = [user.account_id]
    parsed = _parse_before(before, "before")
    if parsed is not None:
        params.append(parsed)
        where.append("started_at < %s")
    if status:
        params.append(status)
        where.append("status = %s")
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
                "distance_source", "est_cost_cents", "rate_plan",
                "started_in_zone", "ended_in_zone", "status", "vehicle_kind",
                "operator", "polyline"]
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
            coords = [[lon, lat] for lat, lon in decode_polyline(ride["polyline"] or "")]
        except PolylineError:
            coords = []  # validated at ingest; belt-and-suspenders
        props = {k: v for k, v in ride.items() if k != "polyline"}
        features.append({
            "type": "Feature",
            "id": ride["id"],
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": props,
        })
    return Response(
        content=json.dumps({"type": "FeatureCollection", "features": features}),
        media_type="application/geo+json",
        headers={"Content-Disposition": 'attachment; filename="rides.geojson"'},
    )


@router.delete("/api/v1/rides/{ride_id}")
def delete_ride(
    ride_id: str,
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    """Hard delete, cascading to the ride's waypoints. 404 for both 'not
    yours' and 'doesn't exist' — no existence oracle across accounts."""
    rid = _parse_ride_id(ride_id)
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
    """Hard delete every ride the account owns, waypoints included.
    Immediate and final."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM rides WHERE account_id = %s", (user.account_id,))
            deleted = cur.rowcount
        conn.commit()
    log.info("off-feed rides wiped: account=%d count=%d", user.account_id, deleted)
    return {"deleted_count": int(deleted)}
