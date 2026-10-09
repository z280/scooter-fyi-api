"""Rider condition checks — the HTTP side (docs/FLEET_REPORTS_PLAN.md §4.4).

    GET  /api/v1/devices/{vid}/conditions        what a rider would be asked
    POST /api/v1/devices/{vid}/condition-checks  answer it

Both signed-in (a resolution is attributed, and points are never
anonymous), both rate-limited per account. The rules live in
src/condition_checks.py; this module is validation, metering and shapes.

WHY A SEPARATE ENDPOINT, NOT A RIDE-ALONG ON THE FEATURE POST. The feature
confirmation (`POST /api/v1/reports/device-features`) accepts anonymous
reports, dedupes on its own answer shape and is graded later by a
ten-minute processor; a condition check must be signed in, must act
immediately (a resolution un-hides a vehicle on the next request) and needs
the list from the GET before it can be answered at all. Folding it into the
feature POST would make that endpoint conditionally require a session and
give one request two dedupe rules. The two are LINKED instead: the feature
POST's `id` is accepted here as `feature_report_id`, which is the proof of
presence the condition check needs, so the usual flow — confirm features,
then "confirm condition as well?" — costs the rider no second plate entry.
The path shape follows the feature read (`GET /api/v1/devices/{vid}/features`).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from . import condition_checks
from .accounts import SessionUser, require_session
from .api_public import latest_complete_cycle
from .pg import connection
from .ratelimit import enforce

log = logging.getLogger(__name__)

router = APIRouter()

_VID = r"^[0-9a-f]{16}$"

#: GET: generous — the map may open the list for several scooters while a
#: rider decides. POST: a rider working a block can check a few an hour; the
#: points caps (points.py) are what bound the reward, this bounds the load.
LIMIT_CONDITIONS_GET_PER_ACCOUNT = (60, 3600)
LIMIT_CONDITION_CHECKS_PER_ACCOUNT = (20, 3600)


class ConditionAnswer(BaseModel):
    report_id: int = Field(..., ge=1)
    still_a_problem: bool


class ConditionCheckIn(BaseModel):
    answers: list[ConditionAnswer] = Field(
        default_factory=list, max_length=condition_checks.MAX_ANSWERS)
    test_ride: bool
    #: Proof of presence — at least one. feature_report_id is the `id` the
    #: feature-confirmation POST returned (same account, same vehicle,
    #: plate-valid, under an hour old).
    feature_report_id: int | None = Field(default=None, ge=1)
    submitted_plate: str | None = Field(default=None, min_length=1, max_length=64)
    qr_raw_value: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _check(self) -> "ConditionCheckIn":
        if (self.feature_report_id is None and self.submitted_plate is None
                and self.qr_raw_value is None):
            raise ValueError("send feature_report_id, submitted_plate or "
                             "qr_raw_value — proof you are at the scooter")
        ids = [a.report_id for a in self.answers]
        if len(ids) != len(set(ids)):
            raise ValueError("each report_id may be answered once")
        return self


def _error(e: condition_checks.CheckError) -> JSONResponse:
    return JSONResponse(status_code=e.status,
                        content={"detail": {"code": e.code, "message": e.message,
                                            **e.extra}})


@router.get("/api/v1/devices/{vehicle_identifier}/conditions")
def get_conditions(
    vehicle_identifier: str = Path(..., pattern=_VID),
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    """The vehicle's standing negative-rideability reports to confirm, with
    their observed dates, plus the not_found reports a test ride resolves
    without asking, and what a test-ridden check would pay this rider."""
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="condition_conditions_account",
                    key=str(user.account_id),
                    limit=LIMIT_CONDITIONS_GET_PER_ACCOUNT[0],
                    window_seconds=LIMIT_CONDITIONS_GET_PER_ACCOUNT[1])
            conn.commit()
            cur.execute("SELECT 1 FROM device_state WHERE vehicle_identifier = %s",
                        (vehicle_identifier,))
            if cur.fetchone() is None:
                raise HTTPException(404, "unknown vehicle_identifier")
            cycle_id, snap = latest_complete_cycle(cur)
            asked, found = condition_checks.standing_conditions(
                cur, cycle_id, vehicle_identifier, user.account_id)
            preview = condition_checks.points_preview(
                cur, account_id=user.account_id, vehicle_identifier=vehicle_identifier,
                asked=asked, found=found)
        conn.rollback()
    return {
        "vehicle_identifier": vehicle_identifier,
        "as_of": snap.isoformat(),
        "needs_condition_check": bool(asked),
        "conditions": asked,
        "auto_resolves": found,
        "points": preview,
        "feed_window_minutes": condition_checks.FEED_WINDOW_MINUTES,
    }


@router.post("/api/v1/devices/{vehicle_identifier}/condition-checks")
def post_condition_check(
    vehicle_identifier: str = Path(..., pattern=_VID),
    payload: ConditionCheckIn = Body(...),
    user: SessionUser = Depends(require_session),
) -> Any:
    """Submit one condition check. test_ride=false discards every answer
    (200, `discarded: true`, nothing changes); test_ride=true resolves each
    "no longer a problem", reconfirms each "still a problem", resolves any
    standing not_found, and pays 10 now / +40 when the feed confirms."""
    with connection() as conn:
        with conn.cursor() as cur:
            # Metered BEFORE the work and committed on its own, so a refused
            # check still spends quota — probing for a passing plate costs.
            enforce(cur, bucket="condition_checks_account", key=str(user.account_id),
                    limit=LIMIT_CONDITION_CHECKS_PER_ACCOUNT[0],
                    window_seconds=LIMIT_CONDITION_CHECKS_PER_ACCOUNT[1])
            conn.commit()
            cycle_id, _snap = latest_complete_cycle(cur)
            try:
                out = condition_checks.submit_check(
                    cur, cycle_id=cycle_id, account_id=user.account_id,
                    vehicle_identifier=vehicle_identifier,
                    answers=[(a.report_id, a.still_a_problem) for a in payload.answers],
                    test_ride=payload.test_ride,
                    feature_report_id=payload.feature_report_id,
                    submitted_plate=payload.submitted_plate,
                    qr_raw_value=payload.qr_raw_value)
            except condition_checks.CheckError as e:
                conn.rollback()
                return _error(e)
        conn.commit()
    log.info("condition check id=%d vehicle=%s account=%d test_ride=%s resolved=%d "
             "reconfirmed=%d points=%d withheld=%s", out["check_id"], vehicle_identifier,
             user.account_id, out["test_ride"], len(out["resolved"]) + len(out["found"]),
             len(out["reconfirmed"]), out["points_awarded"], out["points_withheld_reason"])
    return out
