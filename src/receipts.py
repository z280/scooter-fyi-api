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

import io
import logging
import os
import uuid

import boto3
from botocore.config import Config as BotoConfig
from PIL import Image, UnidentifiedImageError

from .config import load, r2_credentials

log = logging.getLogger(__name__)

MAX_RECEIPT_BYTES = 10 * 1024 * 1024
_JPEG_QUALITY = 85
# Cap pixel dimensions: bounds decode memory (PIL bomb protection is on by
# default, this just tightens it) and receipt legibility never needs more.
_MAX_DIMENSION = 4096


class ReceiptError(Exception):
    """Invalid upload — message is safe for a 400 detail."""


def receipts_bucket() -> str | None:
    """Configured bucket name, or None when receipt storage is off."""
    if not r2_credentials():
        return None
    return os.environ.get("R2_RECEIPTS_BUCKET") or None


def strip_and_reencode(data: bytes) -> bytes:
    """Decode the image and re-encode pixels-only to JPEG.

    Re-encoding (rather than deleting EXIF blocks) guarantees nothing
    survives: EXIF, XMP, IPTC, thumbnails, GPS — none of it transfers to
    a fresh Image buffer.
    """
    if len(data) > MAX_RECEIPT_BYTES:
        raise ReceiptError(f"receipt too large (max {MAX_RECEIPT_BYTES // (1024*1024)} MB)")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ReceiptError("receipt is not a readable image") from e

    if img.width > _MAX_DIMENSION or img.height > _MAX_DIMENSION:
        img.thumbnail((_MAX_DIMENSION, _MAX_DIMENSION))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    out = io.BytesIO()
    # PIL's JPEG encoder only writes EXIF/XMP when they're passed as save()
    # kwargs — a plain re-save emits pixels (+ ICC color profile) only.
    # test_receipts.py asserts the output of a GPS-tagged input has no EXIF.
    img.save(out, format="JPEG", quality=_JPEG_QUALITY)
    return out.getvalue()


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
