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
from datetime import datetime, timedelta, timezone
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
       * vehicle_plate          (raw, never in public endpoint)
       * first_observed_at_location  (from device_state)
       * number_failed_starts        (from device_state)
       * first_ever_observed_at      (from device_state)

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
                       EXISTS (
                           SELECT 1 FROM negative_reports nr
                           WHERE nr.vehicle_identifier = r.vehicle_identifier
                             AND nr.h3_10_index = r.h3_10_index
                             AND nr.reported_at >= NOW() - INTERVAL '24 hours'
                       ) AS has_negative_report
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
                       last_observed_at, last_cycle_id
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
