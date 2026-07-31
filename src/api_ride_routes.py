"""Ride route persistence: POST /api/v1/ride-routes (PLAN_RIDE_MODE_API.md
phase A3, sql/052_ride_surveys_routes.sql).

Screen 4 of the ride wizard picks a route (one of the four
`load().valhalla` profiles) before the ride starts; this endpoint is where
that choice is stored, so Screen 9's survey can rate the leg it names and
A2's `nav_distance_bonus` (src/points.py) can confirm a route exists for
the ride. Named after the `ride_routes` table it owns, not
`api_rides_routes.py`, which would read as a sibling of `api_rides.py` —
the off-feed ride tracker this program does not touch.

CONSENT, not enforced here: the master plan is explicit that the client
calls this endpoint ONLY when `ride_options.nav_improvement` is on — that
consent is what makes storing a route acceptable in the first place. That
rule lives entirely on the client (the same way the off-route-reroute
"never POST automatically" rule does); this handler has no
`ride_options` to check against, since `tracked_ride_id` is null in the
normal wizard flow (Screen 4 precedes ride start).

MULTI-ROW-PER-RIDE IS INTENDED, not an oversight: the S8 New-Destination
loop re-runs Screen 4 mid-ride with the ride id already known, and each
deliberate reselection is its own row (an automatic off-route re-route
never POSTs — a frontend rule this endpoint does not need to enforce).
`nav_distance_bonus` is awarded at most once per ride regardless of row
count (the `(source_table, source_id, action)` dedupe in
`src/points.py:credit_points` — it requires *a* route row, not a specific
one). No uniqueness constraint on `tracked_ride_id` — see sql/052's
comment and tests/test_ride_routes.py's multi-row coverage.

OWNERSHIP: a non-null `tracked_ride_id` must resolve to a ride owned by
the caller, else 404 (not 403) — the FK alone would accept any account's
ride id, and 404 is the no-existence-oracle idiom every tracked-rides
sub-resource uses (see src/api_ride_screenshots.py).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from .accounts import SessionUser, require_session
from .config import load
from .pg import connection
from .polyline import PolylineError, decode as decode_polyline
from .ratelimit import enforce
from .ride_limits import MAX_RIDE_DISTANCE_METERS, measure_path

router = APIRouter()

# Account-scoped, same naming style as tracked_ride_start_account /
# ride_screenshot_account (src/api_tracked_rides.py,
# src/api_ride_screenshots.py).
_LIMIT_RIDE_ROUTE_PER_ACCOUNT = (30, 3600)

# The 3h ride-mode watch window (api_tracked_rides.WATCH_DURATION_HOURS),
# expressed in seconds — not imported from there to avoid a cross-router
# dependency for one constant; the two must be kept in sync by hand if
# WATCH_DURATION_HOURS ever moves.
MAX_ROUTE_DURATION_SECONDS = 3 * 3600  # 10_800

# REVIEW FIX: the payload had no encoded-length or decoded-point cap at
# all before this — the 30/hour rate limit alone doesn't bound a single
# request's size. Both figures are deliberately generous for a real route
# (a Google encoded polyline needs roughly 5-10 bytes/point, so even a
# maximally dense 80 km route — MAX_RIDE_DISTANCE_METERS, one point every
# few meters — fits comfortably within both caps) while still being a
# firm ceiling against an adversarial or buggy client.
MAX_ROUTE_POLYLINE_CHARS = 200_000
MAX_ROUTE_POLYLINE_POINTS = 20_000


class RideRouteIn(BaseModel):
    tracked_ride_id: str | None = Field(
        default=None,
        description="Null in the normal wizard flow (Screen 4 precedes ride "
                    "start); set on the S8 New-Destination loop, and must "
                    "resolve to a ride owned by the caller.",
    )
    profile: str = Field(..., description="A load().valhalla profile key (safe|range|shade|express today)")
    origin: tuple[float, float] = Field(..., description="[lat, lon]")
    destination: tuple[float, float] = Field(..., description="[lat, lon]")
    route_polyline: str = Field(..., min_length=1, max_length=MAX_ROUTE_POLYLINE_CHARS)
    distance_meters: float = Field(..., ge=0, le=MAX_RIDE_DISTANCE_METERS)
    duration_seconds: float = Field(..., ge=0, le=MAX_ROUTE_DURATION_SECONDS)
    battery_percent_estimate: float | None = Field(default=None, ge=0, le=100)


def _parse_ride_id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError:
        raise HTTPException(400, "tracked_ride_id must be a UUID")


@router.post("/api/v1/ride-routes")
def create_ride_route(
    user: SessionUser = Depends(require_session),
    payload: RideRouteIn = Body(...),
) -> dict[str, Any]:
    cfg = load().valhalla

    # Pure validation first, before a connection is taken — same rule
    # src/api_tracked_rides.py:_serialize_ride_options follows: a malformed
    # or out-of-graph request is a client bug, not a reason to hold a
    # pooled connection open.
    prof = cfg.profile(payload.profile)
    if prof is None:
        raise HTTPException(400, {
            "error": "unknown_profile",
            "detail": f"unknown profile {payload.profile!r}",
            "profiles": [p.key for p in cfg.profiles],
        })

    # src/polyline.py decode() at its default precision 5 — /route returns
    # GeoJSON, so the client is the one that encodes this string.
    try:
        points = decode_polyline(payload.route_polyline)
    except PolylineError:
        points = []
    if len(points) < 2:
        raise HTTPException(400, {
            "error": "bad_polyline",
            "detail": "route_polyline must decode to at least 2 points",
        })
    # REVIEW FIX: a compact-but-dense polyline can expand to far more points
    # than `max_length` on the encoded string alone would suggest.
    if len(points) > MAX_ROUTE_POLYLINE_POINTS:
        raise HTTPException(413, {
            "error": "bad_polyline",
            "detail": f"route_polyline decodes to more than "
                      f"{MAX_ROUTE_POLYLINE_POINTS} points",
        })
    # REVIEW FIX: bound the DECODED geometry's real length against the same
    # ride-distance invariant the rest of the codebase enforces, independent
    # of whatever `distance_meters` the client claims — a short claimed
    # distance paired with a geometrically enormous polyline must not sneak
    # an oversized route past the `distance_meters` field's own bound.
    measured_m, _ = measure_path(points, cap_legs=False)
    if measured_m > MAX_RIDE_DISTANCE_METERS:
        raise HTTPException(422, {
            "error": "bad_polyline",
            "detail": "decoded route geometry exceeds the maximum ride distance",
        })

    origin_lat, origin_lon = payload.origin
    dest_lat, dest_lon = payload.destination
    # Mirrors api_route.py's own out_of_coverage rejection: reject up front
    # with the served bbox rather than silently accepting a relocated point.
    for label, (lat, lon) in (("origin", (origin_lat, origin_lon)),
                              ("destination", (dest_lat, dest_lon))):
        if not cfg.contains(lat, lon):
            raise HTTPException(400, {
                "error": "out_of_coverage",
                "detail": f"{label} ({lat}, {lon}) is outside the routing graph",
                "graph_bbox": cfg.bbox,
            })

    rid: UUID | None = None
    if payload.tracked_ride_id is not None:
        rid = _parse_ride_id(payload.tracked_ride_id)

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="ride_route_account", key=str(user.account_id),
                    limit=_LIMIT_RIDE_ROUTE_PER_ACCOUNT[0],
                    window_seconds=_LIMIT_RIDE_ROUTE_PER_ACCOUNT[1])

            if rid is not None:
                # 404, not 403: the FK alone would accept any account's
                # ride id, and this is the no-existence-oracle idiom every
                # tracked-rides sub-resource uses.
                cur.execute(
                    "SELECT 1 FROM tracked_rides WHERE id = %s AND account_id = %s",
                    (str(rid), user.account_id),
                )
                if cur.fetchone() is None:
                    raise HTTPException(404, "no such ride")

            cur.execute(
                """
                INSERT INTO ride_routes (
                    tracked_ride_id, account_id, profile,
                    origin_lat, origin_lon, dest_lat, dest_lon,
                    route_polyline, distance_meters, duration_seconds,
                    battery_percent_estimate
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (str(rid) if rid is not None else None, user.account_id, prof.key,
                 origin_lat, origin_lon, dest_lat, dest_lon,
                 payload.route_polyline, payload.distance_meters,
                 payload.duration_seconds, payload.battery_percent_estimate),
            )
            (ride_route_id,) = cur.fetchone()
        conn.commit()

    return {"ride_route_id": str(ride_route_id)}
