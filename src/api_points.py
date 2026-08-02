"""Points endpoints (requirement #10).

GET /api/v1/points          — the caller's ledger + running total.
GET /api/v1/points/schedule — public; the authoritative action -> award map.

The schedule endpoint exists so rider-facing copy CANNOT DRIFT from the
ledger. The frontend used to hardcode "+5 points" strings next to awards
whose values live in src/points.py, which is how a UI ends up promising a
number the server does not pay. Every value it publishes is read from
src/points.py at request time — there is not one point literal in this
module, and adding one defeats the endpoint. When a constant changes, the
copy changes with the next deploy and nobody has to remember a second
place.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from . import points as points_module
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


# --- Schedule ---------------------------------------------------------------
# Two entry shapes, and only two, so a client can render any action it has
# never heard of:
#   flat     {"points": n}                            — one award, one value
#   formula  {"base": b, "per_step": p, "step_km": k} — b + p * ceil(km / k),
#            rounding UP: the step is a STARTED step, not a completed one.
# A flat award is a formula with no distance term, not a different concept;
# the split exists because writing {"base": 4, "per_step": 0, "step_km": 0}
# for a survey would invite a division by zero in every client.


def points_schedule() -> dict[str, dict[str, int]]:
    """The published action -> award map, built from src/points.py.

    Read through the `points_module` attribute (not `from .points import X`)
    on every call deliberately: a from-import would bind the values at import
    time, so the endpoint would keep serving stale numbers after a constant
    changed under it, and the drift this endpoint exists to prevent would be
    reintroduced by the endpoint itself. It also means the drift test can
    move a constant and watch the payload follow.
    """
    p = points_module
    schedule: dict[str, dict[str, int]] = {
        "qr_scan": {"points": p.POINTS_QR_SCAN},
        "gbfs_trip_validated": {"points": p.POINTS_GBFS_TRIP_VALIDATED},
        # Per waypoint uploaded, credited once at ride end as
        # POINTS_PER_WAYPOINT * count — hence a flat per-unit value and no
        # distance step. Superseded by the ride-mode awards in A2; it stays
        # published because the ledger keeps paying it until then.
        "waypoint": {"points": p.POINTS_PER_WAYPOINT},
        "profile_completion": {"points": p.POINTS_PROFILE_COMPLETION},
    }

    # Report awards come from REPORT_TYPE_POINTS itself rather than being
    # re-listed here, so a new report type shows up in the schedule by
    # existing. Its values are (action, points) pairs keyed by report_type;
    # the schedule is keyed by ACTION, which is what the ledger records.
    for action, value in p.REPORT_TYPE_POINTS.values():
        schedule[action] = {"points": value}

    # Ride Mode (PLAN_RIDE_MODE_API.md A1 ships the complete schedule; A2/A3
    # wire the awards). Published BEFORE anything awards them on purpose —
    # Screen 2's ℹ copy and Screen 9's header interpolate these values, and
    # the frontend needs them the day F2 deploys.
    schedule["battery_contribution"] = {
        "base": p.POINTS_BATTERY_CONTRIBUTION_BASE,
        "per_step": p.POINTS_BATTERY_CONTRIBUTION_PER_STEP,
        "step_km": p.BATTERY_CONTRIBUTION_STEP_KM,
    }
    schedule["nav_route_feedback"] = {"points": p.POINTS_NAV_ROUTE_FEEDBACK}
    schedule["nav_qualitative_feedback"] = {"points": p.POINTS_NAV_QUALITATIVE}
    schedule["nav_distance_bonus"] = {
        # `base: 0` is structural, not a tunable value: this award is purely
        # per-step (2 * ceil(km / 3)). It is stated rather than omitted so a
        # client computing base + per_step * steps over any formula entry
        # gets 0, not undefined/NaN.
        "base": 0,
        "per_step": p.POINTS_NAV_DISTANCE_PER_STEP,
        "step_km": p.NAV_DISTANCE_STEP_KM,
    }
    schedule["ride_survey"] = {"points": p.POINTS_RIDE_SURVEY}

    # Device feature confirmations (sql/055). Read out of
    # FEATURE_STATUS_POINTS rather than re-listed, for the same reason the
    # report awards are: the mapping that decides the award is the mapping
    # that gets published, so the "☑️ Confirm Features" modal's "+124 pts"
    # copy cannot promise a tier the endpoint does not pay. Keyed by ACTION
    # (what the ledger records), not by the feature_status that selected it.
    for action, value in p.FEATURE_STATUS_POINTS.values():
        schedule[action] = {"points": value}

    return schedule


@router.get("/api/v1/points/schedule")
def points_schedule_endpoint(response: Response) -> dict[str, dict[str, int]]:
    """Public — no bearer. The response body IS the map (no envelope): the
    client indexes it by action name, and an action it does not recognise is
    a value it can still render.

    Cached like the other published-policy payload (`/api/v1/meta/privacy`):
    this is a handful of constants, and an hour of staleness on copy is the
    price of not making every wizard launch hit the origin.
    """
    response.headers["Cache-Control"] = "public, max-age=3600"
    return points_schedule()
