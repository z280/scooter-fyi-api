"""Admin-only REST endpoints — gated by ADMIN_EMAILS membership.

These endpoints expose data that is privacy-sensitive to publish in the
clear: raw plate numbers, persistent dwell times at a location, and the
position history of an individual scooter. The data already exists in
the database (it's derived from public GBFS), but joining it under a
stable identifier is the boundary GBFS's per-trip rotation is meant to
prevent.

All routes require `Authorization: Bearer <token>` for a session whose
email is on the admin allowlist (see src/accounts.py `require_admin` /
`is_admin_email`) — reachable via ANY sign-in door, and checked live
against the table rather than read off the session's scopes. This replaced the
retired GitHub map-auth bearer flow (API_REQUIREMENTS.md §2.5).
"""

from __future__ import annotations

import logging
import re
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any

import h3
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .identity import hash_plate
from . import accounts
from .accounts import SessionUser, require_admin
from .pg import connection
from .ratelimit import enforce

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# /api/v1/private/devices/lookup
# ---------------------------------------------------------------------------
@router.get("/api/v1/private/devices/lookup")
def private_devices_lookup(
    user: SessionUser = Depends(require_admin),
    plate: str | None = Query(None, description="Raw visible plate number, e.g. '1025543'"),
    vehicle_identifier: str | None = Query(None, description="16-char identifier"),
) -> dict[str, Any]:
    """Resolve plate → identifier or identifier → plate, and return the
    current state row. Exactly one of `plate` or `vehicle_identifier` is
    required."""
    if (plate is None) == (vehicle_identifier is None):
        raise HTTPException(400, "exactly one of plate or vehicle_identifier required")

    if plate is not None:
        target = hash_plate(plate)
        if not target:
            raise HTTPException(400, "empty plate")
    else:
        target = vehicle_identifier

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT vehicle_identifier, vehicle_plate, current_device_id,
                       current_lat, current_lon, current_spatial_status,
                       current_form_factor, first_observed_at_location,
                       number_failed_starts, first_ever_observed_at,
                       last_observed_at, last_cycle_id,
                       max_observed_range_meters, max_observed_range_at,
                       current_vehicle_use_type, current_vehicle_model_name
                FROM device_state
                WHERE vehicle_identifier = %s
                """,
                (target,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "no device with that identifier/plate")

    return {
        "vehicle_identifier": row[0],
        "vehicle_plate": row[1],
        "current_device_id": row[2],
        "current_lat": float(row[3]) if row[3] is not None else None,
        "current_lon": float(row[4]) if row[4] is not None else None,
        "current_spatial_status": row[5],
        "current_form_factor": row[6],
        "first_observed_at_location": row[7].isoformat() if row[7] else None,
        "number_failed_starts": int(row[8]) if row[8] is not None else None,
        "first_ever_observed_at": row[9].isoformat() if row[9] else None,
        "last_observed_at": row[10].isoformat() if row[10] else None,
        "last_cycle_id": str(row[11]) if row[11] else None,
        "max_observed_range_meters": row[12],
        "max_observed_range_at": row[13].isoformat() if row[13] else None,
        "vehicle_use_type": row[14],
        "vehicle_model_name": row[15],
    }


# ---------------------------------------------------------------------------
# /api/v1/private/devices/lookup-batch
# ---------------------------------------------------------------------------
_MAX_BATCH_PLATES = 200


@router.get("/api/v1/private/devices/lookup-batch")
def private_devices_lookup_batch(
    user: SessionUser = Depends(require_admin),
    plates: str = Query(..., description="Comma-separated raw plate numbers"),
) -> dict[str, Any]:
    """Batch plate -> max_observed_range_meters (+ form factor / dwell)
    lookup. Built for hand-labeled ground-truth sets — e.g. spotting a
    plate in the Veo app and noting its displayed model name (Apollo,
    Cosmo, ...), then checking whether it clusters with other same-model
    plates by observed battery ceiling. See sql/011_max_observed_range.sql
    for why max_observed_range_meters is the reliable signal instead of
    vehicle_type_id.

    Plates with no device_state row (never seen, or no plate in the
    upstream payload) are reported separately rather than silently
    dropped, since a missing plate in a ground-truth set is worth
    noticing. Duplicate plates in the request are deduplicated against
    `requested`/the batch-size cap, not against each other's counts.
    """
    # dict.fromkeys dedupes while preserving first-seen order — the same
    # plate typed twice while building a ground-truth list shouldn't
    # double-count against the batch size cap.
    raw_plates = list(dict.fromkeys(p.strip() for p in plates.split(",") if p.strip()))
    if not raw_plates:
        raise HTTPException(400, "plates must contain at least one non-empty value")
    if len(raw_plates) > _MAX_BATCH_PLATES:
        raise HTTPException(400, f"at most {_MAX_BATCH_PLATES} plates per request")

    by_identifier: dict[str, str] = {}
    for p in raw_plates:
        ident = hash_plate(p)
        if ident:
            by_identifier[ident] = p

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT vehicle_identifier, vehicle_plate, current_form_factor,
                       max_observed_range_meters, max_observed_range_at,
                       first_ever_observed_at, last_observed_at,
                       current_vehicle_use_type, current_vehicle_model_name
                FROM device_state
                WHERE vehicle_identifier = ANY(%s)
                """,
                (list(by_identifier.keys()),),
            )
            rows = cur.fetchall()

    found = [
        {
            "vehicle_plate": r[1],
            "vehicle_identifier": r[0],
            "form_factor": r[2],
            "max_observed_range_meters": r[3],
            "max_observed_range_at": r[4].isoformat() if r[4] else None,
            "first_ever_observed_at": r[5].isoformat() if r[5] else None,
            "last_observed_at": r[6].isoformat() if r[6] else None,
            "vehicle_use_type": r[7],
            "vehicle_model_name": r[8],
        }
        for r in rows
    ]
    found_plates = {d["vehicle_plate"] for d in found}
    not_found = [p for p in raw_plates if p not in found_plates]

    # Sorted by max_observed_range_meters so a mixed-model batch visually
    # clusters — NULLs (still soaking, or never reported a charge level)
    # sort last rather than erroring the comparison.
    found.sort(key=lambda d: (d["max_observed_range_meters"] is None,
                               d["max_observed_range_meters"] or 0),
               reverse=True)

    return {
        "viewed_by": user.email,
        "requested": len(raw_plates),
        "found": found,
        "not_found": not_found,
    }


# ---------------------------------------------------------------------------
# /api/v1/private/devices/{vehicle_identifier}/history
# ---------------------------------------------------------------------------
_VID_RE = re.compile(r"^[0-9a-f]{16}$")
_DEFAULT_RANGE_DAYS = 7
_MAX_RANGE_DAYS = 365


@router.get("/api/v1/private/devices/{vehicle_identifier}/history")
def private_device_history(
    vehicle_identifier: str,
    user: SessionUser = Depends(require_admin),
    since: str | None = Query(None, description="ISO 8601 UTC; default = now - 7d"),
    until: str | None = Query(None, description="ISO 8601 UTC; default = now"),
    limit: int = Query(2000, ge=1, le=10000),
) -> dict[str, Any]:
    """Time-ordered list of position stops for a single scooter.

    Each row is one 'stop' — the scooter arrived at a position and stayed
    until movement was detected. dwell_failed_starts counts bike_id
    rotations that happened during the stop without the scooter moving.
    """
    if not _VID_RE.match(vehicle_identifier):
        raise HTTPException(400, "vehicle_identifier must be 16 lowercase hex chars")

    now = datetime.now(timezone.utc)
    try:
        end = datetime.fromisoformat(until.replace("Z", "+00:00")) if until else now
        start = (
            datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since
            else end - timedelta(days=_DEFAULT_RANGE_DAYS)
        )
    except ValueError as e:
        raise HTTPException(400, f"bad time format: {e}")

    if end < start:
        raise HTTPException(400, "until < since")
    if (end - start).days > _MAX_RANGE_DAYS:
        raise HTTPException(400, f"window > {_MAX_RANGE_DAYS} days")

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT vehicle_plate
                FROM device_state
                WHERE vehicle_identifier = %s
                """,
                (vehicle_identifier,),
            )
            ds_row = cur.fetchone()
            if not ds_row:
                raise HTTPException(404, "no device with that identifier")
            plate = ds_row[0]

            # Stops whose [snapshot_time, departed_at] window overlaps the
            # requested range. departed_at IS NULL means "still there now."
            cur.execute(
                """
                SELECT snapshot_time, departed_at, lat, lon, spatial_status,
                       form_factor, device_id_observed, dwell_failed_starts,
                       cycle_id
                FROM device_history
                WHERE vehicle_identifier = %s
                  AND snapshot_time <= %s
                  AND (departed_at IS NULL OR departed_at >= %s)
                ORDER BY snapshot_time ASC
                LIMIT %s
                """,
                (vehicle_identifier, end, start, limit),
            )
            rows = cur.fetchall()

    stops = [
        {
            "arrived_at": r[0].isoformat(),
            "departed_at": r[1].isoformat() if r[1] else None,
            "lat": float(r[2]),
            "lon": float(r[3]),
            "spatial_status": r[4],
            "form_factor": r[5],
            "device_id_observed": r[6],
            "dwell_failed_starts": int(r[7] or 0),
            "cycle_id": str(r[8]) if r[8] else None,
        }
        for r in rows
    ]

    return {
        "vehicle_identifier": vehicle_identifier,
        "vehicle_plate": plate,
        "since": start.isoformat(),
        "until": end.isoformat(),
        "stop_count": len(stops),
        "viewed_by": user.email,
        "stops": stops,
    }


# ---------------------------------------------------------------------------
# /api/v1/private/devices/max-ranges
# ---------------------------------------------------------------------------
# Sorted dump of every tracked device by the highest current_range_meters
# it has ever reported, descending. Used to separate the pedal/2nd-seat
# bicycles (larger battery, higher achievable charge) from the smaller-
# battery model — the public GBFS feed does not distinguish them. Run the
# system for a few days after deploying this column, then inspect the head
# of this list for the high-battery cluster.
@router.get("/api/v1/private/devices/max-ranges")
def private_devices_max_ranges(
    user: SessionUser = Depends(require_admin),
    form_factor: str | None = Query(None),
    limit: int = Query(5000, ge=1, le=20000),
) -> dict[str, Any]:
    where = ["max_observed_range_meters IS NOT NULL"]
    params: list[Any] = []
    if form_factor:
        where.append("current_form_factor = %s")
        params.append(form_factor)
    params.append(limit)

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT vehicle_identifier, vehicle_plate, current_form_factor,
                       max_observed_range_meters, max_observed_range_at,
                       first_ever_observed_at, last_observed_at
                FROM device_state
                WHERE {' AND '.join(where)}
                ORDER BY max_observed_range_meters DESC NULLS LAST,
                         vehicle_identifier
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()

    devices = [
        {
            "vehicle_identifier": r[0],
            "vehicle_plate": r[1],
            "form_factor": r[2],
            "max_observed_range_meters": r[3],
            "max_observed_range_at": r[4].isoformat() if r[4] else None,
            "first_ever_observed_at": r[5].isoformat() if r[5] else None,
            "last_observed_at": r[6].isoformat() if r[6] else None,
        }
        for r in rows
    ]
    return {
        "viewed_by": user.email,
        "filters": {"form_factor": form_factor},
        "device_count": len(devices),
        "devices": devices,
    }


# ---------------------------------------------------------------------------
# /api/v1/private/trips/daily — read back src/daily_trips.py's rollup
# ---------------------------------------------------------------------------
@router.get("/api/v1/private/trips/daily")
def private_trips_daily(
    user: SessionUser = Depends(require_admin),
    date: str = Query(..., description="Denver-local date, YYYY-MM-DD"),
    limit: int = Query(100, ge=1, le=5000),
) -> dict[str, Any]:
    """Daily trip/popularity rollup for one Denver-local calendar day —
    computed at 9am by `python -m src.cli daily_trips` (see
    src/daily_trips.py). `vehicles` is ranked by `popularity_rank`
    ascending (1 = most trips that day; ties share a rank).
    """
    try:
        d = date_cls.fromisoformat(date)
    except ValueError as e:
        raise HTTPException(400, f"bad date format: {e}")

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT total_trips, distinct_vehicles_tripped, computed_at "
                "FROM daily_trip_summary WHERE trip_date = %s",
                (d,),
            )
            summary = cur.fetchone()
            if not summary:
                raise HTTPException(404, f"no trip rollup for {date}")

            cur.execute(
                """
                SELECT vehicle_identifier, vehicle_plate, form_factor,
                       vehicle_use_type, vehicle_model_name,
                       trip_count, popularity_rank
                FROM daily_vehicle_trip_counts
                WHERE trip_date = %s
                ORDER BY popularity_rank ASC, vehicle_plate ASC
                LIMIT %s
                """,
                (d, limit),
            )
            rows = cur.fetchall()

    return {
        "viewed_by": user.email,
        "trip_date": d.isoformat(),
        "total_trips": int(summary[0]),
        "distinct_vehicles_tripped": int(summary[1]),
        "computed_at": summary[2].isoformat() if summary[2] else None,
        "vehicles": [
            {
                "vehicle_identifier": r[0],
                "vehicle_plate": r[1],
                "form_factor": r[2],
                "vehicle_use_type": r[3],
                "vehicle_model_name": r[4],
                "trip_count": int(r[5]),
                "popularity_rank": int(r[6]),
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# /api/v1/private/area-leaders — admin sibling of GET /api/v1/leaderboard/map
# (FEATURE_PLAN §11.4: "full ranks, ties, account ids"). Unlike the public
# endpoint (src/api_leaderboard.py), this view applies NO privacy filtering —
# every stored rank 1..3, its real account_id, and the raw stored points/
# first_point_at tie-break provenance, exactly as sql/048's
# h3_r8_area_leaders table holds them.
# ---------------------------------------------------------------------------
@router.get("/api/v1/private/area-leaders")
def private_area_leaders(
    user: SessionUser = Depends(require_admin),
) -> dict[str, Any]:
    """Full, unfiltered §11 H3 r8 area-leader report: every stored rank
    (1..3) per cell with its real account_id, points, and first_point_at
    tie-break provenance — no show_in_leaderboards/show_public_username/
    display_name filtering (that read-time privacy layer belongs to the
    public GET /api/v1/leaderboard/map only)."""
    with connection() as conn:
        with conn.cursor() as cur:
            # REVIEW FIX: same snapshot-consistency issue (and same fix) as
            # the public GET /api/v1/leaderboard/map sibling — see that
            # endpoint's own comment. REPEATABLE READ must be set before the
            # first statement in the transaction.
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cur.execute(
                """
                SELECT computed_at, window_start, window_end, cell_count, led_cells
                FROM h3_r8_area_leader_runs
                ORDER BY computed_at DESC, id DESC
                LIMIT 1
                """
            )
            run = cur.fetchone()
            if not run:
                raise HTTPException(503, "no leaderboard computed yet")
            computed_at, window_start, window_end, cell_count, led_cells = run

            cur.execute(
                """
                SELECT r.h3_8_index, r.has_devices, r.has_points,
                       r.total_points, r.distinct_earners,
                       l.rank, l.account_id, l.points, l.first_point_at
                FROM h3_r8_area_report r
                LEFT JOIN h3_r8_area_leaders l ON l.h3_8_index = r.h3_8_index
                ORDER BY r.h3_8_index, l.rank
                """
            )
            rows = cur.fetchall()

    cells: dict[str, dict[str, Any]] = {}
    for (h3_idx, has_devices, has_points, total_points, distinct_earners,
         rank, account_id, points, first_point_at) in rows:
        key = h3.int_to_str(int(h3_idx))
        cell = cells.get(key)
        if cell is None:
            cell = cells[key] = {
                "has_devices": bool(has_devices),
                "has_points": bool(has_points),
                "total_points": int(total_points),
                "distinct_earners": int(distinct_earners),
                "leaders": [],
            }
        if rank is None:
            continue
        cell["leaders"].append({
            "rank": int(rank),
            "account_id": account_id,
            "points": int(points),
            "first_point_at": first_point_at.isoformat() if first_point_at else None,
        })

    return {
        "viewed_by": user.email,
        "computed_at": computed_at.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "cell_count": int(cell_count),
        "led_cells": int(led_cells),
        "cells": cells,
    }


# ---------------------------------------------------------------------------
# /api/v1/private/regional-leaders — admin sibling of GET
# /api/v1/leaderboard/regional (sql/054 regional_leaders). Unlike the public
# endpoint, this view applies NO privacy filtering — every stored rank
# 1..MAX_REGIONAL_LEADERS, its real account_id, and the raw stored
# points/first_point_at tie-break provenance.
# ---------------------------------------------------------------------------
@router.get("/api/v1/private/regional-leaders")
def private_regional_leaders(
    user: SessionUser = Depends(require_admin),
) -> dict[str, Any]:
    """Full, unfiltered whole-database leaderboard: every stored rank with
    its real account_id, points, and first_point_at tie-break provenance —
    no show_in_leaderboards/show_public_username/display_name filtering."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cur.execute(
                """
                SELECT computed_at, window_start, window_end
                FROM h3_r8_area_leader_runs
                ORDER BY computed_at DESC, id DESC
                LIMIT 1
                """
            )
            run = cur.fetchone()
            if not run:
                raise HTTPException(503, "no leaderboard computed yet")
            computed_at, window_start, window_end = run

            cur.execute(
                "SELECT rank, account_id, points, first_point_at "
                "FROM regional_leaders ORDER BY rank"
            )
            rows = cur.fetchall()

    return {
        "viewed_by": user.email,
        "computed_at": computed_at.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "leaders": [
            {
                "rank": int(rank),
                "account_id": account_id,
                "points": int(points),
                "first_point_at": first_point_at.isoformat() if first_point_at else None,
            }
            for rank, account_id, points, first_point_at in rows
        ],
    }


# ---------------------------------------------------------------------------
# /api/v1/private/admins — CRUD on the admin allowlist
# ---------------------------------------------------------------------------
# TRUST BOUNDARY, stated plainly: these routes are gated by require_admin,
# which is allowlist membership. So an account-session admin can grant and
# revoke account-session admin. That is a real change from the portal at
# /admin/admins, whose GitHub-OAuth gate is a SEPARATE boundary — there, a
# GitHub operator decides who counts as an account admin, and an account
# admin could not promote anyone.
#
# The portal still exists and still works; it is now the out-of-band way in
# when nobody holds an account admin session. Two consequences follow, and
# both are handled below rather than left implicit:
#
#   * The list is self-propagating. Adding an admin hands over exactly the
#     power you hold, including this endpoint. `added_by` records who did it
#     so the table is an audit trail rather than just a set.
#   * The list can be emptied. Removing the final admin would lock every
#     account out of /private/* and of this endpoint — recoverable only
#     through the GitHub portal or the CLI. That is refused (409); every
#     other removal, including your own, is allowed.
_LIMIT_ADMIN_WRITE = (30, 3600)


class AdminEmailIn(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)


def _admin_rows(you: str | None) -> dict[str, Any]:
    """The allowlist as the UI renders it. `is_you` is computed server-side
    against the SAME normalization the allowlist is keyed by, so the client
    never has to reimplement it to decide which row is dangerous to remove."""
    rows = accounts.list_admins()
    me = accounts.normalize_email(you) if you else None
    return {
        "count": len(rows),
        "admins": [{**r, "is_you": r["email"] == me} for r in rows],
    }


@router.get("/api/v1/private/admins")
def private_admins_list(
    user: SessionUser = Depends(require_admin),
) -> dict[str, Any]:
    """Everyone on the admin allowlist, newest first."""
    return _admin_rows(user.email)


@router.post("/api/v1/private/admins")
def private_admins_add(
    payload: AdminEmailIn = Body(...),
    user: SessionUser = Depends(require_admin),
) -> dict[str, Any]:
    """Add an email. Idempotent: re-adding an existing admin is a 200 with
    `added: false`, not a conflict — the caller's intent is satisfied either
    way, and a 409 here would just be noise in a UI that re-submits."""
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="admin_allowlist_write", key=str(user.account_id),
                    limit=_LIMIT_ADMIN_WRITE[0], window_seconds=_LIMIT_ADMIN_WRITE[1])
        conn.commit()
    try:
        added = accounts.add_admin(payload.email, added_by=user.email)
    except ValueError:
        raise HTTPException(400, "not an email address")
    if added:
        log.info("admin allowlist: %s added %s", user.email, payload.email)
    return {**_admin_rows(user.email),
            "email": accounts.normalize_email(payload.email), "added": added}


@router.delete("/api/v1/private/admins")
def private_admins_remove(
    user: SessionUser = Depends(require_admin),
    email: str = Query(..., description="Address to remove; matched normalized"),
) -> dict[str, Any]:
    """Remove an email. `email` rides in the query string rather than the
    path because an address is full of characters a path segment handles
    badly — dots, `+`, and an `@` that some proxies normalize.

    Refuses to remove the LAST admin (409). Removing yourself is allowed
    while others remain: stepping down is legitimate, locking the door on
    an empty room is not.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="admin_allowlist_write", key=str(user.account_id),
                    limit=_LIMIT_ADMIN_WRITE[0], window_seconds=_LIMIT_ADMIN_WRITE[1])
        conn.commit()
    target = accounts.normalize_email(email)
    try:
        # Guarded, not accounts.remove_admin: the count and the DELETE have to
        # be one transaction. Checking here and deleting there would let two
        # concurrent removals of different addresses both pass a count of two
        # and both commit, emptying the allowlist — the exact state this
        # refusal exists to prevent.
        removed = accounts.remove_admin_guarded(target)
    except accounts.LastAdminError:
        raise HTTPException(
            409,
            "refusing to remove the last admin — add another first, or use "
            "the GitHub-gated portal at /admin/admins",
        )
    if removed:
        log.info("admin allowlist: %s removed %s", user.email, target)
    return {**_admin_rows(user.email), "email": target, "removed": removed}


# ---------------------------------------------------------------------------
# /api/v1/private/analytics/* — read back src/analytics.py's rollups
# ---------------------------------------------------------------------------
@router.get("/api/v1/private/analytics/daily")
def private_analytics_daily(
    user: SessionUser = Depends(require_admin),
    days: int = Query(30, ge=1, le=3650),
) -> dict[str, Any]:
    """Per-day totals from telemetry_daily (computed at 9:20am Denver by
    `python -m src.cli rollup_analytics`). Aggregate-only — the raw
    tables are never exposed over HTTP.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT day, SUM(events)::int,
                       MAX(visitors)::int, MAX(sessions)::int
                FROM telemetry_daily
                WHERE day >= CURRENT_DATE - %s
                GROUP BY day ORDER BY day DESC
                """,
                (days,),
            )
            rows = [
                {
                    "day": str(day),
                    "events": events,
                    # Per-event-name visitor counts can't be summed
                    # (one visitor fires many event names); MAX of the
                    # per-name distinct counts is a lower bound, with
                    # page_load's count the practical daily-active figure.
                    "max_event_visitors": visitors,
                    "max_event_sessions": sessions,
                }
                for day, events, visitors, sessions in cur.fetchall()
            ]
    return {"days": days, "daily": rows}


@router.get("/api/v1/private/analytics/events")
def private_analytics_events(
    user: SessionUser = Depends(require_admin),
    name: str = Query(..., min_length=1, max_length=64),
    days: int = Query(30, ge=1, le=3650),
) -> dict[str, Any]:
    """One event name's daily rollup rows, prop_summary included."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT day, city_id, events, visitors, sessions, prop_summary
                FROM telemetry_daily
                WHERE name = %s AND day >= CURRENT_DATE - %s
                ORDER BY day DESC
                """,
                (name, days),
            )
            rows = [
                {
                    "day": str(day),
                    "city_id": city_id,
                    "events": events,
                    "visitors": visitors,
                    "sessions": sessions,
                    "prop_summary": prop_summary,
                }
                for day, city_id, events, visitors, sessions, prop_summary
                in cur.fetchall()
            ]
    return {"name": name, "days": days, "daily": rows}


@router.get("/api/v1/private/analytics/requests/daily")
def private_analytics_requests_daily(
    user: SessionUser = Depends(require_admin),
    days: int = Query(30, ge=1, le=3650),
) -> dict[str, Any]:
    """request_metrics_daily rows for the window, newest first."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT day, city_id, route, method, status_class,
                       requests, p50_ms, p95_ms, authed_requests
                FROM request_metrics_daily
                WHERE day >= CURRENT_DATE - %s
                ORDER BY day DESC, requests DESC
                """,
                (days,),
            )
            rows = [
                {
                    "day": str(day),
                    "city_id": city_id,
                    "route": route,
                    "method": method,
                    "status_class": status_class,
                    "requests": requests,
                    "p50_ms": p50,
                    "p95_ms": p95,
                    "authed_requests": authed,
                }
                for day, city_id, route, method, status_class,
                    requests, p50, p95, authed in cur.fetchall()
            ]
    return {"days": days, "daily": rows}
