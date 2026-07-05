"""Authenticated REST endpoints — gated by map-auth bearer tokens.

These endpoints expose data that is privacy-sensitive to publish in the
clear: raw plate numbers, persistent dwell times at a location, and the
position history of an individual scooter. The data already exists in
the database (it's derived from public GBFS), but joining it under a
stable identifier is the boundary GBFS's per-trip rotation is meant to
prevent.

All routes require `Authorization: Bearer <token>` minted by
src/map_auth.py. Token verification + last_used_at tracking lives in
src/map_auth_dep.py.
"""

from __future__ import annotations

import logging
import re
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from .identity import hash_plate
from .map_auth_dep import MapUser, require_map_user
from .pg import connection
from .quality import compute_quality_designation

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# /api/v1/private/devices/current
# ---------------------------------------------------------------------------
@router.get("/api/v1/private/devices/current")
def private_devices_current(
    user: MapUser = Depends(require_map_user),
    form_factor: str | None = Query(None),
    spatial_status: str | None = Query(None),
    include_outliers: bool = Query(False),
    bbox: str | None = Query(None),
) -> dict[str, Any]:
    """Same shape as /api/v1/devices/current, plus:
       * first_ever_observed_at      (from device_state)
       * max_observed_range_meters / max_observed_range_at

    (vehicle_plate, number_failed_starts, and first_observed_at_location
    were private-only until API_REQUIREMENTS.md §1.1/§1.2 promoted them
    to the public endpoint; they remain here for compatibility.)

    Devices without a vehicle_identifier are omitted (we can't track
    them across cycles, so the extra fields would be null anyway).
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cycle_id, snapshot_time
                FROM observation_cycles oc
                JOIN snapshot_metadata_core USING (cycle_id)
                WHERE oc.job_status = 'complete'
                ORDER BY snapshot_time DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(503, detail="no completed cycles yet")
            cycle_id, snapshot_time = row[0], row[1]

            where = ["r.cycle_id = %s", "r.vehicle_identifier IS NOT NULL"]
            params: list[Any] = [cycle_id]

            if spatial_status:
                where.append("r.spatial_status = %s")
                params.append(spatial_status)
            elif not include_outliers:
                where.append("r.spatial_status = 'denver_core'")

            if form_factor:
                where.append("r.form_factor = %s")
                params.append(form_factor)

            if bbox:
                parts = bbox.split(",")
                if len(parts) != 4:
                    raise HTTPException(400, "bbox must be 4 comma-separated numbers")
                try:
                    min_lon, min_lat, max_lon, max_lat = [float(p) for p in parts]
                except ValueError as e:
                    raise HTTPException(400, f"bbox parse error: {e}")
                where.append(
                    "r.longitude BETWEEN %s AND %s AND r.latitude BETWEEN %s AND %s"
                )
                params.extend([min_lon, max_lon, min_lat, max_lat])

            sql = f"""
                SELECT r.device_id, r.form_factor, r.latitude, r.longitude,
                       r.spatial_status, r.vehicle_identifier, r.vehicle_plate,
                       r.is_disabled, r.is_reserved, r.current_range_meters,
                       r.propulsion_type,
                       ds.first_observed_at_location, ds.number_failed_starts,
                       ds.first_ever_observed_at,
                       r.h3_8_index, r.h3_9_index, r.h3_10_index,
                       r.max_range_meters_for_type,
                       (EXISTS (
                           SELECT 1 FROM negative_reports nr
                           WHERE nr.vehicle_identifier = r.vehicle_identifier
                             AND nr.h3_10_index = r.h3_10_index
                             AND nr.reported_at >= NOW() - INTERVAL '24 hours'
                       ) OR EXISTS (
                           SELECT 1 FROM device_reports dr
                           WHERE dr.vehicle_identifier = r.vehicle_identifier
                             AND dr.h3_10_index = r.h3_10_index
                             AND dr.reported_at >= NOW() - INTERVAL '24 hours'
                       )) AS has_negative_report,
                       ds.max_observed_range_meters, ds.max_observed_range_at,
                       r.vehicle_use_type, r.vehicle_model_name
                FROM raw_telemetry_points r
                LEFT JOIN device_state ds USING (vehicle_identifier)
                WHERE {' AND '.join(where)}
                ORDER BY r.device_id
            """
            cur.execute(sql, params)
            rows = cur.fetchall()

    features = []
    for r in rows:
        quality = compute_quality_designation(
            current_range_meters=r[9],
            max_range_meters_for_type=r[17],
            is_disabled=r[7],
            is_reserved=r[8],
            number_failed_starts=int(r[12]) if r[12] is not None else None,
            first_observed_at_location=r[11],
            has_negative_report=bool(r[18]),
        )
        features.append({
            "type": "Feature",
            "id": r[5],  # vehicle_identifier as the GeoJSON id — stable across cycles
            "geometry": {"type": "Point", "coordinates": [float(r[3]), float(r[2])]},
            "properties": {
                "device_id": r[0],
                "form_factor": r[1],
                "spatial_status": r[4],
                "vehicle_identifier": r[5],
                "vehicle_plate": r[6],
                "is_disabled": r[7],
                "is_reserved": r[8],
                "current_range_meters": r[9],
                "propulsion_type": r[10],
                "first_observed_at_location": r[11].isoformat() if r[11] else None,
                "number_failed_starts": int(r[12]) if r[12] is not None else None,
                "first_ever_observed_at": r[13].isoformat() if r[13] else None,
                "h3_8_index": int(r[14]) if r[14] is not None else None,
                "h3_9_index": int(r[15]) if r[15] is not None else None,
                "h3_10_index": int(r[16]) if r[16] is not None else None,
                "max_range_meters_for_type": r[17],
                "has_negative_report": bool(r[18]),
                "quality_designation": quality,
                "max_observed_range_meters": r[19],
                "max_observed_range_at": r[20].isoformat() if r[20] else None,
                "vehicle_use_type": r[21],
                "vehicle_model_name": r[22],
            },
        })

    return {
        "type": "FeatureCollection",
        "metadata": {
            "cycle_id": str(cycle_id),
            "snapshot_time": snapshot_time.isoformat(),
            "device_count": len(features),
            "viewed_by": user.login,
            "filters": {
                "form_factor": form_factor,
                "spatial_status": spatial_status,
                "include_outliers": include_outliers,
                "bbox": bbox,
            },
        },
        "features": features,
    }


# ---------------------------------------------------------------------------
# /api/v1/private/devices/lookup
# ---------------------------------------------------------------------------
@router.get("/api/v1/private/devices/lookup")
def private_devices_lookup(
    user: MapUser = Depends(require_map_user),
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
    user: MapUser = Depends(require_map_user),
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
        "viewed_by": user.login,
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
    user: MapUser = Depends(require_map_user),
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
        "viewed_by": user.login,
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
    user: MapUser = Depends(require_map_user),
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
        "viewed_by": user.login,
        "filters": {"form_factor": form_factor},
        "device_count": len(devices),
        "devices": devices,
    }


# ---------------------------------------------------------------------------
# /api/v1/private/trips/daily — read back src/daily_trips.py's rollup
# ---------------------------------------------------------------------------
@router.get("/api/v1/private/trips/daily")
def private_trips_daily(
    user: MapUser = Depends(require_map_user),
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
        "viewed_by": user.login,
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
