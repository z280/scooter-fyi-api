"""Device photo storage (requirements #12/14; sql/031_device_photos.sql).

Mirrors src/receipts.py's upload pipeline, except the destination is the
PUBLIC R2_BUCKET_NAME bucket under a device-photos/ prefix — device
photos must be publicly viewable (unlike the private receipts bucket) —
and the pipeline itself is the shared one in src/image_processing.py.
"""

from __future__ import annotations

import logging
import uuid

import boto3
from botocore.config import Config as BotoConfig

from .config import load, r2_credentials
from .image_processing import ImageProcessingError, strip_and_reencode

log = logging.getLogger(__name__)

MAX_DEVICE_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTOS_PER_DEVICE = 3


class DevicePhotoError(Exception):
    """Invalid upload — message is safe for a 400 detail."""


def device_photos_bucket() -> str | None:
    """The PUBLIC bucket (R2_BUCKET_NAME), or None when R2 creds are
    absent — same fail-open-to-503 contract as receipts.receipts_bucket()."""
    creds = r2_credentials()
    return creds["bucket"] if creds else None


def _r2_client():
    creds = r2_credentials()
    cfg = load().r2
    return boto3.client(
        "s3", endpoint_url=cfg.endpoint_url(creds["account_id"]),
        aws_access_key_id=creds["access_key_id"],
        aws_secret_access_key=creds["secret_access_key"],
        config=BotoConfig(signature_version="s3v4"), region_name="auto",
    )


def store_device_photo(account_id: int, data: bytes) -> str:
    """Strip metadata, re-encode, upload. Returns the R2 key. No DB write
    here — the caller owns the row insert + cap-of-3 check (both
    transactional), same split as receipts.store_receipt."""
    bucket = device_photos_bucket()
    if not bucket:
        raise DevicePhotoError("photo storage not configured")
    try:
        clean = strip_and_reencode(data, max_bytes=MAX_DEVICE_PHOTO_BYTES)
    except ImageProcessingError as e:
        raise DevicePhotoError(str(e)) from e
    key = f"device-photos/{account_id}/{uuid.uuid4()}.jpg"
    _r2_client().put_object(Bucket=bucket, Key=key, Body=clean, ContentType="image/jpeg")
    log.info("device photo stored: %s (%d bytes after re-encode)", key, len(clean))
    return key


def delete_device_photo(key: str) -> None:
    bucket = device_photos_bucket()
    if not bucket:
        raise DevicePhotoError("photo storage not configured")
    _r2_client().delete_object(Bucket=bucket, Key=key)


def public_photo_url(key: str) -> str | None:
    """Public HTTPS URL, or None until config.json's r2.public_base_url is
    set (a one-time Cloudflare Dashboard step — see src/config.py:R2Config)."""
    base = load().r2.public_base_url
    return f"{base.rstrip('/')}/{key}" if base else None
