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
soft-delete column, no tombstone — and the waypoint rows cascade with
them. Both commitments are stated publicly in /api/v1/meta/privacy;
breaking either is a breach of that page.

The third commitment on that page is narrower than it used to be written
here, and is stated precisely because the old wording ("no other module
may query these tables for analytics") is contradicted by the code:
src/badges.py reads BOTH `rides` and `tracked_rides` to compute a rider's
own mileage and streak badges. sql/027 was honest about this and called it
a pre-existing inconsistency; asserting a commitment the repo already
breaks is worse than a narrower one it keeps. What is actually promised,
and what badges.py satisfies:

    NO ROUTE EVER LEAVES ITS OWNER. Waypoints, polylines and ride
    endpoints are readable only on behalf of the account that recorded
    them. They are never aggregated across riders, never published,
    never exported to the compliance dataset, and never used to describe
    the fleet or the city.

badges.py is an owner-scoped read on one account's own history, returning
a boolean-shaped badge to that same rider — not third-party analytics —
and it reads distances and end timestamps, never a coordinate or a
polyline. A module that wants ride ROUTES, or wants any of this across
accounts, is the thing this rule forbids.
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
from .geo import path_length_meters
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

# ---------------------------------------------------------------------------
# Plausibility bounds for a CLIENT-ASSERTED ride (POST /api/v1/rides only).
# ---------------------------------------------------------------------------
# The lifecycle path measures distance server-side from the rider's own
# track. The one-shot path does not: the client hands us a finished number
# and we store it. src/badges.py sums that number, and miles_100 is 160 934 m
# — inside the 200 000 m per-ride ceiling — so before these checks a SINGLE
# request earned the top mileage badge, and the 120/day limit allowed 24 000
# km/day of fiction. Badges must keep counting off-feed rides (a rider's
# mileage is the miles they rode, whichever mechanism recorded them), so the
# fix cannot be to stop counting them; it has to be to stop believing
# impossible ones.
#
# Both bounds below reject rather than clamp. A silent clamp would tell an
# honest client
# with a unit bug (feet for metres, centimetres for metres) that its upload
# succeeded while quietly rewriting the ride, and the rider would never find
# out their history is wrong. A 422 naming the bound is fixable.

# Ride-AVERAGE speed ceiling: 20 m/s = 72 km/h = 45 mph.
#
# Chosen as "no micromobility trip averages this, and no honest client is
# anywhere near it", not as a legal limit. Shared e-scooters are governed at
# ~24 km/h (6.7 m/s); a class-3 e-bike tops out at 45 km/h (12.5 m/s); the
# UCI hour record — a professional, on a track, for exactly one hour — is
# 15.7 m/s. Averaging 20 m/s over a whole ride, stops and lights included,
# is a car on a highway. Deliberately ~3x the fastest thing this table is
# for, because it is an AVERAGE: a downhill sprint, a stretch of GPS drift,
# or a ride that is briefly carried in a vehicle must not cost an honest
# rider their log. The vector it closes is 200 km in 90 seconds, which
# misses by three orders of magnitude, not by a factor of two.
_MAX_AVG_SPEED_MPS = 20.0

# Consistency between the claimed distance and the route actually shown.
# `polyline` is required on this path, so there is always something to check
# against. An encoded polyline is a SAMPLED path and therefore a chord-wise
# UNDERCOUNT of the true route, so the tolerance has to be one-sided and
# loose: we only reject claiming far MORE than the route supports. Claiming
# LESS is never rejected — undercounting is not a farming vector, and a
# client reporting a vehicle odometer reading is being honest, not evasive.
#
# Reject when distance_m exceeds BOTH:
#   * the decoded route length x 3     (proportional: a coarsely sampled
#     track of a winding route can easily read 2-2.5x short), and
#   * the decoded route length + 1 km  (absolute: the multiplicative rule
#     alone collapses to zero on a degenerate polyline, which would reject
#     every short ride whose two points nearly coincide).
# The absolute floor is what a farmer is left with: a two-identical-points
# polyline now buys 1 km per request instead of 200.
_POLYLINE_DISTANCE_FACTOR = 3.0
_POLYLINE_DISTANCE_SLACK_M = 1000.0


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


def _track_points(cur, rid: UUID) -> list[tuple[float, float]]:
    """The ride's waypoints as (lat, lon), oldest first. Read whole rather
    than appended incrementally because waypoints can arrive out of order
    (client retry, offline buffer)."""
    cur.execute(
        "SELECT lat, lon FROM off_feed_ride_waypoints WHERE ride_id = %s "
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
    undercounts the ride by the length of a sampling gap, and the trailing
    gap is routinely the whole ride: one early fix 20 m from the start used
    to record 20 m for a 10 km trip, tagged high-confidence.

    A client whose first/last waypoint IS the start/end contributes a
    zero-length leg, which costs nothing.
    """
    points: list[tuple[float, float]] = []
    if start_lat is not None and start_lon is not None:
        points.append((start_lat, start_lon))
    points.extend(track)
    if end_lat is not None and end_lon is not None:
        points.append((end_lat, end_lon))
    return points


def _rebuild_track(cur, rid: UUID) -> None:
    """Recompute polyline + distance from the ride's full ordered waypoint
    set, led by the ride's start point (see _measured_path).

    This runs while the ride is still ACTIVE, so there is no reported end
    to close the path with yet — PATCH .../end recomputes over the same
    points plus its own end coordinates.
    """
    cur.execute("SELECT start_lat, start_lon FROM rides WHERE id = %s", (str(rid),))
    row = cur.fetchone()
    start_lat, start_lon = (row[0], row[1]) if row else (None, None)
    points = _measured_path(start_lat, start_lon, _track_points(cur, rid))
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
    """The caller's one active ride, or null.

    Filters on status = 'active', which is the same predicate as
    idx_rides_one_active_per_account — so the moment the sql/040 sweep
    expires an abandoned ride this goes back to null and
    POST /api/v1/rides/start succeeds again. The two can never disagree
    about whether the slot is occupied, because they read the same column.
    An expired ride is still in GET /api/v1/rides (?status=expired); it is
    just no longer the ride you are on.
    """
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
    after: str | None = Query(None, description="ISO timestamp — the NEXT page: waypoints recorded after this"),
    before: str | None = Query(None, description="ISO timestamp — the PREVIOUS page: the last `limit` waypoints recorded before this"),
) -> dict[str, Any]:
    """Waypoints oldest-first.

    Pagination pairs the cursor with the sort direction, which it did not
    used to: `before` with an ascending sort re-served the OLDEST rows on
    every call, so page 2 was the start of page 1 and nothing past the first
    page was reachable at all. Page forward with `after` (the last
    waypoint_at you received); `before` walks backwards by taking the last
    `limit` rows older than the cursor and returning them oldest-first, so
    the two are inverses of each other.

    Passing both narrows to an open interval, taking the newest rows in it.
    Cursors are timestamps, so waypoints sharing an exact waypoint_at are
    not split across pages by `after`; the tie-break on id only orders
    within a page.
    """
    rid = _parse_ride_id(ride_id)
    where = ["ride_id = %s", "account_id = %s"]
    params: list[Any] = [str(rid), user.account_id]
    parsed_after = _parse_before(after, "after")
    parsed_before = _parse_before(before, "before")
    if parsed_after is not None:
        params.append(parsed_after)
        where.append("waypoint_at > %s")
    if parsed_before is not None:
        params.append(parsed_before)
        where.append("waypoint_at < %s")
    # Walking backwards means "the newest rows older than the cursor", so the
    # LIMIT has to bite from the far end — hence the flip, undone below.
    backwards = parsed_before is not None
    order = "DESC" if backwards else "ASC"
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
                "SELECT status, started_at, start_lat, start_lon "
                "FROM rides WHERE id = %s AND account_id = %s FOR UPDATE",
                (str(rid), user.account_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "no such ride")
            status, started_at, start_lat, start_lon = row
            if status == "expired":
                # Distinguished from 'completed' because the fix is
                # different: there is nothing to reconcile, the rider just
                # needs to know the ride is closed and their slot is free.
                # Not endable — an end reported a day late would attach a
                # bogus duration to a ride nobody was on (sql/040).
                raise HTTPException(409, {
                    "error": "ride_expired",
                    "detail": "this ride was left active for over 24 hours and "
                              "has expired; it can no longer be ended. Its "
                              "waypoints are kept and still export, and you "
                              "can start a new ride.",
                })
            if status != "active":
                raise HTTPException(409, "this ride has already ended")
            if payload.ended_at < started_at:
                raise HTTPException(400, "ended_at < started_at")

            # Measure the WHOLE path, start -> fixes -> reported end. The
            # end report is the last thing we learn about the ride, so it is
            # the only chance to close the trailing sampling gap; the old
            # code kept whatever _rebuild_track had measured up to the last
            # fix and so never measured the final leg at all.
            #
            # distance_source stays honest about how the number was reached:
            # 'waypoints' when the rider actually handed us a track,
            # 'straight_line' when the only two points we have are the ends
            # — which undercounts any route that isn't straight (sql/035).
            track = _track_points(cur, rid)
            points = _measured_path(start_lat, start_lon, track,
                                    payload.end_lat, payload.end_lon)
            distance = round(path_length_meters(points))
            source = "waypoints" if track else "straight_line"
            # A ride with a track gets its polyline re-encoded over the same
            # points the distance was measured over, so the two can't
            # disagree. A ride without one keeps '' rather than gaining a
            # fabricated two-point "route" (GET /rides/export builds that
            # line from start/end at read time instead).
            polyline_sql = "polyline = %s" if track else "polyline = COALESCE(polyline, '')"
            polyline_params: tuple = (encode_polyline(points),) if track else ()

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
                    {polyline_sql},
                    distance_m = %s,
                    distance_source = %s
                WHERE id = %s
                RETURNING {_RIDE_COLS}
                """,
                (payload.ended_at, payload.end_lat, payload.end_lon, payload.ended_at,
                 payload.est_cost_cents, payload.rate_plan, payload.started_in_zone,
                 payload.ended_in_zone, *polyline_params, distance, source, str(rid)),
            )
            ride = _row_to_ride(cur.fetchone())
        conn.commit()
    return ride


# ---------------------------------------------------------------------------
# One-shot log + history
# ---------------------------------------------------------------------------

def _check_plausible(distance_m: int, duration_s: int, route: list[tuple[float, float]]) -> None:
    """Refuse a client-asserted ride that could not have happened.

    Raises 422 with a machine-readable `error` and a detail that names the
    bound and the numbers that broke it, so a client with a unit bug can see
    what it did instead of guessing. See the bounds' rationale above.

    A zero-distance ride is always plausible (a cancelled unlock, a ride
    that went nowhere) and is checked first, because it is also the only
    case where a zero duration is legitimate.
    """
    if distance_m <= 0:
        return

    if duration_s <= 0:
        raise HTTPException(422, {
            "error": "implausible_speed",
            "detail": f"{distance_m} m in {duration_s} s is infinite speed; "
                      "a ride that covered distance took time",
        })

    speed = distance_m / duration_s
    if speed > _MAX_AVG_SPEED_MPS:
        raise HTTPException(422, {
            "error": "implausible_speed",
            "detail": f"{distance_m} m in {duration_s} s averages "
                      f"{speed:.1f} m/s, above the {_MAX_AVG_SPEED_MPS:.0f} m/s "
                      f"({_MAX_AVG_SPEED_MPS * 3.6:.0f} km/h) ceiling for a "
                      "scooter or bike ride. Check the units on distance_m "
                      "(metres) and duration_s (seconds).",
        })

    route_m = path_length_meters(route)
    ceiling = max(route_m * _POLYLINE_DISTANCE_FACTOR,
                  route_m + _POLYLINE_DISTANCE_SLACK_M)
    if distance_m > ceiling:
        raise HTTPException(422, {
            "error": "distance_exceeds_polyline",
            "detail": f"distance_m {distance_m} is more than the route in "
                      f"`polyline` supports — it decodes to {route_m:.0f} m. "
                      "Send the track you actually rode; a distance is only "
                      "believable next to the route it was measured over.",
        })


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
        route = decode_polyline(payload.polyline)
    except PolylineError as e:
        raise HTTPException(400, f"polyline won't decode: {e}")
    # Sits with the rest of the input validation, which already runs ahead of
    # enforce() on this endpoint: these are pure checks on an already-parsed
    # body that store nothing and call nothing, so unlike the upload paths
    # (where the limiter deliberately precedes the expensive work) there is
    # no work here for a quota to be protecting.
    _check_plausible(payload.distance_m, payload.duration_s, route)

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
    # 'expired' included since sql/040: the list returns those rows whether
    # or not you filter, so refusing them as a filter value would 422 a
    # client for asking about rides this endpoint hands it anyway.
    status: str | None = Query(None, pattern="^(active|completed|expired)$"),
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


def _ride_geometry(ride: dict[str, Any]) -> dict[str, Any] | None:
    """GeoJSON geometry for one exported ride, or None.

    A LineString needs at least two positions — RFC 7946 §3.1.4 — and
    QGIS/GDAL/geojson.io all reject `{"type":"LineString","coordinates":[]}`
    outright, taking the whole FeatureCollection down with it. A ride that
    uploaded no waypoints stores polyline '' but DOES know where it started
    and where it ended, so build the line from those columns instead of
    emitting an invalid one. When even that isn't available (an active ride
    with no end yet), emit `null` geometry, which is valid GeoJSON for a
    Feature and keeps the row's properties exportable.
    """
    try:
        coords = [[lon, lat] for lat, lon in decode_polyline(ride["polyline"] or "")]
    except PolylineError:
        coords = []  # validated at ingest; belt-and-suspenders
    if len(coords) < 2:
        ends = [
            [ride["start_lon"], ride["start_lat"]]
            if ride["start_lat"] is not None and ride["start_lon"] is not None else None,
            [ride["end_lon"], ride["end_lat"]]
            if ride["end_lat"] is not None and ride["end_lon"] is not None else None,
        ]
        coords = [c for c in ends if c is not None]
    if len(coords) < 2:
        return None
    return {"type": "LineString", "coordinates": coords}


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
        props = {k: v for k, v in ride.items() if k != "polyline"}
        features.append({
            "type": "Feature",
            "id": ride["id"],
            "geometry": _ride_geometry(ride),
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
