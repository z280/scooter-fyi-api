"""Shared image re-encode pipeline for every user-uploaded photo in this
app (receipts, device photos, ride transaction screenshots). Decode, bake
in EXIF rotation, cap dimensions, strip ALL metadata by re-encoding to a
fresh JPEG.

Extracted from src/receipts.py (the original — and until now, only —
caller) when device-photo/screenshot uploads needed the identical
pipeline; receipts.py now re-exports this via a thin wrapper, so every
existing caller/test is unaffected (tests/test_receipts.py passes
unmodified).
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps, UnidentifiedImageError

DEFAULT_MAX_DIMENSION = 4096
DEFAULT_JPEG_QUALITY = 85


class ImageProcessingError(Exception):
    """Invalid upload — message is safe for a 400 detail."""


def strip_and_reencode(
    data: bytes,
    *,
    max_bytes: int,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> bytes:
    """Decode the image and re-encode pixels-only to JPEG.

    Re-encoding (rather than deleting EXIF blocks) guarantees nothing
    survives: EXIF, XMP, IPTC, thumbnails, GPS — none of it transfers to
    a fresh Image buffer.
    """
    if len(data) > max_bytes:
        raise ImageProcessingError(f"image too large (max {max_bytes // (1024 * 1024)} MB)")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ImageProcessingError("upload is not a readable image") from e

    # Bake EXIF orientation into the pixels before the EXIF block itself is
    # dropped below — otherwise phone photos that rely on orientation (not
    # rotated pixels) come out sideways once re-encoded.
    img = ImageOps.exif_transpose(img)

    if img.width > max_dimension or img.height > max_dimension:
        img.thumbnail((max_dimension, max_dimension))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    out = io.BytesIO()
    # PIL's JPEG encoder only writes EXIF/XMP when passed as save() kwargs —
    # a plain re-save emits pixels (+ ICC color profile) only.
    img.save(out, format="JPEG", quality=jpeg_quality)
    return out.getvalue()
