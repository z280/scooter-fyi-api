"""Route feedback that isn't tied to a tracked ride (sql/068).

    POST /api/v1/route-feedback    navigation feedback from a private ride

Screen 9's navigation pane posts to /tracked-rides/{id}/survey — owner-only,
keyed to a tracked ride. A "My own Device" or guest ride is private by
definition (no tracked_rides row, no ride id), so those riders had no way to
say what they thought of the route they chose and rode. This endpoint takes
the SAME navigation answers with the route described inline (profile plus
the client's own distance/duration figures), because for a private ride no
ride_routes row was ever written.

WHAT THIS DOES NOT DO: no points, ever — private rides are never
points-eligible, and an award here would just invite drive-by farming of an
endpoint that requires no proof a ride happened. No ride_routes linkage, no
single-shot rule (there is no ride to be single per); the rate limits are
the flood control. Anonymous is allowed, same stance as device reports: the
opinion still carries data, it is just weighted by the fact that nothing
anchors it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, Field, model_validator

from .accounts import SessionUser, optional_session
from .client_ip import real_client_ip
from .pg import connection
from .ratelimit import enforce

log = logging.getLogger(__name__)

router = APIRouter()

# Anonymous is metered hard; authenticated riders get room for a real day of
# riding their own machine around town, and no more — there is nothing to
# earn here, so nobody honest needs a bigger bucket.
_LIMIT_ROUTE_FEEDBACK_ANON_PER_IP = (5, 3600)
_LIMIT_ROUTE_FEEDBACK_AUTH_PER_ACCOUNT = (20, 3600)


class RouteFeedbackIn(BaseModel):
    """The survey's navigation vocabulary, verbatim (same names, same
    ranges as sql/052's ride_surveys columns), plus the inline route
    description that stands in for the ride_routes row a private ride
    never wrote."""
    #: A config.json valhalla.profiles key. Not validated against the live
    #: profile set on purpose: config can change between the client caching
    #: a route and the rider submitting, and feedback about a renamed
    #: profile is still evidence.
    route_profile: str = Field(..., min_length=1, max_length=64)
    distance_m: float | None = Field(default=None, ge=0)
    duration_s: float | None = Field(default=None, ge=0)
    nav_route_rating: int | None = Field(default=None, ge=1, le=10)
    nav_deviated: bool | None = None
    nav_deviated_needs_improvement: bool | None = None
    nav_nps: int | None = Field(default=None, ge=0, le=10)
    nav_qualitative: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _check_substance(self) -> "RouteFeedbackIn":
        # A row carrying only a profile name says nothing — require at
        # least one actual answer, so the table can't fill with empty
        # submissions from a client bug (or a bored script).
        qualitative = (self.nav_qualitative or "").strip()
        self.nav_qualitative = qualitative or None
        if (
            self.nav_route_rating is None
            and self.nav_deviated is None
            and self.nav_nps is None
            and self.nav_qualitative is None
        ):
            raise ValueError(
                "answer at least one question — rating, deviation, NPS, or "
                "qualitative text"
            )
        # Mirrors the survey's dependent-question shape: the follow-up is
        # only asked after a Yes, so a No/unanswered deviation cannot carry
        # an improvement verdict.
        if self.nav_deviated is not True:
            self.nav_deviated_needs_improvement = None
        return self


@router.post("/api/v1/route-feedback")
def submit_route_feedback(
    request: Request,
    payload: RouteFeedbackIn = Body(...),
    user: SessionUser | None = Depends(optional_session),
) -> dict[str, Any]:
    """Store one navigation opinion. Returns the row's id and timestamp —
    there are no awards to report and no status to echo."""
    ip = real_client_ip(request)
    ua = request.headers.get("user-agent")

    with connection() as conn:
        with conn.cursor() as cur:
            if user is None:
                enforce(cur, bucket="route_feedback_ip", key=ip or "?",
                        limit=_LIMIT_ROUTE_FEEDBACK_ANON_PER_IP[0],
                        window_seconds=_LIMIT_ROUTE_FEEDBACK_ANON_PER_IP[1])
            else:
                enforce(cur, bucket="route_feedback_account",
                        key=str(user.account_id),
                        limit=_LIMIT_ROUTE_FEEDBACK_AUTH_PER_ACCOUNT[0],
                        window_seconds=_LIMIT_ROUTE_FEEDBACK_AUTH_PER_ACCOUNT[1])
            cur.execute(
                """
                INSERT INTO route_feedback (
                    account_id, reporter_ip, reporter_user_agent,
                    route_profile, distance_m, duration_s,
                    nav_route_rating, nav_deviated,
                    nav_deviated_needs_improvement, nav_nps, nav_qualitative
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (
                    user.account_id if user else None, ip, ua,
                    payload.route_profile, payload.distance_m,
                    payload.duration_s, payload.nav_route_rating,
                    payload.nav_deviated,
                    payload.nav_deviated_needs_improvement,
                    payload.nav_nps, payload.nav_qualitative,
                ),
            )
            new_id, created_at = cur.fetchone()
        conn.commit()

    log.info(
        "route feedback id=%d profile=%s rating=%s auth=%s",
        new_id, payload.route_profile, payload.nav_route_rating,
        user is not None,
    )
    return {"id": int(new_id), "created_at": created_at.isoformat()}
