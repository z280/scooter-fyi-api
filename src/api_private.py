"""Admin-only REST endpoints — gated by the Google `admin` session scope.

These endpoints expose data that is privacy-sensitive to publish in the
clear: raw plate numbers, persistent dwell times at a location, and the
position history of an individual scooter. The data already exists in
the database (it's derived from public GBFS), but joining it under a
stable identifier is the boundary GBFS's per-trip rotation is meant to
prevent.

All routes require `Authorization: Bearer <token>` for a session carrying
the `admin` scope (see src/accounts.py `require_admin`). Admin is granted
only via Google sign-in for an email on the ADMIN_EMAILS allowlist. This
replaced the retired GitHub map-auth bearer flow (API_REQUIREMENTS.md
§2.5).
"""

from __future__ import annotations

import logging
import re
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from .identity import hash_plate
from .accounts import SessionUser, require_admin
from .pg import connection

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
