"""GBFS fetch, freshness check, and Denver-envelope tagging.

Data is never dropped — outliers are tagged so the admin panel can still
see how many devices were reporting from China / Null Island / etc.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import h3
import httpx

from .config import load
from .identity import hash_plate
from .pg import connection

log = logging.getLogger(__name__)


class UpstreamError(Exception):
    """Wrap any failure that should land in api_failures."""

    def __init__(self, kind: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.kind = kind  # 'unavailable' | 'timeout' | 'malformed_payload'
        self.http_status = http_status


@dataclass(frozen=True)
class VehicleType:
    form_factor: str
    propulsion_type: str | None = None
    max_range_meters: int | None = None  # from GBFS vehicle_types.json


@dataclass(frozen=True)
class TaggedDevice:
    device_id: str                           # rotating GBFS bike_id
    vehicle_type_id: str | None
    form_factor: str
    lat: float
    lon: float
    spatial_status: str  # denver_core | china_glitch | other_outlier
    vehicle_plate: str | None = None         # raw plate from rental_uris (INTERNAL)
    vehicle_identifier: str | None = None    # sha256(plate)[:16] — public-safe stable ID
    is_disabled: bool | None = None
    is_reserved: bool | None = None
    current_range_meters: int | None = None
    propulsion_type: str | None = None
    max_range_meters_for_type: int | None = None  # per-type rated max from vehicle_types
    h3_8_index: int | None = None    # H3 v4 cell IDs at three resolutions
    h3_9_index: int | None = None    # (signed-bigint-safe — see sql/006)
    h3_10_index: int | None = None
    vehicle_use_type: str | None = None    # "sitting" | "standing" — see _KNOWN_VEHICLE_TYPES
    vehicle_model_name: str | None = None  # Veo's in-app name, e.g. "Apollo"; None if unconfirmed


# rental_uris.android/.ios deep-links embed the visible plate, e.g.
#   "https://gmjc.adj.st/?adj_t=5vyf0nr&number=1025543"
# That `number` is the only persistent device identifier the GBFS spec allows
# for dockless fleets — `bike_id` itself MUST rotate per trip.
_NUMBER_RE = re.compile(r"[?&]number=([^&]+)")

# Known vehicle_type_id -> ground truth, from direct visual confirmation
# in the field (2026-07-05) — NOT taken as-given from Veo's upstream
# vehicle_types.json, which is at least partly wrong (id=4 declares
# "scooter" but is a seated, pedal-equipped bike).
#
# form_factor:  override for Veo's registry value, or None to trust it.
#               Drives total_bike_denver/total_scooter_denver and
#               therefore the RFP compliance percentages, so a stale/wrong
#               upstream label needs a correction point, not silent trust.
# use_type:     "sitting" | "standing" — the accessibility-relevant split,
#               independent of form_factor. Every vehicle here happens to
#               agree with its (corrected) form_factor today, but the two
#               are tracked separately since GBFS's vocabulary and the
#               compliance-relevant distinction aren't guaranteed to be
#               the same axis forever.
# app_name:     Veo's own in-app display name for the model.
#
#   id=1 (Astro):  registry says "scooter" — agrees. Standing kick scooter.
#   id=3 (Cosmo):  registry says "bicycle" — agrees. Throttle e-bike, no
#                  pedals, seated.
#   id=4 (Apollo): registry says "scooter" — WRONG. Two-person pedal
#                  e-bike, seated, ~18mph. Overridden to bicycle.
#   id=5 (Rover):  registry says "scooter"/67000m — WRONG. A three-wheeled
#                  seated trike -- Veo calls it the Rover -- field-confirmed
#                  2026-07-29 off plate
#                  1036661. Overridden to bicycle so it stops inflating the
#                  standing-scooter share against the contract fleet cap. The
#                  67000m "max range" is Veo's junk metadata (same phantom
#                  entry as id=4) and is ignored — battery_percent is
#                  rank-based off the real SoC LUT, not this value.
#
#                  This id was labelled "Cosmo" until 2026-07-29. The
#                  2026-07-16 field note ("seated throttle e-bike, no
#                  pedals") is consistent with a trike in every respect
#                  except wheel count, which is what that observation
#                  missed; it also never explained why Veo would issue a
#                  separate type_id for a Cosmo. Being a trike does. The
#                  corroborating signal is commercial: across the whole
#                  live feed each vehicle_type_id maps 1:1 to exactly one
#                  pricing_plan_id, and id=5 has its own (483) distinct
#                  from the Cosmo's (225) — Veo sells it as its own
#                  product, so it is not a Cosmo hardware revision.
#
#                  A trike is strictly neither "bicycle" nor "scooter", and
#                  GBFS 2.2 would permit form_factor="other". We keep
#                  "bicycle" deliberately: form_factor exists here to
#                  reproduce the original 22 RFP metrics, whose vocabulary
#                  IS that binary, and a third value would break the
#                  total_bike + total_scooter == total_devices invariant
#                  for a fleet-cap axis the trike doesn't even ride on (the
#                  enforceable cap is on stand-up vehicles, and a trike is
#                  seated). The trike-ness lives in app_name instead.
#
# id=0, id=2 (registered bicycle classes, zero live devices as of
# 2026-07-05) are deliberately absent — nothing to correct. A vehicle_type_id
# absent here falls back to Veo's registry values as-is.
@dataclass(frozen=True)
class KnownVehicleType:
    app_name: str
    use_type: str                    # "sitting" | "standing"
    form_factor: str | None = None   # override; None = trust the registry


_KNOWN_VEHICLE_TYPES: dict[str, KnownVehicleType] = {
    "1": KnownVehicleType(app_name="Astro", use_type="standing"),
    "3": KnownVehicleType(app_name="Cosmo", use_type="sitting"),
    "4": KnownVehicleType(app_name="Apollo", use_type="sitting", form_factor="bicycle"),
    "5": KnownVehicleType(app_name="Rover", use_type="sitting", form_factor="bicycle"),
}


def _use_type_for_form_factor(form_factor: str) -> str | None:
    """Fallback derivation for vehicle_type_ids not in the known registry:
    every bicycle sits, every scooter stands, as of everything observed
    so far. Returns None for "unknown" form_factor — no basis to guess."""
    if form_factor == "bicycle":
        return "sitting"
    if form_factor == "scooter":
        return "standing"
    return None


def _extract_vehicle_plate(rental_uris: Any) -> str | None:
    if not isinstance(rental_uris, dict):
        return None
    for key in ("android", "ios", "web"):
        uri = rental_uris.get(key)
        if not isinstance(uri, str):
            continue
        m = _NUMBER_RE.search(uri)
        if m:
            return m.group(1)
    return None


def _h3_cells(lat: float, lon: float) -> tuple[int, int, int]:
    """Compute (h3_8, h3_9, h3_10) cell IDs as ints for a lat/lon. H3 v4
    returns strings; we convert to int for BIGINT-friendly storage."""
    return (
        int(h3.latlng_to_cell(lat, lon, 8), 16),
        int(h3.latlng_to_cell(lat, lon, 9), 16),
        int(h3.latlng_to_cell(lat, lon, 10), 16),
    )


@dataclass(frozen=True)
class IngestPayload:
    last_updated: int | None
    payload_sha256: str
    devices: list[TaggedDevice]
    raw_count: int


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------
def fetch_gbfs() -> tuple[dict[str, Any], dict[str, VehicleType]]:
    """Fetch free_bike_status + vehicle_types in a single client session.

    Returns (free_bike_status_json, {vehicle_type_id: VehicleType}).
    The vehicle_types lookup is best-effort: failure falls back to the
    canonical {"1": scooter, "3": bicycle} mapping with no propulsion data.
    """
    cfg = load().gbfs
    headers = {"User-Agent": cfg.user_agent}
    timeout = httpx.Timeout(cfg.timeout_seconds)

    with httpx.Client(headers=headers, timeout=timeout) as client:
        try:
            bikes_resp = client.get(cfg.url)
        except httpx.TimeoutException as e:
            raise UpstreamError("timeout", f"GBFS timeout: {e}") from e
        except httpx.HTTPError as e:
            raise UpstreamError("unavailable", f"GBFS HTTP error: {e}") from e

        if bikes_resp.status_code >= 400:
            raise UpstreamError(
                "unavailable",
                f"GBFS returned HTTP {bikes_resp.status_code}",
                http_status=bikes_resp.status_code,
            )

        try:
            bikes_json = bikes_resp.json()
        except json.JSONDecodeError as e:
            raise UpstreamError("malformed_payload", f"GBFS non-JSON: {e}") from e

        vt_map: dict[str, VehicleType] = {
            "1": VehicleType(form_factor="scooter"),
            "3": VehicleType(form_factor="bicycle"),
        }
        try:
            vt_resp = client.get(cfg.vehicle_types_url)
            if vt_resp.status_code < 400:
                vt_data = vt_resp.json().get("data", {}).get("vehicle_types", [])
                for vt in vt_data:
                    vid = str(vt.get("vehicle_type_id"))
                    ff = vt.get("form_factor")
                    if vid and ff:
                        try:
                            mr = vt.get("max_range_meters")
                            max_range = int(mr) if mr is not None else None
                        except (TypeError, ValueError):
                            max_range = None
                        vt_map[vid] = VehicleType(
                            form_factor=ff,
                            propulsion_type=vt.get("propulsion_type"),
                            max_range_meters=max_range,
                        )
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as e:
            log.warning("vehicle_types lookup failed, using fallback map: %s", e)

    return bikes_json, vt_map


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------
def previous_signature() -> tuple[int | None, str | None]:
    """Return (gbfs_last_updated, payload_sha256) from the most recent
    completed cycle, used to detect stale upstream data."""
    sql = """
        SELECT gbfs_last_updated, gbfs_payload_sha256
        FROM observation_cycles
        WHERE job_status = 'complete'
        ORDER BY start_ts DESC
        LIMIT 1
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
    if not row:
        return None, None
    return row[0], row[1]


def is_stale(this_last_updated: int | None, this_sha: str) -> bool:
    """Compare against the previous successful cycle."""
    prev_lu, prev_sha = previous_signature()
    if this_last_updated is not None and prev_lu is not None:
        return int(this_last_updated) == int(prev_lu)
    # fall back to payload hash if last_updated unavailable on either side
    return prev_sha is not None and prev_sha == this_sha


# ---------------------------------------------------------------------------
# Envelope tagging
# ---------------------------------------------------------------------------
def tag_envelope(payload: dict[str, Any], vt_map: dict[str, VehicleType]) -> IngestPayload:
    cfg = load()
    bikes = payload.get("data", {}).get("bikes", []) or []
    last_updated = payload.get("last_updated")

    # Deterministic signature — hash of (device_id, lat, lon) tuples sorted.
    sig_src = json.dumps(
        sorted(
            (str(b.get("bike_id") or b.get("id") or ""), b.get("lat"), b.get("lon"))
            for b in bikes
        ),
        separators=(",", ":"),
    )
    sha = hashlib.sha256(sig_src.encode()).hexdigest()

    tagged: list[TaggedDevice] = []
    for b in bikes:
        device_id = str(b.get("bike_id") or b.get("id") or "")
        if not device_id:
            continue
        try:
            lat = float(b["lat"])
            lon = float(b["lon"])
        except (TypeError, KeyError, ValueError):
            continue

        if cfg.denver_core.contains(lat, lon):
            status = "denver_core"
        elif cfg.china_glitch.contains(lat, lon):
            status = "china_glitch"
        else:
            status = "other_outlier"

        vt_id = b.get("vehicle_type_id")
        vt_key = str(vt_id) if vt_id is not None else None
        form_factor = "unknown"
        propulsion_type: str | None = None
        max_range_for_type: int | None = None
        if vt_key and vt_key in vt_map:
            vt = vt_map[vt_key]
            form_factor = vt.form_factor
            propulsion_type = vt.propulsion_type
            max_range_for_type = vt.max_range_meters
        elif b.get("form_factor"):
            form_factor = str(b["form_factor"])

        # Ground-truth override + use_type/app_name, applied regardless of
        # whether the vt_map lookup above succeeded — a known-bad registry
        # entry (id=4) should be corrected even if the live
        # vehicle_types.json fetch fails and we fell back to the canonical
        # {"1": scooter, "3": bicycle} map.
        known = _KNOWN_VEHICLE_TYPES.get(vt_key) if vt_key else None
        vehicle_model_name: str | None = None
        vehicle_use_type: str | None = None
        if known is not None:
            if known.form_factor is not None:
                form_factor = known.form_factor
            vehicle_use_type = known.use_type
            vehicle_model_name = known.app_name
        else:
            vehicle_use_type = _use_type_for_form_factor(form_factor)

        # is_disabled / is_reserved: GBFS spec says both REQUIRED bools, but
        # be defensive — fall back to None rather than coercing missing.
        is_disabled = b.get("is_disabled")
        is_reserved = b.get("is_reserved")
        if not isinstance(is_disabled, bool):
            is_disabled = None
        if not isinstance(is_reserved, bool):
            is_reserved = None

        rng = b.get("current_range_meters")
        try:
            current_range_meters = int(rng) if rng is not None else None
        except (TypeError, ValueError):
            current_range_meters = None

        plate = _extract_vehicle_plate(b.get("rental_uris"))
        h3_8, h3_9, h3_10 = _h3_cells(lat, lon)
        tagged.append(
            TaggedDevice(
                device_id=device_id,
                vehicle_type_id=vt_key,
                form_factor=form_factor,
                lat=lat,
                lon=lon,
                spatial_status=status,
                vehicle_plate=plate,
                vehicle_identifier=hash_plate(plate),
                is_disabled=is_disabled,
                is_reserved=is_reserved,
                current_range_meters=current_range_meters,
                propulsion_type=propulsion_type,
                max_range_meters_for_type=max_range_for_type,
                h3_8_index=h3_8,
                h3_9_index=h3_9,
                h3_10_index=h3_10,
                vehicle_use_type=vehicle_use_type,
                vehicle_model_name=vehicle_model_name,
            )
        )

    return IngestPayload(
        last_updated=int(last_updated) if last_updated is not None else None,
        payload_sha256=sha,
        devices=tagged,
        raw_count=len(bikes),
    )
