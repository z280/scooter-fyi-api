"""POST /api/v1/devices/qr-scan (requirement #15 + the qr_scan points
bonus)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from .accounts import SessionUser, require_session
from .pg import connection
from .points import credit_qr_scan_points
from .qr import QrValidationError, validate_scan
from .ratelimit import enforce

router = APIRouter()

_LIMIT_QR_SCAN_PER_ACCOUNT = (20, 3600)


class QrScanIn(BaseModel):
    vehicle_identifier: str = Field(..., min_length=16, max_length=16, pattern=r"^[0-9a-f]{16}$")
    qr_raw_value: str = Field(..., min_length=1, max_length=2000)
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)


@router.post("/api/v1/devices/qr-scan")
def scan_device_qr(
    payload: QrScanIn = Body(...),
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    try:
        validate_scan(payload.qr_raw_value, payload.vehicle_identifier)
    except QrValidationError as e:
        raise HTTPException(400, str(e))

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="qr_scan_account", key=str(user.account_id),
                    limit=_LIMIT_QR_SCAN_PER_ACCOUNT[0],
                    window_seconds=_LIMIT_QR_SCAN_PER_ACCOUNT[1])
            cur.execute(
                "SELECT 1 FROM device_state WHERE vehicle_identifier = %s",
                (payload.vehicle_identifier,),
            )
            if cur.fetchone() is None:
                raise HTTPException(400, "unknown device")
            cur.execute(
                """
                INSERT INTO device_qr_codes (
                    vehicle_identifier, qr_raw_value, first_scanned_by, last_scanned_by
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (vehicle_identifier) DO UPDATE SET
                    qr_raw_value = EXCLUDED.qr_raw_value,
                    last_scanned_by = EXCLUDED.last_scanned_by,
                    last_scanned_at = NOW(),
                    scan_count = device_qr_codes.scan_count + 1
                RETURNING scan_count, first_scanned_at
                """,
                (payload.vehicle_identifier, payload.qr_raw_value,
                 user.account_id, user.account_id),
            )
            scan_count, first_scanned_at = cur.fetchone()
            awarded = credit_qr_scan_points(
                cur, account_id=user.account_id,
                vehicle_identifier=payload.vehicle_identifier,
                lat=payload.lat, lng=payload.lng,
            )
        conn.commit()
    return {
        "vehicle_identifier": payload.vehicle_identifier,
        "scan_count": int(scan_count),
        "first_scanned_at": first_scanned_at.isoformat(),
        "points_awarded": awarded["points"] if awarded else 0,
        "already_scanned_by_you": awarded is None,
    }
