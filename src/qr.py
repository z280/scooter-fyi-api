"""Per-device QR code content storage + plate validation (requirement #15;
sql/032_device_qr_codes.sql).

Veo's physical QR sticker is assumed to encode the same rental_uris
deep-link shape GBFS embeds (e.g. "...&number=<plate>") — the same plate
printed on the scooter and used by src/identity.py. extract_plate()'s
fallback (treat the whole trimmed payload as the plate) covers a
plain-text-plate sticker if that assumption turns out wrong; verify
against a real sticker before shipping.
"""

from __future__ import annotations

import logging
import re

from .identity import hash_plate

log = logging.getLogger(__name__)

# Mirrors src/ingest.py's plate-extraction regex (same upstream URL
# shape). Duplicated rather than imported: that symbol is private to the
# GBFS ingest hot path, which this rider-facing module has no other
# dependency on. If ingest.py's shape ever changes, update both.
_NUMBER_RE = re.compile(r"[?&]number=([^&]+)")


class QrValidationError(Exception):
    """Safe for a 400 detail."""


def extract_plate(qr_raw_value: str) -> str | None:
    m = _NUMBER_RE.search(qr_raw_value)
    if m:
        return m.group(1)
    stripped = qr_raw_value.strip()
    return stripped or None


def validate_scan(qr_raw_value: str, claimed_vehicle_identifier: str) -> str:
    """Return the extracted plate if it hashes to claimed_vehicle_identifier;
    raise QrValidationError (400-safe) otherwise."""
    plate = extract_plate(qr_raw_value)
    if not plate:
        raise QrValidationError("could not read a plate from this QR code")
    computed = hash_plate(plate)
    if computed != claimed_vehicle_identifier:
        log.warning("qr scan mismatch: claimed=%s computed=%s",
                    claimed_vehicle_identifier, computed)
        raise QrValidationError("scanned QR does not match the claimed device")
    return plate
