"""GBFS fetch, freshness check, and Denver-envelope tagging.

Data is never dropped — outliers are tagged so the admin panel can still
see how many devices were reporting from China / Null Island / etc.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .config import load
from .pg import connection

log = logging.getLogger(__name__)


class UpstreamError(Exception):
    """Wrap any failure that should land in api_failures."""

    def __init__(self, kind: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.kind = kind  # 'unavailable' | 'timeout' | 'malformed_payload'
        self.http_status = http_status


@dataclass(frozen=True)
class TaggedDevice:
    device_id: str
    vehicle_type_id: str | None
    form_factor: str
    lat: float
    lon: float
    spatial_status: str  # denver_core | china_glitch | other_outlier


@dataclass(frozen=True)
class IngestPayload:
    last_updated: int | None
    payload_sha256: str
    devices: list[TaggedDevice]
    raw_count: int


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------
def fetch_gbfs() -> tuple[dict[str, Any], dict[str, str]]:
    """Fetch free_bike_status + vehicle_types in a single client session.

    Returns (free_bike_status_json, {vehicle_type_id: form_factor}).
    The vehicle_types lookup is best-effort: failure falls back to the
    canonical {"1": "scooter", "3": "bicycle"} mapping.
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

        vt_map: dict[str, str] = {"1": "scooter", "3": "bicycle"}
        try:
            vt_resp = client.get(cfg.vehicle_types_url)
            if vt_resp.status_code < 400:
                vt_data = vt_resp.json().get("data", {}).get("vehicle_types", [])
                for vt in vt_data:
                    vid = str(vt.get("vehicle_type_id"))
                    ff = vt.get("form_factor")
                    if vid and ff:
                        vt_map[vid] = ff
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
def tag_envelope(payload: dict[str, Any], vt_map: dict[str, str]) -> IngestPayload:
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
        if vt_key and vt_key in vt_map:
            form_factor = vt_map[vt_key]
        elif b.get("form_factor"):
            form_factor = str(b["form_factor"])

        tagged.append(
            TaggedDevice(
                device_id=device_id,
                vehicle_type_id=vt_key,
                form_factor=form_factor,
                lat=lat,
                lon=lon,
                spatial_status=status,
            )
        )

    return IngestPayload(
        last_updated=int(last_updated) if last_updated is not None else None,
        payload_sha256=sha,
        devices=tagged,
        raw_count=len(bikes),
    )
