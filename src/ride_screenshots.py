"""Ride transaction screenshot storage (requirement #16;
sql/033_ride_transaction_screenshots.sql). PRIVATE bucket — reuses
R2_RECEIPTS_BUCKET (same env var as src/receipts.py, no new bucket
needed), same EXIF-strip pipeline via src/image_processing.py."""

from __future__ import annotations

import logging
import os
import uuid

import boto3
from botocore.config import Config as BotoConfig

from .config import load, r2_credentials
from .image_processing import ImageProcessingError, strip_and_reencode

log = logging.getLogger(__name__)

MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024


class RideScreenshotError(Exception):
    """Safe for a 400 detail."""


def screenshots_bucket() -> str | None:
    if not r2_credentials():
        return None
    return os.environ.get("R2_RECEIPTS_BUCKET") or None


def _r2_client():
    creds = r2_credentials()
    cfg = load().r2
    return boto3.client(
        "s3", endpoint_url=cfg.endpoint_url(creds["account_id"]),
        aws_access_key_id=creds["access_key_id"],
        aws_secret_access_key=creds["secret_access_key"],
        config=BotoConfig(signature_version="s3v4"), region_name="auto",
    )


def store_screenshot(account_id: int, data: bytes) -> str:
    bucket = screenshots_bucket()
    if not bucket:
        raise RideScreenshotError("screenshot storage not configured")
    try:
        clean = strip_and_reencode(data, max_bytes=MAX_SCREENSHOT_BYTES)
    except ImageProcessingError as e:
        raise RideScreenshotError(str(e)) from e
    key = f"ride-screenshots/{account_id}/{uuid.uuid4()}.jpg"
    _r2_client().put_object(Bucket=bucket, Key=key, Body=clean, ContentType="image/jpeg")
    return key


def delete_screenshot(key: str) -> None:
    bucket = screenshots_bucket()
    if not bucket:
        raise RideScreenshotError("screenshot storage not configured")
    _r2_client().delete_object(Bucket=bucket, Key=key)


def presigned_screenshot_url(key: str, expires_in: int = 600) -> str | None:
    bucket = screenshots_bucket()
    if not bucket:
        return None
    return _r2_client().generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in
    )


def delete_screenshots_best_effort(keys: list[str]) -> dict[str, int]:
    """Delete a deleted ride's screenshot objects, after its rows are gone.

    For DELETE /api/v1/tracked-rides(/{id}): the rows cascade with the ride
    (sql/033), and the retention job only finds objects THROUGH rows, so
    without this the images outlived the ride forever. Best-effort by design —
    the rider's delete has already committed and must not fail over storage;
    an object this misses is unreferenced from now on, which is exactly what
    the weekly `sweep_orphan_images` removes. Logs counts only, never keys
    (a key carries the account id).
    """
    deleted = failed = 0
    for key in keys:
        try:
            delete_screenshot(key)
            deleted += 1
        except Exception as exc:  # noqa: BLE001 — never fail the rider's delete
            failed += 1
            log.warning("ride screenshot delete failed (%s); left for "
                        "sweep_orphan_images", type(exc).__name__)
    if keys:
        log.info("ride screenshots deleted with their ride: deleted=%d failed=%d",
                 deleted, failed)
    return {"deleted": deleted, "failed": failed}
