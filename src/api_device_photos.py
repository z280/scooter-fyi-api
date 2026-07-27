"""Device photo endpoints (requirements #12-14, #17)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field

from .accounts import SessionUser, require_session
from .device_photos import (
    MAX_DEVICE_PHOTO_BYTES,
    MAX_PHOTOS_PER_DEVICE,
    DevicePhotoError,
    delete_device_photo,
    device_photos_bucket,
    public_photo_url,
    store_device_photo,
)
from .pg import connection
from .ratelimit import enforce
from .ride_screenshots import presigned_screenshot_url

log = logging.getLogger(__name__)

router = APIRouter()

_LIMIT_PHOTO_PER_ACCOUNT = (20, 3600)
_LIMIT_PHOTO_REPORT_PER_ACCOUNT = (10, 3600)
_VID_RE = r"^[0-9a-f]{16}$"


@router.post("/api/v1/devices/{vehicle_identifier}/photos")
async def upload_device_photo(
    request: Request,
    vehicle_identifier: str = Path(..., min_length=16, max_length=16, pattern=_VID_RE),
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    form = await request.form()
    photo = form.get("photo")
    if photo is None or isinstance(photo, str):
        raise HTTPException(422, "multipart field `photo` is required")
    data = await photo.read()
    if len(data) > MAX_DEVICE_PHOTO_BYTES:
        raise HTTPException(413, "photo too large (max 10 MB)")
    if not device_photos_bucket():
        raise HTTPException(503, "photo storage not configured")

    key: str | None = None
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                enforce(cur, bucket="device_photo_account", key=str(user.account_id),
                        limit=_LIMIT_PHOTO_PER_ACCOUNT[0],
                        window_seconds=_LIMIT_PHOTO_PER_ACCOUNT[1])
                # Closes the same "two concurrent uploads both observe
                # count=2 and both insert" race src/ratelimit.py's
                # docstring calls out for its own table.
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"device_photos:{vehicle_identifier}",),
                )
                cur.execute(
                    "SELECT COUNT(*) FROM device_photos "
                    "WHERE vehicle_identifier = %s AND status = 'visible'",
                    (vehicle_identifier,),
                )
                (count,) = cur.fetchone()
                if count >= MAX_PHOTOS_PER_DEVICE:
                    raise HTTPException(409, f"device already has {MAX_PHOTOS_PER_DEVICE} photos")
                key = store_device_photo(user.account_id, data)
                cur.execute(
                    """
                    INSERT INTO device_photos (vehicle_identifier, account_id, r2_key)
                    VALUES (%s, %s, %s) RETURNING id, created_at
                    """,
                    (vehicle_identifier, user.account_id, key),
                )
                new_id, created_at = cur.fetchone()
            conn.commit()
    except DevicePhotoError as e:
        raise HTTPException(400, str(e))
    except Exception:
        if key is not None:
            try:
                delete_device_photo(key)
            except DevicePhotoError:
                log.exception("orphaned device photo cleanup failed for %s", key)
        raise

    return {"id": int(new_id), "vehicle_identifier": vehicle_identifier,
            "photo_url": public_photo_url(key), "created_at": created_at.isoformat()}


@router.get("/api/v1/devices/{vehicle_identifier}/photos")
def list_device_photos(
    vehicle_identifier: str = Path(..., min_length=16, max_length=16, pattern=_VID_RE),
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    """Joins accounts.public_username at READ time (sql/025), respecting
    show_public_username, so a later username change or privacy-setting
    flip is reflected immediately. Still gated by require_session like
    every other endpoint in this project — "publicly retrievable" (item
    14) is satisfied by the R2 object URLs themselves being unauthenticated
    static files once you have one, not by making this listing API
    anonymous."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.r2_key, p.created_at,
                       CASE WHEN a.show_public_username THEN a.public_username ELSE NULL END
                FROM device_photos p
                JOIN accounts a ON a.id = p.account_id
                WHERE p.vehicle_identifier = %s AND p.status = 'visible'
                ORDER BY p.created_at ASC
                """,
                (vehicle_identifier,),
            )
            rows = cur.fetchall()
    return {
        "vehicle_identifier": vehicle_identifier, "count": len(rows),
        "photos": [
            {"id": int(r[0]), "photo_url": public_photo_url(r[1]),
             "created_at": r[2].isoformat(), "uploaded_by": r[3]}
            for r in rows
        ],
    }


class DevicePhotoReportIn(BaseModel):
    reason: str = Field(..., pattern="^(wrong_device|inappropriate|other)$")
    comment: str | None = Field(default=None, max_length=2000)


@router.post("/api/v1/photos/{photo_id}/reports")
def report_device_photo(
    photo_id: int,
    payload: DevicePhotoReportIn = Body(...),
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    """Reports a problem with a PHOTO (requirement #13) — distinct from
    device_reports, which reports on the DEVICE. No points are credited
    here: the "photo is wrong" +100pt award is gated on a future
    moderator-adjudication workflow, explicitly out of scope now."""
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="device_photo_report_account", key=str(user.account_id),
                    limit=_LIMIT_PHOTO_REPORT_PER_ACCOUNT[0],
                    window_seconds=_LIMIT_PHOTO_REPORT_PER_ACCOUNT[1])
            cur.execute("SELECT 1 FROM device_photos WHERE id = %s", (photo_id,))
            if cur.fetchone() is None:
                raise HTTPException(404, "no such photo")
            cur.execute(
                """
                INSERT INTO device_photo_reports (photo_id, account_id, reason, comment)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (photo_id, account_id) DO NOTHING
                RETURNING id, status, created_at
                """,
                (photo_id, user.account_id, payload.reason, payload.comment),
            )
            row = cur.fetchone()
        conn.commit()
    if row is None:
        return {"photo_id": photo_id, "deduped": True}
    new_id, status, created_at = row
    return {"id": int(new_id), "photo_id": photo_id, "reason": payload.reason,
            "status": status, "created_at": created_at.isoformat(), "deduped": False}


@router.get("/api/v1/photos/mine")
def list_my_photos(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    """'Review all photos I have uploaded' (requirement #17) — device
    photos AND ride transaction screenshots, combined into one call but
    kept as two top-level keys (different content/visibility models: one
    is public, the other private). Both scoped to the caller only, so
    including private screenshots here is not a leak: "private" means
    "not visible to OTHER accounts," not "hidden from its own uploader.\""""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, vehicle_identifier, r2_key, created_at, status "
                "FROM device_photos WHERE account_id = %s ORDER BY created_at DESC",
                (user.account_id,),
            )
            photo_rows = cur.fetchall()
            cur.execute(
                "SELECT id, ride_id, screenshot_type, r2_key, created_at "
                "FROM ride_transaction_screenshots WHERE account_id = %s "
                "ORDER BY created_at DESC",
                (user.account_id,),
            )
            screenshot_rows = cur.fetchall()
    return {
        "device_photos": [
            {"id": int(r[0]), "vehicle_identifier": r[1], "photo_url": public_photo_url(r[2]),
             "created_at": r[3].isoformat(), "status": r[4]}
            for r in photo_rows
        ],
        "ride_transaction_screenshots": [
            {"id": int(r[0]), "ride_id": str(r[1]), "screenshot_type": r[2],
             "url": presigned_screenshot_url(r[3]), "created_at": r[4].isoformat()}
            for r in screenshot_rows
        ],
    }
