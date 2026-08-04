"""Device photo storage (requirements #12/14; sql/031_device_photos.sql).

Mirrors src/receipts.py's upload pipeline, except the destination is the
R2_BUCKET_NAME bucket under a device-photos/ prefix — device photos are
public content (unlike receipts) — and the pipeline itself is the shared
one in src/image_processing.py. "Public content" is about who may look,
not about the bucket's ACL: see public_photo_url for how a photo is
addressed when that bucket has no public origin in front of it.
"""

from __future__ import annotations

import logging
import uuid
from functools import lru_cache

import boto3
from botocore.config import Config as BotoConfig

from .config import load, r2_credentials
from .image_processing import ImageProcessingError, strip_and_reencode

log = logging.getLogger(__name__)

MAX_DEVICE_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTOS_PER_DEVICE = 3
# Lifetime of the presigned fallback URL (see public_photo_url). Longer than
# ride_screenshots' 600s because these are rendered in an <img> the rider
# leaves open — a gallery that 403s while it is still on screen is the bug
# this fallback exists to avoid — and short enough that a copied URL is not a
# durable handout of an object we may later hide.
PRESIGNED_TTL_SECONDS = 3600


class DevicePhotoError(Exception):
    """Invalid upload — message is safe for a 400 detail."""


def device_photos_bucket() -> str | None:
    """The PUBLIC bucket (R2_BUCKET_NAME), or None when R2 creds are
    absent — same fail-open-to-503 contract as receipts.receipts_bucket()."""
    creds = r2_credentials()
    return creds["bucket"] if creds else None


@lru_cache(maxsize=1)
def _client(endpoint_url: str, access_key_id: str, secret_access_key: str):
    """Cached because public_photo_url now signs a URL PER PHOTO: building a
    fresh boto3 client for each row of a listing is milliseconds apiece that
    buy nothing. Keyed on the credentials so a rotated key still takes
    effect; boto3's low-level clients are thread-safe, which is what the
    threadpool FastAPI runs sync endpoints in requires."""
    return boto3.client(
        "s3", endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=BotoConfig(signature_version="s3v4"), region_name="auto",
    )


def _r2_client():
    creds = r2_credentials()
    cfg = load().r2
    return _client(cfg.endpoint_url(creds["account_id"]),
                   creds["access_key_id"], creds["secret_access_key"])


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


def presigned_photo_url(key: str, expires_in: int = PRESIGNED_TTL_SECONDS) -> str | None:
    """A signed, time-limited GET URL for a photo — same mechanism as
    ride_screenshots.presigned_screenshot_url, against this bucket. None
    only when R2 is unconfigured entirely, in which case there is no photo
    to point at either (uploads 503 out at device_photos_bucket())."""
    bucket = device_photos_bucket()
    if not bucket:
        return None
    return _r2_client().generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in,
    )


def public_photo_url(key: str) -> str | None:
    """A URL a browser can render the photo from.

    Prefers config.json's r2.public_base_url — a plain static object URL is
    cacheable, permanent, and costs nothing to produce. That setting needs a
    Cloudflare Dashboard step this repo cannot perform, and it is a step with
    a real cost here: R2_BUCKET_NAME is not a photos-only bucket, so opening
    it publicly would expose everything else stored alongside device-photos/.

    So the fallback is a presigned URL rather than None. Returning None
    stranded every rendered gallery — clients (correctly) refuse to put a
    non-http(s) value in an <img src>, so a null reached the rider as "this
    photo can't be displayed" for a photo that had uploaded fine."""
    base = load().r2.public_base_url
    if base:
        return f"{base.rstrip('/')}/{key}"
    return presigned_photo_url(key)
