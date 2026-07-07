"""Negative-report + quality-feedback write/read endpoints.

PUBLIC:
    POST /api/v1/reports                  submit a negative report (no auth yet)
    POST /api/v1/quality-feedback         positive/negative feedback on the
                                          quality_designation our system
                                          showed for a scooter at a cell
    GET  /api/v1/devices/current          gains a `has_negative_report`
                                          flag when a report at the
                                          device's current h3_10 cell
                                          is ≤24h old (wired in
                                          api_public.py).

PRIVATE (admin session scope required):
    GET  /api/v1/private/reports             list all reports
    GET  /api/v1/private/quality-feedback    list all feedback

Anti-abuse is intentionally minimal in this iteration. A follow-up
will add:
  * per-IP rate limiting
  * consensus surfacing (require ≥2 distinct reporter IPs in a window)
  * a Turnstile / similar challenge on the public form
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import h3
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from .client_ip import real_client_ip
from .identity import hash_plate
from .accounts import SessionUser, require_admin
from .pg import connection

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request body schema
# ---------------------------------------------------------------------------
class NegativeReportIn(BaseModel):
    """Payload for POST /api/v1/reports.

    At least one of vehicle_identifier or vehicle_plate must be supplied.
    Caller MAY supply h3_*_index for cross-check, but server always
    computes and stores its own values from report_lat/report_lon.
    """
    vehicle_identifier: str | None = Field(default=None, min_length=16, max_length=16)
    vehicle_plate: str | None = Field(default=None, min_length=1, max_length=64)
    report_lat: float = Field(..., ge=-90, le=90)
    report_lon: float = Field(..., ge=-180, le=180)
    problem_tags: list[str] = Field(default_factory=list, max_length=20)
    problem_description: str | None = Field(default=None, max_length=4000)
    # Optional client-supplied h3 values — accepted but server values win.
    h3_8_index: int | None = None
    h3_9_index: int | None = None
    h3_10_index: int | None = None

    @model_validator(mode="after")
    def _at_least_one_id(self):
        if not self.vehicle_identifier and not self.vehicle_plate:
            raise ValueError("must supply vehicle_identifier or vehicle_plate")
        return self


# ---------------------------------------------------------------------------
# POST /api/v1/reports — public write
# ---------------------------------------------------------------------------
@router.post("/api/v1/reports")
def submit_report(
    request: Request,
    payload: NegativeReportIn = Body(...),
) -> dict[str, Any]:
    """Accept a citizen-submitted negative report. Public, no auth.

    Returns the persisted record's `id` and `reported_at`. The caller
    cannot read other reports through this endpoint — that's behind the
    admin session scope at /api/v1/private/reports.
    """
    # Resolve identity. Plate is ground truth; if both are given and
    # they disagree, prefer plate and log the mismatch.
    plate = payload.vehicle_plate
    ident = payload.vehicle_identifier
    if plate:
        computed = hash_plate(plate)
        if ident and computed != ident:
            log.warning(
                "report: plate/identifier mismatch (plate=%s, given_id=%s, computed=%s)",
                plate, ident, computed,
            )
        ident = computed

    # Look up the device in device_state. We want two things from this row:
    #   1. plate backfill (when the caller only gave us an identifier)
    #   2. the SCOOTER'S current h3 cells — these are what we'll store as
    #      the canonical h3_*_index on the report row. Reason: the
    #      has_negative_report flag on /devices/current matches the
    #      device's CURRENT h3 against the report's h3, so storing the
    #      reporter's clicked-location h3 would make the flag silently
    #      fail any time the reporter stood ≥75 m from the scooter
    #      (one res-10 cell). Anchoring to the scooter's actual position
    #      makes "report sticks until scooter moves" work as advertised.
    device_h3_8 = device_h3_9 = device_h3_10 = None
    if ident:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT vehicle_plate,
                           current_h3_8_index, current_h3_9_index, current_h3_10_index
                    FROM device_state WHERE vehicle_identifier = %s
                    """,
                    (ident,),
                )
                row = cur.fetchone()
                if row:
                    if not plate:
                        plate = row[0]
                    device_h3_8, device_h3_9, device_h3_10 = row[1], row[2], row[3]

    # Anchor h3 to the scooter's position when we know it; otherwise fall
    # back to the reporter's clicked position. (For a brand-new
    # identifier we've never observed, the reporter's location is the
    # only signal we have.) Server-side computation either way — any
    # caller-supplied h3_*_index values on the payload are discarded.
    if device_h3_10 is not None:
        h3_8, h3_9, h3_10 = int(device_h3_8), int(device_h3_9), int(device_h3_10)
    else:
        h3_8 = int(h3.latlng_to_cell(payload.report_lat, payload.report_lon, 8), 16)
        h3_9 = int(h3.latlng_to_cell(payload.report_lat, payload.report_lon, 9), 16)
        h3_10 = int(h3.latlng_to_cell(payload.report_lat, payload.report_lon, 10), 16)

    ip = real_client_ip(request)
    ua = request.headers.get("user-agent")

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO negative_reports (
                    vehicle_identifier, vehicle_plate,
                    report_lat, report_lon,
                    h3_8_index, h3_9_index, h3_10_index,
                    problem_tags, problem_description,
                    reporter_ip, reporter_user_agent
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, reported_at
                """,
                (
                    ident, plate, payload.report_lat, payload.report_lon,
                    h3_8, h3_9, h3_10,
                    payload.problem_tags, payload.problem_description,
                    ip, ua,
                ),
            )
            new_id, reported_at = cur.fetchone()
        conn.commit()

    log.info(
        "report received id=%d vehicle_identifier=%s tags=%s anchored=%s",
        new_id, ident, payload.problem_tags,
        "device" if device_h3_10 is not None else "reporter",
    )
    return {
        "id": int(new_id),
        "reported_at": reported_at.isoformat(),
        "vehicle_identifier": ident,
        "h3_10_index": h3_10,
    }


# ---------------------------------------------------------------------------
# GET /api/v1/private/reports — auth-gated read
# ---------------------------------------------------------------------------
_DEFAULT_RANGE_HOURS = 24
_MAX_RANGE_DAYS = 90


@router.get("/api/v1/private/reports")
def list_reports(
    user: SessionUser = Depends(require_admin),
    since: str | None = Query(None, description="ISO 8601 UTC; default = now - 24h"),
    until: str | None = Query(None, description="ISO 8601 UTC; default = now"),
    vehicle_identifier: str | None = Query(None, min_length=16, max_length=16),
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    """Full negative-report listing. Bearer-gated."""
    now = datetime.now(timezone.utc)
    try:
        end = datetime.fromisoformat(until.replace("Z", "+00:00")) if until else now
        start = (
            datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since else end - timedelta(hours=_DEFAULT_RANGE_HOURS)
        )
    except ValueError as e:
        raise HTTPException(400, f"bad time format: {e}")
    if end < start:
        raise HTTPException(400, "until < since")
    if (end - start).days > _MAX_RANGE_DAYS:
        raise HTTPException(400, f"window > {_MAX_RANGE_DAYS} days")

    where = ["reported_at >= %s", "reported_at <= %s"]
    params: list[Any] = [start, end]
    if vehicle_identifier:
        where.append("vehicle_identifier = %s")
        params.append(vehicle_identifier)
    params.append(limit)

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, reported_at, vehicle_identifier, vehicle_plate,
                       report_lat, report_lon,
                       h3_8_index, h3_9_index, h3_10_index,
                       problem_tags, problem_description,
                       reporter_ip, reporter_user_agent
                FROM negative_reports
                WHERE {' AND '.join(where)}
                ORDER BY reported_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()

    return {
        "since": start.isoformat(),
        "until": end.isoformat(),
        "count": len(rows),
        "viewed_by": user.email,
        "reports": [
            {
                "id": int(r[0]),
                "reported_at": r[1].isoformat(),
                "vehicle_identifier": r[2],
                "vehicle_plate": r[3],
                "report_lat": float(r[4]),
                "report_lon": float(r[5]),
                "h3_8_index": int(r[6]),
                "h3_9_index": int(r[7]),
                "h3_10_index": int(r[8]),
                "problem_tags": list(r[9] or []),
                "problem_description": r[10],
                "reporter_ip": str(r[11]) if r[11] else None,
                "reporter_user_agent": r[12],
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Quality feedback — public POST + private read
# ---------------------------------------------------------------------------
class QualityFeedbackIn(BaseModel):
    vehicle_identifier: str = Field(..., min_length=16, max_length=16)
    h3_10_index: int = Field(..., description="h3 v4 cell ID at resolution 10")
    polarity: str = Field(..., pattern="^(positive|negative)$")
    designation_observed: str | None = Field(
        default=None,
        description='The quality_designation value the user was reacting to '
                    '(one of "poor", "acceptable", "good", "great", "N/A"). '
                    "Optional — useful for separating 'you said great but it was bad' "
                    "from 'you said poor but it was actually great'.",
    )
    comment: str | None = Field(default=None, max_length=2000)


@router.post("/api/v1/quality-feedback")
def submit_quality_feedback(
    request: Request,
    payload: QualityFeedbackIn = Body(...),
) -> dict[str, Any]:
    """Accept positive/negative feedback on a quality_designation. Public.

    Returns the persisted record's `id` and `feedback_at`."""
    ip = real_client_ip(request)
    ua = request.headers.get("user-agent")

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quality_feedback (
                    vehicle_identifier, h3_10_index, polarity,
                    designation_observed, comment,
                    reporter_ip, reporter_user_agent
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, feedback_at
                """,
                (
                    payload.vehicle_identifier, payload.h3_10_index,
                    payload.polarity, payload.designation_observed,
                    payload.comment, ip, ua,
                ),
            )
            new_id, ts = cur.fetchone()
        conn.commit()

    log.info(
        "quality_feedback id=%d vehicle=%s polarity=%s designation=%s",
        new_id, payload.vehicle_identifier, payload.polarity,
        payload.designation_observed,
    )
    return {
        "id": int(new_id),
        "feedback_at": ts.isoformat(),
    }


@router.get("/api/v1/private/quality-feedback")
def list_quality_feedback(
    user: SessionUser = Depends(require_admin),
    since: str | None = Query(None, description="ISO 8601 UTC; default = now - 7d"),
    until: str | None = Query(None, description="ISO 8601 UTC; default = now"),
    vehicle_identifier: str | None = Query(None, min_length=16, max_length=16),
    polarity: str | None = Query(None, pattern="^(positive|negative)$"),
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    """Auth-gated full feedback listing."""
    now = datetime.now(timezone.utc)
    try:
        end = datetime.fromisoformat(until.replace("Z", "+00:00")) if until else now
        start = (
            datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since else end - timedelta(days=7)
        )
    except ValueError as e:
        raise HTTPException(400, f"bad time format: {e}")
    if end < start:
        raise HTTPException(400, "until < since")

    where = ["feedback_at >= %s", "feedback_at <= %s"]
    params: list[Any] = [start, end]
    if vehicle_identifier:
        where.append("vehicle_identifier = %s")
        params.append(vehicle_identifier)
    if polarity:
        where.append("polarity = %s")
        params.append(polarity)
    params.append(limit)

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, feedback_at, vehicle_identifier, h3_10_index,
                       polarity, designation_observed, comment,
                       reporter_ip, reporter_user_agent
                FROM quality_feedback
                WHERE {' AND '.join(where)}
                ORDER BY feedback_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()

    return {
        "since": start.isoformat(),
        "until": end.isoformat(),
        "count": len(rows),
        "viewed_by": user.email,
        "feedback": [
            {
                "id": int(r[0]),
                "feedback_at": r[1].isoformat(),
                "vehicle_identifier": r[2],
                "h3_10_index": int(r[3]),
                "polarity": r[4],
                "designation_observed": r[5],
                "comment": r[6],
                "reporter_ip": str(r[7]) if r[7] else None,
                "reporter_user_agent": r[8],
            }
            for r in rows
        ],
    }
