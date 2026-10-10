"""The Fleet admin reads and writes the in-app console needs, as JSON.

    GET    /api/v1/private/reports/queue             the reports queue, filtered
    POST   /api/v1/private/reports/{id}/reinstate    put back a rider resolution
    GET    /api/v1/private/fleet/reporters           per-account volume + spread
    GET    /api/v1/private/fleet/watches             SMS watches
    POST   /api/v1/private/fleet/watches             start one on your OWN phone
    DELETE /api/v1/private/fleet/watches/{id}        stop one

These are the four gaps Phase 1 left: everything else the console's Fleet
category needs (census, resolve, dossier, export) was already exposed by
src/api_fleet_reports.py and had simply never been called.

ONE IMPLEMENTATION, TWO SURFACES. The queue and the reporters view read
`src/fleet_admin_queries.py`, which is the same code the Jinja pages at
/admin/fleet/reports and /admin/fleet/reporters now call.
tests/test_fleet_admin_json_pg.py asserts the two surfaces agree from the
same fixture, because the alternative is a console that quietly reports a
different number of open reports than the portal does.

AUTH is `accounts.require_admin` — the rider-session allowlist gate, checked
live, same as every other /api/v1/private/* route. Note that this is a
DIFFERENT door from the /admin pages' `auth.require_admin` (GitHub OAuth), so
writes from here are attributed to the acting ACCOUNT and writes from there to
the GitHub login. Both columns exist for exactly that reason.

NO RIDER EMAILS. Reporters are account id plus public username, as on the
pages. A watch carries the account it texts, never the number.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from . import admin_watch, fleet_admin_queries, fleet_reports, vehicle_identity
from .accounts import SessionUser, require_admin
from .pg import connection

log = logging.getLogger(__name__)

router = APIRouter()

_MAX_REASON = 500


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _acting_login(user: SessionUser) -> str:
    """What goes in the *_by_login audit columns when an admin acts through
    the app. `admin_device_watches.created_by_login` is NOT NULL and the
    column is a GitHub login on the portal's side, so the account id goes in
    marked as such — never the email."""
    return f"account:{user.account_id}"


# ---------------------------------------------------------------------------
# 1. The reports queue
# ---------------------------------------------------------------------------

@router.get("/api/v1/private/reports/queue")
def reports_queue(
    user: SessionUser = Depends(require_admin),
    report_type: str | None = Query(None, description="exact device_reports.report_type"),
    reason: str | None = Query(
        None, description="a not_rideable reason, or 'unspecified' for one that named none"),
    region: str | None = Query(None, description="neighborhood name; see `regions`"),
    standing: str | None = Query(None, pattern="^(yes|no)?$",
                                 description="only reports that still stand, or only those that do not"),
    status: str | None = Query(None, pattern="^(open|resolved)?$"),
    page: int = Query(0, ge=0),
) -> dict[str, Any]:
    """One page of the reports queue, newest first, with the filter
    vocabularies alongside so the console can build its controls without a
    second request.

    Paging is 50 a page, and `has_next` says whether to offer another — a
    total count is deliberately absent, because counting every matching
    report on every page view costs more than it tells an operator.

    THE REGION FILTER IS SCAN-LIMITED. A report's region is a
    point-in-polygon over coordinates that may come from the report, the cell
    it was filed in, or the vehicle's current position, so it cannot go in the
    WHERE clause. With `region` set, the newest `scan_limit` matching reports
    are examined and `scan_limited` says whether that window was the whole
    set. Callers should SAY SO when it is true rather than implying there is
    nothing older.

    `as_of` is the newest complete cycle's snapshot, not the wall clock: it is
    what "still standing" is measured against."""
    with connection() as conn:
        with conn.cursor() as cur:
            q = fleet_admin_queries.reports_queue(
                cur, report_type=report_type, reason=reason, region=region,
                standing=standing, status=status, page=page)
    return {
        "as_of": _iso(q["as_of"]),
        "page": q["page"],
        "page_size": fleet_admin_queries.PAGE_SIZE,
        "has_next": q["has_next"],
        "scan_limit": fleet_admin_queries.REGION_SCAN_LIMIT,
        "scan_limited": q["scan_limited"],
        "reports": [_queue_row(r) for r in q["rows"]],
        "filters": {
            "report_types": list(fleet_admin_queries.report_types()),
            "reasons": list(fleet_admin_queries.reason_options()),
            "regions": fleet_admin_queries.region_names(),
        },
    }


def _queue_row(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    for k in ("observed_at", "reported_at", "resolved_at"):
        out[k] = _iso(r[k])
    return out


# ---------------------------------------------------------------------------
# 2. Reinstate a rider's resolution
# ---------------------------------------------------------------------------

class ReinstateIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=_MAX_REASON)


@router.post("/api/v1/private/reports/{report_id}/reinstate")
def reinstate_report(
    report_id: int = Path(..., ge=1),
    payload: ReinstateIn = Body(...),
    user: SessionUser = Depends(require_admin),
) -> dict[str, Any]:
    """Put back a report that a RIDER's condition check resolved (plan §4.4):
    a false "no longer a problem" un-hides a vehicle. An ADMIN's resolution is
    final and is refused with 409 — there is no path here to rewrite an
    admin's own judgement. 404 when there is no such report.

    The rider's answers stay in device_condition_check_answers and the points
    they were paid are not clawed back; who reinstated it and why is stamped
    on the report, so nothing is erased."""
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(422, "reason must not be blank")
    with connection() as conn:
        with conn.cursor() as cur:
            try:
                fleet_reports.reinstate_report(
                    cur, report_id, reason=reason, account_id=user.account_id)
            except fleet_reports.ReportNotFound:
                raise HTTPException(404, "no such report")
            except fleet_reports.NotRiderResolved:
                raise HTTPException(409, "that report was not resolved by a rider check")
        conn.commit()
    log.info("device report id=%d reinstated by admin_account=%d", report_id,
             user.account_id)
    return {"id": report_id, "reinstated": True, "reason": reason}


# ---------------------------------------------------------------------------
# 3. Reporters
# ---------------------------------------------------------------------------

#: rep.* and chk.* both select an `account_id`, and the unnamed type counters
#: all arrive as `count`, so the row dict is read by name only for the columns
#: that are unambiguous. The coalesced id is `aid`; the type counts come from
#: `by_type`, which the query builder fills positionally.
_REP_FIELDS = ("reports", "vehicles", "cells", "days_active", "hours_of_day",
               "voided", "rider_resolved")
_CHK_FIELDS = ("checks", "no_ride_checks", "resolutions", "reconfirmations",
               "feed_confirmed", "feed_unconfirmed")


@router.get("/api/v1/private/fleet/reporters")
def fleet_reporters(
    user: SessionUser = Depends(require_admin),
    days: int = Query(30, ge=1, le=365),
    account_id: int | None = Query(
        None, ge=1, description="also return this account's reports, checks, "
                                "hour-of-day profile and H3-8 spread"),
) -> dict[str, Any]:
    """Per-account report volume and spread, for spotting griefing (§2.6(2)),
    with each account's rider condition-check resolutions beside its reports:
    an account resolving reports nobody else's rides corroborate is the same
    signal as one filing them.

    Ordered by reports + resolutions, 500 accounts at most. Accounts are
    identified by id and public username; there are no emails here.

    With `account_id`, `detail` also carries that account's reports and checks
    (300 of each at most), its reports by hour of Denver local time, and its
    spread over H3 resolution 8 — about 0.7 km², chosen to show a cluster
    without naming an address."""
    with connection() as conn:
        with conn.cursor() as cur:
            q = fleet_admin_queries.reporters(cur, days=days, account_id=account_id)
    return {
        "days": q["days"],
        "since": _iso(q["since"]),
        "report_types": list(fleet_admin_queries.report_types()),
        "reporters": [_reporter_row(r) for r in q["rows"]],
        "detail": _reporter_detail(q["detail"]) if q["detail"] else None,
    }


def _int(v: Any) -> int | None:
    return None if v is None else int(v)


def _reporter_row(rec: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "account_id": _int(rec.get("aid")),
        "public_username": rec.get("public_username"),
        "first_at": _iso(rec.get("first_at")),
        "last_at": _iso(rec.get("last_at")),
        "by_type": {k: _int(v) or 0 for k, v in rec["by_type"].items()},
    }
    for f in _REP_FIELDS + _CHK_FIELDS:
        out[f] = _int(rec.get(f)) or 0
    return out


def _reporter_detail(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_id": d["account_id"],
        "public_username": d["public_username"],
        "reports": [
            {**r, "reported_at": _iso(r["reported_at"]),
             "resolved_at": _iso(r["resolved_at"])}
            for r in d["reports"]],
        "checks": [
            {**c, "submitted_at": _iso(c["submitted_at"])} for c in d["checks"]],
        "by_hour": [{"hour": h, "reports": n} for h, n in d["by_hour"]],
        "cells": [{"cell": c, "reports": n} for c, n in d["cells"]],
    }


# ---------------------------------------------------------------------------
# 4. SMS watches
# ---------------------------------------------------------------------------

class WatchIn(BaseModel):
    vehicle_identifier: str = Field(..., pattern=r"^[0-9a-f]{16}$")
    hours: int = Field(admin_watch.DEFAULT_WATCH_HOURS, ge=1,
                       le=admin_watch.MAX_WATCH_HOURS)
    consent: bool = False


@router.get("/api/v1/private/fleet/watches")
def list_watches(
    user: SessionUser = Depends(require_admin),
    vehicle_identifier: str | None = Query(None, pattern=r"^[0-9a-f]{16}$"),
    include_ended: bool = Query(True),
) -> dict[str, Any]:
    """Every admin SMS watch, live ones first. Shows which account each watch
    texts and how many texts it has spent of its cap — never the number."""
    with connection() as conn:
        with conn.cursor() as cur:
            watches = admin_watch.list_watches(
                cur, vehicle_identifier=vehicle_identifier,
                include_ended=include_ended)
    return {
        "limits": {
            "max_hours": admin_watch.MAX_WATCH_HOURS,
            "default_hours": admin_watch.DEFAULT_WATCH_HOURS,
            "max_texts": admin_watch.MAX_TEXTS_PER_WATCH,
            "max_live_per_account": admin_watch.MAX_LIVE_WATCHES_PER_ACCOUNT,
        },
        "watches": [_watch_row(w, user) for w in watches],
    }


def _watch_row(w: dict[str, Any], user: SessionUser) -> dict[str, Any]:
    return {
        "id": int(w["id"]),
        "vehicle_identifier": w["vehicle_identifier"],
        "display_name": vehicle_identity.public_name(w["vehicle_identifier"]),
        "account_id": _int(w["account_id"]),
        "public_username": w["public_username"],
        # So the console can offer Stop only on a watch texting this admin's
        # own phone; someone else's watch is theirs to end.
        "mine": w["account_id"] == user.account_id,
        "created_at": _iso(w["created_at"]),
        "expires_at": _iso(w["expires_at"]),
        "ended_at": _iso(w["ended_at"]),
        "ended_reason": w["ended_reason"],
        "texts_sent": int(w["texts_sent"] or 0),
        "last_texted_at": _iso(w["last_texted_at"]),
        "last_event": w["last_event"],
        "live": bool(w["live"]),
    }


@router.post("/api/v1/private/fleet/watches", status_code=201)
def start_watch(
    payload: WatchIn = Body(...),
    user: SessionUser = Depends(require_admin),
) -> dict[str, Any]:
    """Start an SMS watch on one vehicle, texting THIS admin's own verified
    phone.

    Unlike the portal's form, there is no recipient field: an admin signed in
    here can only sign themselves up. Starting texts to a colleague's phone is
    a thing one person should not be able to do to another from a phone screen,
    and the portal's form already exists for the rare case that it is wanted.

    `consent` must be true — the watch sends SMS, and 422 is the honest answer
    to a request that has not said so. 409 for everything the watch itself
    refuses: no verified phone, STOP on that number, an unknown vehicle, or
    this account's live-watch cap.
    """
    if not payload.consent:
        raise HTTPException(422, "consent must be true: this sends SMS to your phone")
    if not re.fullmatch(r"[0-9a-f]{16}", payload.vehicle_identifier):
        raise HTTPException(422, "vehicle_identifier must be 16 hex")
    try:
        w = admin_watch.subscribe(
            vehicle_identifier=payload.vehicle_identifier,
            # require_admin matched this email against the allowlist, so
            # it is not None here (a phone-only account is never an admin).
            account_email=user.email or "",
            login=_acting_login(user),
            hours=payload.hours,
            consent=True,
        )
    except admin_watch.WatchError as e:
        raise HTTPException(409, str(e))
    log.info("admin watch %d on %s by admin_account=%d", w["id"],
             payload.vehicle_identifier, user.account_id)
    return {
        "id": w["id"],
        "vehicle_identifier": payload.vehicle_identifier,
        "display_name": vehicle_identity.public_name(payload.vehicle_identifier),
        "expires_at": _iso(w["expires_at"]),
        "hours": payload.hours,
    }


@router.delete("/api/v1/private/fleet/watches/{watch_id}")
def stop_watch(
    watch_id: int = Path(..., ge=1),
    user: SessionUser = Depends(require_admin),
) -> dict[str, Any]:
    """Stop a live watch. 404 when there is no live watch with that id —
    including one that already expired, which is the same outcome by a
    different route. Idempotent in effect: a second call 404s rather than
    pretending to stop it twice."""
    if not admin_watch.unsubscribe(watch_id, login=_acting_login(user)):
        raise HTTPException(404, "no live watch with that id")
    log.info("admin watch %d stopped by admin_account=%d", watch_id, user.account_id)
    return {"id": watch_id, "stopped": True}
