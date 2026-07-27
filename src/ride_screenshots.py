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
