"""Ride transaction screenshot endpoints (requirement #16). Namespaced
under /api/v1/tracked-rides (not /api/v1/rides, which belongs to the
separate off-feed ride tracker — see sql/027_tracked_rides.sql and
sql/035_off_feed_rides.sql)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .accounts import SessionUser, require_session
from .pg import connection
from .ratelimit import enforce
from .ride_screenshots import (
    MAX_SCREENSHOT_BYTES,
    RideScreenshotError,
    delete_screenshot,
    presigned_screenshot_url,
    screenshots_bucket,
    store_screenshot,
)

log = logging.getLogger(__name__)

router = APIRouter()

_LIMIT_SCREENSHOT_PER_ACCOUNT = (20, 3600)


def _parse_ride_id(ride_id: str) -> UUID:
    try:
        return UUID(ride_id)
    except ValueError:
        raise HTTPException(400, "ride id must be a UUID")


@router.post("/api/v1/tracked-rides/{ride_id}/screenshots")
async def upload_ride_screenshot(
    ride_id: str,
    request: Request,
    screenshot_type: str = Query(..., pattern="^(overview|receipt)$"),
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    rid = _parse_ride_id(ride_id)
    form = await request.form()
    screenshot = form.get("screenshot")
    if screenshot is None or isinstance(screenshot, str):
        raise HTTPException(422, "multipart field `screenshot` is required")
    data = await screenshot.read()
    if len(data) > MAX_SCREENSHOT_BYTES:
        raise HTTPException(413, "screenshot too large (max 10 MB)")
    if not screenshots_bucket():
        raise HTTPException(503, "screenshot storage not configured")

    old_key: str | None = None
    new_key: str | None = None
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                enforce(cur, bucket="ride_screenshot_account", key=str(user.account_id),
                        limit=_LIMIT_SCREENSHOT_PER_ACCOUNT[0],
                        window_seconds=_LIMIT_SCREENSHOT_PER_ACCOUNT[1])
                cur.execute(
                    "SELECT 1 FROM tracked_rides WHERE id = %s AND account_id = %s",
                    (str(rid), user.account_id),
                )
                if cur.fetchone() is None:
                    raise HTTPException(404, "no such ride")
                cur.execute(
                    "SELECT r2_key FROM ride_transaction_screenshots "
                    "WHERE ride_id = %s AND screenshot_type = %s",
                    (str(rid), screenshot_type),
                )
                existing = cur.fetchone()
                old_key = existing[0] if existing else None

                new_key = store_screenshot(user.account_id, data)
                cur.execute(
                    """
                    INSERT INTO ride_transaction_screenshots (
                        ride_id, account_id, screenshot_type, r2_key
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (ride_id, screenshot_type) DO UPDATE SET
                        r2_key = EXCLUDED.r2_key, updated_at = NOW(),
                        account_id = EXCLUDED.account_id
                    RETURNING id, created_at, updated_at
                    """,
                    (str(rid), user.account_id, screenshot_type, new_key),
                )
                row_id, created_at, updated_at = cur.fetchone()
            conn.commit()
    except HTTPException:
        if new_key is not None:
            try:
                delete_screenshot(new_key)
            except RideScreenshotError:
                log.exception("orphan cleanup failed for %s", new_key)
        raise
    except RideScreenshotError as e:
        raise HTTPException(400, str(e))

    if old_key and old_key != new_key:
        try:
            delete_screenshot(old_key)
        except RideScreenshotError:
            log.exception("failed to delete superseded screenshot %s", old_key)

    return {"id": int(row_id), "ride_id": str(rid), "screenshot_type": screenshot_type,
            "created_at": created_at.isoformat(), "updated_at": updated_at.isoformat(),
            "replaced_previous": old_key is not None}


@router.get("/api/v1/tracked-rides/{ride_id}/screenshots")
def list_ride_screenshots(
    ride_id: str, user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    rid = _parse_ride_id(ride_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM tracked_rides WHERE id = %s AND account_id = %s",
                (str(rid), user.account_id),
            )
            if cur.fetchone() is None:
                raise HTTPException(404, "no such ride")
            cur.execute(
                "SELECT id, screenshot_type, r2_key, created_at, updated_at "
                "FROM ride_transaction_screenshots WHERE ride_id = %s",
                (str(rid),),
            )
            rows = cur.fetchall()
    return {
        "ride_id": str(rid),
        "screenshots": [
            {"id": int(r[0]), "screenshot_type": r[1], "url": presigned_screenshot_url(r[2]),
             "created_at": r[3].isoformat(), "updated_at": r[4].isoformat()}
            for r in rows
        ],
    }
