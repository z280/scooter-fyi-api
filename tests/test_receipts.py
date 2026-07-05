"""Receipt EXIF stripping — the §3.2 privacy guarantee."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from src.receipts import MAX_RECEIPT_BYTES, ReceiptError, strip_and_reencode


def _jpeg_with_gps() -> bytes:
    """A tiny JPEG carrying identifying EXIF tags."""
    img = Image.new("RGB", (32, 32), (200, 10, 10))
    exif = Image.Exif()
    exif[0x013B] = "definitely a person"          # Artist
    exif[0x0131] = "CameraOS 99.1 (GPS 39.7,-105)" # Software — stands in for GPS
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    data = buf.getvalue()
    assert b"definitely a person" in data          # sanity: EXIF really embedded
    return data


def test_strip_removes_exif():
    out = strip_and_reencode(_jpeg_with_gps())
    assert b"definitely a person" not in out
    reopened = Image.open(io.BytesIO(out))
    assert dict(reopened.getexif()) == {}


def test_output_is_jpeg_with_same_dimensions():
    out = strip_and_reencode(_jpeg_with_gps())
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert img.size == (32, 32)


def test_png_input_reencoded_to_jpeg():
    buf = io.BytesIO()
    Image.new("RGBA", (16, 16), (0, 100, 0, 255)).save(buf, format="PNG")
    out = strip_and_reencode(buf.getvalue())
    assert Image.open(io.BytesIO(out)).format == "JPEG"


def test_non_image_rejected():
    with pytest.raises(ReceiptError):
        strip_and_reencode(b"%PDF-1.4 not an image at all")


def test_oversize_rejected():
    with pytest.raises(ReceiptError):
        strip_and_reencode(b"\xff" * (MAX_RECEIPT_BYTES + 1))


def test_huge_dimensions_downscaled():
    buf = io.BytesIO()
    Image.new("RGB", (5000, 1000), (1, 2, 3)).save(buf, format="JPEG", quality=30)
    out = strip_and_reencode(buf.getvalue())
    img = Image.open(io.BytesIO(out))
    assert max(img.size) <= 4096
