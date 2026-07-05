"""Stable public scooter identifier.

GBFS rotates `bike_id` per trip. Veo embeds a stable visible plate (e.g.
"1025543") in `rental_uris.android/.ios` as `&number=...`. That plate is
printed on the side of the scooter and in its QR code. We key all
cross-cycle state (device_state, device_history, negative_reports) on an
HMAC of the plate rather than the plate itself.

The hash is HMAC-SHA256 keyed by VEHICLE_IDENTIFIER_SALT, truncated to
16 hex characters (64 bits — at ~8k devices, collision probability is
≈ 2e-12, negligible).

PRIVACY MODEL --------------------------------------------------------
The raw plate is NEVER exposed over an unauthenticated wire. Only the
HMAC `vehicle_identifier` appears on the public /api/v1/devices/current
endpoint; the plate is served exclusively by the bearer-gated
/api/v1/private/* endpoints. (A §1.1 promotion of the plate to the
public endpoint was later reverted — see API_REQUIREMENTS.md §1.1.)
With the salt set:
  * Casual scrapers see opaque 16-char identifiers, not plates.
  * Anyone with our public API alone cannot reverse identifier → plate.
  * The identifier is the stable primary key across all state/history
    tables — plates could be re-painted or re-issued; the HMAC namespace
    is ours. Per-device position history and dwell trails are queryable
    only by identifier via the authenticated /api/v1/private/* endpoints,
    and public report submission accepts the identifier so reporters
    never need to transmit a plate.
  * Only our system can resolve identifier ↔ plate (the salt is a
    server-side secret).

The salt is LOAD-BEARING and REQUIRED. There is no dev fallback: a
missing env var is a startup error, not a silent degradation. Treat the
salt like a database encryption key — generate once, back up out of
repo, never rotate without intent.
"""

from __future__ import annotations

import hmac
import os
from hashlib import sha256

_IDENTIFIER_LEN = 16
_ENV_VAR = "VEHICLE_IDENTIFIER_SALT"


def _salt() -> bytes:
    raw = os.environ.get(_ENV_VAR)
    if not raw:
        raise RuntimeError(
            f"{_ENV_VAR} is required but unset. Generate with "
            "`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` "
            "and set it in your environment (GHA secret in prod, .env / shell "
            "locally). Losing the value strands every stored vehicle_identifier."
        )
    return raw.encode("utf-8")


def hash_plate(plate: str | None) -> str | None:
    """Return the stable public identifier for a raw plate, or None.

    HMAC-SHA256(salt, plate) truncated to 16 hex chars. Deterministic for
    a given (salt, plate). Anyone with the salt can rederive; anyone
    without it cannot.
    """
    if not plate:
        return None
    mac = hmac.new(_salt(), plate.encode("utf-8"), sha256)
    return mac.hexdigest()[:_IDENTIFIER_LEN]
