"""Receipt image handling for discount reports (§3.2).

Receipts are the only user-uploaded binary this system holds. Rules:

  * PRIVATE R2 bucket (R2_RECEIPTS_BUCKET) — never the public archive
    bucket, never a public URL. Reads happen only via operator tooling.
  * EXIF (and every other metadata block) is stripped on ingest by fully
    re-encoding the pixel data to a fresh JPEG. A rider photographing a
    receipt at home would otherwise hand us their GPS coordinates.
  * 18-month retention — `python -m src.cli cleanup_receipts` (daily cron)
    deletes the R2 object and stamps receipt_deleted_at; the report row
    itself is kept.
"""

from __future__ import annotations

import logging
import os
import uuid

import boto3
from botocore.config import Config as BotoConfig

from .config import load, r2_credentials
from .image_processing import ImageProcessingError
from .image_processing import strip_and_reencode as _strip_and_reencode

log = logging.getLogger(__name__)

MAX_RECEIPT_BYTES = 10 * 1024 * 1024


class ReceiptError(Exception):
    """Invalid upload — message is safe for a 400 detail."""


def receipts_bucket() -> str | None:
    """Configured bucket name, or None when receipt storage is off."""
    if not r2_credentials():
        return None
    return os.environ.get("R2_RECEIPTS_BUCKET") or None


def strip_and_reencode(data: bytes) -> bytes:
    """Decode the image and re-encode pixels-only to JPEG — thin wrapper
    around src/image_processing.py's shared pipeline (also used by device
    photos and ride transaction screenshots), translated back to
    ReceiptError so every existing caller/test is unaffected."""
    try:
        return _strip_and_reencode(data, max_bytes=MAX_RECEIPT_BYTES)
    except ImageProcessingError as e:
        raise ReceiptError(str(e)) from e


def _r2_client():
    creds = r2_credentials()
    cfg = load().r2
    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint_url(creds["account_id"]),
        aws_access_key_id=creds["access_key_id"],
        aws_secret_access_key=creds["secret_access_key"],
        config=BotoConfig(signature_version="s3v4"),
        region_name="auto",
    )


def store_receipt(account_id: int, data: bytes) -> str:
    """Strip, re-encode, upload. Returns the R2 object key."""
    bucket = receipts_bucket()
    if not bucket:
        raise ReceiptError("receipt storage not configured")
    clean = strip_and_reencode(data)
    key = f"receipts/{account_id}/{uuid.uuid4()}.jpg"
    _r2_client().put_object(
        Bucket=bucket, Key=key, Body=clean, ContentType="image/jpeg"
    )
    log.info("receipt stored: %s (%d bytes after re-encode)", key, len(clean))
    return key


def delete_receipt(key: str) -> None:
    bucket = receipts_bucket()
    if not bucket:
        raise ReceiptError("receipt storage not configured")
    _r2_client().delete_object(Bucket=bucket, Key=key)
