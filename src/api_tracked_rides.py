"""Server-detected ride tracking (requirements items 5-9; sql/027_tracked_rides.sql).

    POST   /api/v1/tracked-rides                    start a ride + watch
    GET    /api/v1/tracked-rides                     owner-only paginated list
    GET    /api/v1/tracked-rides/active               the caller's one active ride, if any
    GET    /api/v1/tracked-rides/{ride_id}             one ride's full detail
    PATCH  /api/v1/tracked-rides/{ride_id}/end         rider-reported end (single-shot)
    POST   /api/v1/tracked-rides/{ride_id}/waypoints   append a waypoint
    GET    /api/v1/tracked-rides/{ride_id}/waypoints   paginated waypoint list
    DELETE /api/v1/tracked-rides/{ride_id}             hard-delete one ride
    DELETE /api/v1/tracked-rides                       hard-delete every ride the account owns

Deliberately separate from the `rides` table, which tracks OFF-FEED rides
on vehicles with no vehicle_identifier (src/api_rides.py,
sql/035_off_feed_rides.sql). Use this module when there IS a GBFS vehicle
to anchor to, that one when there isn't. Every endpoint here is
`require_session`
(open to all riders — signed-in is the only gate this product has).

ANTI-FRAUD: the points system pays a bonus when the GBFS-observed
reappearance is within 20m of the rider's own reported end location. If a
rider could see the GBFS answer before submitting their report, they could
tune their guess to land inside that window — so the four `gbfs_*` fields
are nulled out in every response until `user_reported_ended_at` is set
(the underlying columns are always populated normally; this is a
response-layer redaction only), and the end-report endpoint is single-shot.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .accounts import SessionUser, require_session
from .geo import distance_meters
from .identity import plate_display_code
from .pg import connection
from .points import credit_gbfs_validation_points, credit_waypoint_points
from .polyline import PolylineError, decode as decode_polyline, encode as encode_polyline
from .ratelimit import enforce
from .ride_limits import (
    MAX_LEG_METERS,
    MAX_RIDE_DISTANCE_METERS,
    clamp_distance,
    close_out_path as _close_out,
    leg_is_plausible,
    measure_path,
    partial_source,
)

router = APIRouter()

WATCH_DURATION_HOURS = 3
_LIMIT_START_RIDE_PER_ACCOUNT = (20, 3600)
_LIMIT_WAYPOINT_PER_ACCOUNT = (600, 3600)
_VEHICLE_IDENTIFIER_RE = r"^[0-9a-f]{16}$"

_RIDE_COLS = (
    "id, status, started_at, start_lat, start_lon, watch_expires_at, "
    "gbfs_left_feed_at, gbfs_reappeared_at, gbfs_end_lat, gbfs_end_lon, "
    "gbfs_end_battery_percent, user_reported_ended_at, end_lat, end_lon, "
    "reported_battery_percent, total_cost_cents, metadata, path_polyline, "
    "vehicle_identifier, created_at, updated_at, distance_meters, "
    "distance_source, distance_clamped_from_m"
)


class StartRideIn(BaseModel):
    vehicle_identifier: str = Field(..., min_length=16, max_length=16, pattern=_VEHICLE_IDENTIFIER_RE)
    start_lat: float = Field(..., ge=-90, le=90)
    start_lon: float = Field(..., ge=-180, le=180)


class EndRideIn(BaseModel):
    ended_at: datetime
    end_lat: float = Field(..., ge=-90, le=90)
    end_lon: float = Field(..., ge=-180, le=180)
    reported_battery_percent: float | None = Field(default=None, ge=0, le=100)
    total_cost_cents: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] | None = Field(default=None)


class WaypointIn(BaseModel):
    waypoint_at: datetime
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    metadata: dict[str, Any] | None = Field(default=None)


def _row_to_ride(r: tuple, *, path_geojson: bool = True) -> dict[str, Any]:
    (ride_id, status, started_at, start_lat, start_lon, watch_expires_at,
     gbfs_left_feed_at, gbfs_reappeared_at, gbfs_end_lat, gbfs_end_lon,
     gbfs_end_battery_percent, user_reported_ended_at, end_lat, end_lon,
     reported_battery_percent, total_cost_cents, metadata, path_polyline,
     vehicle_identifier, created_at, updated_at, ride_distance_meters,
     distance_source, distance_clamped_from_m) = r

    # ANTI-FRAUD: see module docstring. Redacted as None in the API
    # response only — the underlying columns are untouched.
    reported = user_reported_ended_at is not None
    out = {
        "id": str(ride_id),
        "status": status,
        "started_at": started_at.isoformat(),
        "start_lat": start_lat,
        "start_lon": start_lon,
        "watch_expires_at": watch_expires_at.isoformat(),
        "gbfs_left_feed_at": gbfs_left_feed_at.isoformat() if (reported and gbfs_left_feed_at) else None,
        "gbfs_reappeared_at": gbfs_reappeared_at.isoformat() if (reported and gbfs_reappeared_at) else None,
        "gbfs_end_lat": gbfs_end_lat if reported else None,
        "gbfs_end_lon": gbfs_end_lon if reported else None,
        "gbfs_end_battery_percent": gbfs_end_battery_percent if reported else None,
        "user_reported_ended_at": user_reported_ended_at.isoformat() if user_reported_ended_at else None,
        "end_lat": end_lat,
        "end_lon": end_lon,
        "reported_battery_percent": float(reported_battery_percent) if reported_battery_percent is not None else None,
        "total_cost_cents": total_cost_cents,
        "metadata": metadata if isinstance(metadata, dict) else {},
        "vehicle_identifier": vehicle_identifier,
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        # Not redacted with the gbfs_* fields above: this is derived from
        # the rider's OWN waypoints or their own reported end, so showing
        # it back to them reveals nothing they didn't tell us.
        "distance_meters": (
            round(float(ride_distance_meters), 1)
            if ride_distance_meters is not None else None
        ),
        "distance_source": distance_source,
        # NULL unless the operator's 80 km ride cap bound; see api_rides.py.
        # Derived from the rider's own data like distance itself, so it is
        # not part of the gbfs_* redaction.
        "distance_clamped_from_m": (
            round(float(distance_clamped_from_m), 1)
            if distance_clamped_from_m is not None else None
        ),
    }
    if path_geojson:
        out["path_polyline"] = path_polyline
        if path_polyline:
            try:
                coords = [[lon, lat] for lat, lon in decode_polyline(path_polyline)]
            except PolylineError:
                coords = []
            out["path_geojson"] = {"type": "LineString", "coordinates": coords}
        else:
            out["path_geojson"] = None
    return out


def _plate_display_code_for(cur, vehicle_identifier: str) -> str | None:
    cur.execute(
        "SELECT vehicle_plate FROM device_state WHERE vehicle_identifier = %s",
        (vehicle_identifier,),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    return plate_display_code(row[0])


def _parse_ride_id(ride_id: str) -> UUID:
    try:
        return UUID(ride_id)
    except ValueError:
        raise HTTPException(400, "ride id must be a UUID")


def _parse_before(before: str | None, field: str = "before") -> datetime | None:
    if not before:
        return None
    try:
        parsed = datetime.fromisoformat(before.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(400, f"bad {field} timestamp: {e}")
    if parsed.tzinfo is None:
        raise HTTPException(400, f"{field} must include a timezone (e.g. trailing Z)")
    return parsed


def _track_points(cur, rid: UUID) -> list[tuple[float, float]]:
    """The ride's waypoints as (lat, lon), oldest first. Read whole rather
    than appended incrementally because waypoints can arrive out of order
    (client retry/offline buffering)."""
    cur.execute(
        "SELECT lat, lon FROM ride_waypoints WHERE tracked_ride_id = %s "
        "ORDER BY waypoint_at ASC, id ASC",
        (str(rid),),
    )
    return [(r[0], r[1]) for r in cur.fetchall()]


def _measured_path(
    start_lat: float | None, start_lon: float | None,
    track: list[tuple[float, float]],
    end_lat: float | None = None, end_lon: float | None = None,
) -> list[tuple[float, float]]:
    """The full path we are willing to claim we measured, in order:

        ride start -> every uploaded GPS fix -> rider-reported end

    BOTH ends matter, for the same reason. The rider was already moving
    between where they started and wherever their first GPS fix landed, and
    they kept moving between their LAST fix and where they parked — a phone
    that backgrounded, saved battery or went through a tunnel stops
    producing fixes long before the ride stops. Dropping either leg
    undercounts the ride by a sampling gap, and the trailing gap is
    routinely the whole ride.

    Byte-for-byte the same rule as src/api_rides.py:_measured_path, and it
    has to stay that way: src/badges.py sums distance across both tables, so
    a rider's mileage must not depend on which mechanism logged the ride.

    Callers now pass only start + track; closing the path with the reported
    end is ride_limits.close_out_path's job, which both tables share so the
    "must stay that way" above is enforced by there being one copy rather
    than by whoever edits next remembering.
    """
    points: list[tuple[float, float]] = []
    if start_lat is not None and start_lon is not None:
        points.append((start_lat, start_lon))
    points.extend(track)
    if end_lat is not None and end_lon is not None:
        points.append((end_lat, end_lon))
    return points


def _ordered_track(cur, rid: UUID) -> list[tuple[datetime, float, float]]:
    """(waypoint_at, lat, lon), oldest first — _track_points plus the
    timestamp, so a new fix can be placed where it will actually land."""
    cur.execute(
        "SELECT waypoint_at, lat, lon FROM ride_waypoints "
        "WHERE tracked_ride_id = %s ORDER BY waypoint_at ASC, id ASC",
        (str(rid),),
    )
    return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def _prospective_path(
    cur, rid: UUID, start_lat: float | None, start_lon: float | None,
    new_at: datetime, new_lat: float, new_lon: float,
) -> tuple[list[tuple[float, float]], int]:
    """The path this ride WOULD have if `new` were appended, and the index
    the new point takes in it. Same contract and same reasoning as
    src/api_rides.py:_prospective_path — waypoints arrive out of order, so
    a new fix can land mid-path and create two new adjacencies."""
    existing = _ordered_track(cur, rid)
    idx = sum(1 for at, _, _ in existing if at <= new_at)
    track = [(lat, lon) for _, lat, lon in existing]
    track.insert(idx, (new_lat, new_lon))
    points = _measured_path(start_lat, start_lon, track)
    lead = 1 if (start_lat is not None and start_lon is not None) else 0
    return points, idx + lead


def _check_appendable(points: list[tuple[float, float]], idx: int) -> None:
    """Operator leg cap + ride cap, enforced at append.

    Byte-for-byte the same rule as src/api_rides.py:_check_appendable, and
    it has to stay that way: src/badges.py sums distance across both
    tables, so what each will record must not depend on which one you are
    talking to.
    """
    for a, b in ((idx - 1, idx), (idx, idx + 1)):
        if a < 0 or b >= len(points):
            continue
        if not leg_is_plausible(points[a], points[b]):
            gap = distance_meters(*points[a], *points[b])
            raise HTTPException(422, {
                "error": "waypoint_too_far",
                "detail": f"this fix is {gap:.0f} m from the adjacent point on "
                          f"the ride's path, above the {MAX_LEG_METERS:.0f} m "
                          "limit between consecutive points. The fix was not "
                          "recorded; the ride is still active and the next one "
                          "will be accepted normally.",
            })

    measured, _ = measure_path(points, cap_legs=True)
    if measured > MAX_RIDE_DISTANCE_METERS:
        raise HTTPException(422, {
            "error": "ride_distance_cap_reached",
            "detail": f"this fix would put the ride at {measured:.0f} m, above "
                      f"the {MAX_RIDE_DISTANCE_METERS:.0f} m limit for a single "
                      "ride. The fix was not recorded. End this ride and start "
                      "a new one to keep logging.",
        })


@router.post("/api/v1/tracked-rides")
def start_ride(
    user: SessionUser = Depends(require_session),
    payload: StartRideIn = Body(...),
) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="tracked_ride_start_account", key=str(user.account_id),
                    limit=_LIMIT_START_RIDE_PER_ACCOUNT[0],
                    window_seconds=_LIMIT_START_RIDE_PER_ACCOUNT[1])

            cur.execute(
                "SELECT 1 FROM device_state WHERE vehicle_identifier = %s",
                (payload.vehicle_identifier,),
            )
            if cur.fetchone() is None:
                raise HTTPException(404, "unknown vehicle_identifier")

            # Advisory-lock the account to close the TOCTOU where two
            # near-simultaneous start requests both pass the active-ride
            # check before either commits (mirrors ratelimit.enforce's own
            # check-then-act technique).
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"tracked_ride_start:{user.account_id}",),
            )
            cur.execute(
                """
                SELECT 1 FROM tracked_rides
                WHERE account_id = %s AND user_reported_ended_at IS NULL
                  AND gbfs_reappeared_at IS NULL AND watch_expires_at > NOW()
                LIMIT 1
                """,
                (user.account_id,),
            )
            if cur.fetchone() is not None:
                raise HTTPException(409, "an active ride already exists")

            cur.execute(
                """
                INSERT INTO tracked_rides (
                    account_id, vehicle_identifier, start_lat, start_lon, watch_expires_at
                ) VALUES (%s, %s, %s, %s, NOW() + make_interval(hours => %s))
                RETURNING id, watch_expires_at
                """,
                (user.account_id, payload.vehicle_identifier,
                 payload.start_lat, payload.start_lon, WATCH_DURATION_HOURS),
            )
            ride_id, watch_expires_at = cur.fetchone()
            cur.execute(
                """
                INSERT INTO user_device_watch_list (
                    tracked_ride_id, account_id, vehicle_identifier, watch_expires_at
                ) VALUES (%s, %s, %s, %s)
                """,
                (str(ride_id), user.account_id, payload.vehicle_identifier, watch_expires_at),
            )
            cur.execute(f"SELECT {_RIDE_COLS} FROM tracked_rides WHERE id = %s", (str(ride_id),))
            ride = _row_to_ride(cur.fetchone())
            ride["plate_display_code"] = _plate_display_code_for(cur, payload.vehicle_identifier)
        conn.commit()
    return ride


@router.get("/api/v1/tracked-rides")
def list_tracked_rides(
    user: SessionUser = Depends(require_session),
    limit: int = Query(50, ge=1, le=500),
    before: str | None = Query(None, description="ISO timestamp — return rides started before this"),
    status: str | None = Query(None, pattern="^(watching|left_feed|completed|expired)$"),
) -> dict[str, Any]:
    where = ["account_id = %s"]
    params: list[Any] = [user.account_id]
    parsed_before = _parse_before(before)
    if parsed_before is not None:
        where.append("started_at < %s")
        params.append(parsed_before)
    if status is not None:
        where.append("status = %s")
        params.append(status)
    params.append(limit)

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_RIDE_COLS} FROM tracked_rides
                WHERE {' AND '.join(where)}
                ORDER BY started_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
    # path_geojson omitted here (list view) to keep a multi-ride response
    # bounded — a ride with a long path would otherwise bloat every list
    # call. Full path is available from GET /{ride_id}.
    rides = [_row_to_ride(r, path_geojson=False) for r in rows]
    return {"count": len(rides), "rides": rides}


@router.get("/api/v1/tracked-rides/active")
def active_tracked_ride(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    """Registered before /{ride_id} on purpose — Starlette matches
    path-shaped routes in registration order, so 'active' would otherwise
    be swallowed as a {ride_id} value. The same hazard applies to
    /{ride_id}/waypoints and /{ride_id}/screenshots below."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_RIDE_COLS} FROM tracked_rides
                WHERE account_id = %s AND user_reported_ended_at IS NULL
                  AND gbfs_reappeared_at IS NULL AND watch_expires_at > NOW()
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (user.account_id,),
            )
            row = cur.fetchone()
            if row is None:
                return {"active": None}
            ride = _row_to_ride(row)
            ride["plate_display_code"] = _plate_display_code_for(cur, ride["vehicle_identifier"])
    return {"active": ride}


@router.get("/api/v1/tracked-rides/{ride_id}")
def get_tracked_ride(
    ride_id: str,
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    rid = _parse_ride_id(ride_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_RIDE_COLS} FROM tracked_rides WHERE id = %s AND account_id = %s",
                (str(rid), user.account_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "no such ride")
            ride = _row_to_ride(row)
            ride["plate_display_code"] = _plate_display_code_for(cur, ride["vehicle_identifier"])
    return ride


@router.patch("/api/v1/tracked-rides/{ride_id}/end")
def end_tracked_ride(
    ride_id: str,
    user: SessionUser = Depends(require_session),
    payload: EndRideIn = Body(...),
) -> dict[str, Any]:
    rid = _parse_ride_id(ride_id)
    if payload.ended_at.tzinfo is None:
        raise HTTPException(400, "ended_at must include a UTC offset")

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_reported_ended_at, vehicle_identifier, "
                "gbfs_reappeared_at, gbfs_end_lat, gbfs_end_lon, "
                "start_lat, start_lon "
                "FROM tracked_rides WHERE id = %s AND account_id = %s FOR UPDATE",
                (str(rid), user.account_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "no such ride")
            (already_ended, vehicle_identifier, _gbfs_reappeared_at,
             gbfs_end_lat, gbfs_end_lon, start_lat, start_lon) = row
            if already_ended is not None:
                raise HTTPException(409, "this ride's end has already been reported")

            # Measure the WHOLE path, start -> fixes -> reported end. The end
            # report is the last thing we learn about the ride, so it is the
            # only chance to close the trailing sampling gap; keeping the
            # distance the last waypoint upload happened to leave behind
            # meant the final leg was never measured at all.
            #
            # distance_source stays honest about how the number was reached:
            # 'waypoints' when the rider actually handed us a track,
            # 'straight_line' when the only two points we have are the ends
            # — which undercounts any route that isn't straight (sql/034).
            # NOTHING BELOW THIS LINE CAN REFUSE THE END REPORT. An
            # implausible final leg is dropped and an over-cap distance is
            # clamped; the ride completes either way. Refusing would strand
            # the rider — the active-ride predicate would keep answering
            # "you are still on a ride" until the watch window elapsed.
            track = _track_points(cur, rid)
            points, new_distance, new_source, clamped_from = _close_out(
                start_lat, start_lon, track, payload.end_lat, payload.end_lon)
            # Re-encode the stored path over the same points the distance was
            # measured over, so polyline and distance can't disagree. A ride
            # with no track keeps path_polyline NULL rather than gaining a
            # fabricated two-point "route" it never observed.
            path_sql = "path_polyline = %s," if track else ""
            path_params: tuple = (encode_polyline(points),) if track else ()

            cur.execute(
                f"""
                UPDATE tracked_rides SET
                    status = 'completed',
                    user_reported_ended_at = %s,
                    end_lat = %s,
                    end_lon = %s,
                    reported_battery_percent = %s,
                    total_cost_cents = %s,
                    metadata = %s::jsonb,
                    {path_sql}
                    distance_meters = %s,
                    distance_source = %s,
                    distance_clamped_from_m = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (payload.ended_at, payload.end_lat, payload.end_lon,
                 payload.reported_battery_percent, payload.total_cost_cents,
                 json.dumps(payload.metadata or {}), *path_params,
                 new_distance, new_source, clamped_from, str(rid)),
            )

            # Points (requirement #10), credited now that the ride is
            # confirmed complete — both are no-ops if their condition
            # isn't met (zero waypoints; no GBFS reappearance within 20m).
            cur.execute(
                "SELECT COUNT(*) FROM ride_waypoints WHERE tracked_ride_id = %s",
                (str(rid),),
            )
            (waypoint_count,) = cur.fetchone()
            # Attribute the award to the last location the MEASUREMENT is
            # willing to stand behind, which is not always the reported end.
            # When the final leg was too long to believe, _close_out dropped
            # the reported end from the measured path — and filing the
            # ledger row at that same coordinate would record the rider
            # earning points in a cell the ride itself just declined to
            # claim they reached. That is not cosmetic: user_points.lat/lng
            # and the h3_8_index derived from them ARE the per-area points
            # geography, so one bad GPS fix would otherwise plant a rider's
            # whole ride payout in a hexagon they were never in.
            #
            # The rider's reported end is still stored in end_lat/end_lon
            # untouched — it is their report and we keep it. This governs
            # only where the AWARD is filed.
            award_lat, award_lng = (
                points[-1] if points else (payload.end_lat, payload.end_lon)
            )
            credit_waypoint_points(
                cur, account_id=user.account_id, vehicle_identifier=vehicle_identifier,
                waypoint_count=waypoint_count, end_lat=award_lat, end_lng=award_lng,
                ride_id=str(rid),
            )
            # Deliberately the REPORTED end, not award_lat/award_lng above.
            # This award exists because an independent observation — the
            # vehicle reappearing on GBFS within 20 m — corroborates the
            # reported end, which makes it the best-attested point on the
            # whole ride even in the case where the track's last fix
            # disagrees with it. The two awards can therefore land in
            # different cells, and that is the correct outcome: each is
            # filed where its own evidence puts it.
            credit_gbfs_validation_points(
                cur, account_id=user.account_id, vehicle_identifier=vehicle_identifier,
                end_lat=payload.end_lat, end_lng=payload.end_lon,
                reappear_lat=gbfs_end_lat, reappear_lng=gbfs_end_lon,
                ride_id=str(rid),
            )

            cur.execute(f"SELECT {_RIDE_COLS} FROM tracked_rides WHERE id = %s", (str(rid),))
            ride = _row_to_ride(cur.fetchone())
        conn.commit()
    return ride


@router.post("/api/v1/tracked-rides/{ride_id}/waypoints")
def add_waypoint(
    ride_id: str,
    user: SessionUser = Depends(require_session),
    payload: WaypointIn = Body(...),
) -> dict[str, Any]:
    rid = _parse_ride_id(ride_id)
    if payload.waypoint_at.tzinfo is None:
        raise HTTPException(400, "waypoint_at must include a UTC offset")

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="tracked_ride_waypoint_account", key=str(user.account_id),
                    limit=_LIMIT_WAYPOINT_PER_ACCOUNT[0],
                    window_seconds=_LIMIT_WAYPOINT_PER_ACCOUNT[1])

            cur.execute(
                "SELECT user_reported_ended_at, gbfs_reappeared_at, watch_expires_at, "
                "start_lat, start_lon "
                "FROM tracked_rides WHERE id = %s AND account_id = %s",
                (str(rid), user.account_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "no such ride")
            ended, reappeared, expires_at, start_lat, start_lon = row
            if not (ended is None and reappeared is None and expires_at > datetime.now(timezone.utc)):
                raise HTTPException(409, {"error": "ride_not_active",
                                          "detail": "cannot add waypoints to a ride that isn't active"})

            points, idx = _prospective_path(
                cur, rid, start_lat, start_lon,
                payload.waypoint_at, payload.lat, payload.lon,
            )
            _check_appendable(points, idx)

            cur.execute(
                """
                INSERT INTO ride_waypoints (tracked_ride_id, account_id, waypoint_at, lat, lon, metadata)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id, created_at
                """,
                (str(rid), user.account_id, payload.waypoint_at, payload.lat, payload.lon,
                 json.dumps(payload.metadata or {})),
            )
            new_id, created_at = cur.fetchone()

            # Rebuild path_polyline from the full ordered set — waypoints
            # can arrive out of order (client retry/offline buffering), so
            # an incremental append would silently corrupt the path. The
            # ride's start point leads it (see _measured_path); the ride is
            # still active, so there is no reported end to close it with yet
            # — PATCH .../end recomputes over these same points plus its own
            # end coordinates. Off-feed rides do exactly the same
            # (api_rides.py:_rebuild_track) — badges sum distance across
            # both tables, so the two must measure the same way.
            #
            # `points` from _prospective_path IS that full ordered set: it
            # is the existing track with this fix inserted at the position
            # the same ORDER BY (waypoint_at, id) puts it in, which is what
            # the row we just INSERTed now occupies. Re-reading the track to
            # rebuild the identical list walked it a second time on every
            # append — doubling an already-quadratic cost over a 600-fix
            # ride, for a value that cannot differ inside one transaction.
            rebuilt = points
            # Distance is recomputed from the same full ordered set, for the
            # same reason: an incremental += would be wrong the moment a
            # waypoint arrives out of order. Measured under the operator's
            # leg cap and clamped to the ride cap — neither should bind,
            # because _check_appendable just refused anything that would
            # breach them, but a ride that predates those checks must still
            # come out of here satisfying the invariant.
            measured, excluded = measure_path(rebuilt, cap_legs=True)
            recorded, clamped_from = clamp_distance(measured)
            cur.execute(
                "UPDATE tracked_rides SET path_polyline = %s, distance_meters = %s, "
                "distance_source = %s, distance_clamped_from_m = %s, "
                "updated_at = NOW() WHERE id = %s",
                (encode_polyline(rebuilt), recorded,
                 partial_source("waypoints", partial=excluded > 0),
                 clamped_from, str(rid)),
            )
        conn.commit()
    return {
        "id": int(new_id), "ride_id": str(rid),
        "waypoint_at": payload.waypoint_at.isoformat(),
        "lat": payload.lat, "lon": payload.lon,
        "metadata": payload.metadata or {},
        "created_at": created_at.isoformat(),
    }


@router.get("/api/v1/tracked-rides/{ride_id}/waypoints")
def list_waypoints(
    ride_id: str,
    user: SessionUser = Depends(require_session),
    limit: int = Query(500, ge=1, le=5000),
    after: str | None = Query(None, description="ISO timestamp — the NEXT page: waypoints recorded after this"),
    before: str | None = Query(None, description="ISO timestamp — the PREVIOUS page: the last `limit` waypoints recorded before this"),
) -> dict[str, Any]:
    """Waypoints oldest-first.

    Pagination pairs the cursor with the sort direction, which it did not
    used to: `before` with an ascending sort re-served the OLDEST rows on
    every call, so page 2 was the start of page 1 and nothing past the first
    page was reachable at all. Page forward with `after` (the last
    waypoint_at you received); `before` walks backwards by taking the last
    `limit` rows older than the cursor and returning them oldest-first.
    Same contract as GET /api/v1/rides/{id}/waypoints.
    """
    rid = _parse_ride_id(ride_id)
    parsed_after = _parse_before(after, "after")
    parsed_before = _parse_before(before)

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM tracked_rides WHERE id = %s AND account_id = %s",
                (str(rid), user.account_id),
            )
            if cur.fetchone() is None:
                raise HTTPException(404, "no such ride")

            where = ["tracked_ride_id = %s"]
            params: list[Any] = [str(rid)]
            if parsed_after is not None:
                where.append("waypoint_at > %s")
                params.append(parsed_after)
            if parsed_before is not None:
                where.append("waypoint_at < %s")
                params.append(parsed_before)
            # Walking backwards means "the newest rows older than the
            # cursor", so the LIMIT has to bite from the far end.
            backwards = parsed_before is not None
            order = "DESC" if backwards else "ASC"
            params.append(limit)
            cur.execute(
                f"""
                SELECT id, waypoint_at, lat, lon, metadata, created_at
                FROM ride_waypoints
                WHERE {' AND '.join(where)}
                ORDER BY waypoint_at {order}, id {order}
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
    if backwards:
        rows = list(reversed(rows))
    waypoints = [
        {"id": int(r[0]), "waypoint_at": r[1].isoformat(), "lat": r[2], "lon": r[3],
         "metadata": r[4] if isinstance(r[4], dict) else {}, "created_at": r[5].isoformat()}
        for r in rows
    ]
    return {"count": len(waypoints), "waypoints": waypoints}


@router.delete("/api/v1/tracked-rides/{ride_id}")
def delete_tracked_ride(
    ride_id: str,
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    """Hard delete, cascades to user_device_watch_list + ride_waypoints.
    404 for both 'not yours' and 'doesn't exist' — no existence oracle
    across accounts."""
    rid = _parse_ride_id(ride_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM tracked_rides WHERE id = %s AND account_id = %s",
                (str(rid), user.account_id),
            )
            deleted = cur.rowcount
        conn.commit()
    if not deleted:
        raise HTTPException(404, "no such ride")
    return {"deleted": True}


@router.delete("/api/v1/tracked-rides")
def delete_all_tracked_rides(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    """Hard delete every tracked ride the account owns. Immediate and final."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tracked_rides WHERE account_id = %s", (user.account_id,))
            deleted = cur.rowcount
        conn.commit()
    return {"deleted_count": int(deleted)}
