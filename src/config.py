"""Single source of truth for runtime configuration.

Non-secret values come from ``config.json`` (path via ``$VEO_CONFIG``).
Secrets ALWAYS come from environment variables — never the JSON file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


CONFIG_PATH = os.environ.get("VEO_CONFIG", "/app/config.json")


@dataclass(frozen=True)
class GBFSConfig:
    url: str
    vehicle_types_url: str
    timeout_seconds: int
    user_agent: str


@dataclass(frozen=True)
class ScheduleConfig:
    cycle_minutes: int
    archive_hours: int


@dataclass(frozen=True)
class EnvelopeBox:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def contains(self, lat: float, lon: float) -> bool:
        return self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max


@dataclass(frozen=True)
class BoundaryLayer:
    region_category: str
    region_type: str
    file: str
    name_prefix: str
    name_strategy: str          # 'ordinal' | 'field' | 'field_alnum'
    name_field: str | None
    filter_nonnull_field: str | None = None


@dataclass(frozen=True)
class TransmissionEndpoint:
    name: str
    url: str
    method: str
    path: str
    auth_env: str | None = None


@dataclass(frozen=True)
class R2Config:
    bucket_name: str
    endpoint_template: str

    def endpoint_url(self, account_id: str) -> str:
        return self.endpoint_template.format(account_id=account_id)


@dataclass(frozen=True)
class AuthConfig:
    allowed_github_orgs: tuple[str, ...]
    callback_url: str


@dataclass(frozen=True)
class MapAuthConfig:
    """Auth for the elevated-map flow — distinct from admin-panel auth.

    Uses a SEPARATE GitHub OAuth app (different client_id/secret), so the
    set of users who can read non-anonymized device data is decoupled from
    the set who can manage cron/replay snapshots.
    """
    allowed_github_orgs: tuple[str, ...]   # e.g. ("scooter-club",)
    callback_url: str                       # e.g. https://data.scooter.fyi/map-auth/callback
    allowed_return_origins: tuple[str, ...] # exact-match origins for the return= parameter
    token_ttl_hours: int                    # 8 by default


@dataclass(frozen=True)
class DeviceTrackingConfig:
    # Distance threshold (meters) below which a scooter is considered to have
    # not moved between cycles. Below this, a device_id rotation counts as a
    # "failed start"; above it, we mint a new device_history row. 16m is
    # deliberately just outside typical GPS jitter on a stationary device
    # (5–15m), so a parked scooter doesn't accumulate spurious "movements"
    # from GPS noise.
    stationary_threshold_meters: float


@dataclass(frozen=True)
class SpatialConfig:
    # Distance (meters) the actual Denver city polygon is buffered outward
    # before deciding denver_core membership. Veo lets riders start some
    # vehicles from just over the city line; a small buffer keeps those in
    # the dataset (and, deliberately, in the compliance denominator) instead
    # of tagging them other_outlier. 0 disables the buffer entirely (exact
    # inside-the-polygon behavior). See compute._refine_spatial_status.
    denver_core_buffer_meters: float


@dataclass(frozen=True)
class AppConfig:
    gbfs: GBFSConfig
    schedule: ScheduleConfig
    denver_core: EnvelopeBox
    china_glitch: EnvelopeBox
    boundaries: tuple[BoundaryLayer, ...]
    transmission_endpoints: tuple[TransmissionEndpoint, ...]
    cors_origins: tuple[str, ...]
    cors_origin_patterns: tuple[str, ...]
    r2: R2Config
    auth: AuthConfig
    map_auth: MapAuthConfig
    device_tracking: DeviceTrackingConfig
    spatial: SpatialConfig
    log_level: str


def _envelope(raw: dict[str, Any]) -> EnvelopeBox:
    lat = raw["lat"]
    lon = raw["lon"]
    return EnvelopeBox(lat_min=lat[0], lat_max=lat[1], lon_min=lon[0], lon_max=lon[1])


def _boundaries(raw: list[dict[str, Any]]) -> tuple[BoundaryLayer, ...]:
    return tuple(
        BoundaryLayer(
            region_category=b["region_category"],
            region_type=b["region_type"],
            file=b["file"],
            name_prefix=b["name_prefix"],
            name_strategy=b["name_strategy"],
            name_field=b.get("name_field"),
            filter_nonnull_field=b.get("filter_nonnull_field"),
        )
        for b in raw
    )


def _endpoints(raw: list[dict[str, Any]]) -> tuple[TransmissionEndpoint, ...]:
    return tuple(
        TransmissionEndpoint(
            name=e["name"],
            url=e["url"],
            method=e.get("method", "POST"),
            path=e.get("path", ""),
            auth_env=e.get("auth_env"),
        )
        for e in raw
    )


@lru_cache(maxsize=1)
def load() -> AppConfig:
    with open(CONFIG_PATH) as f:
        raw = json.load(f)

    return AppConfig(
        gbfs=GBFSConfig(**raw["gbfs"]),
        schedule=ScheduleConfig(**raw["schedule"]),
        denver_core=_envelope(raw["envelope"]["denver_core"]),
        china_glitch=_envelope(raw["envelope"]["china_glitch"]),
        boundaries=_boundaries(raw["boundaries"]),
        transmission_endpoints=_endpoints(raw["transmission"]["endpoints"]),
        cors_origins=tuple(raw["cors"]["allowed_origins"]),
        cors_origin_patterns=tuple(raw["cors"].get("allowed_origin_patterns", [])),
        r2=R2Config(**raw["r2"]),
        auth=AuthConfig(
            allowed_github_orgs=tuple(raw["auth"]["allowed_github_orgs"]),
            callback_url=raw["auth"]["callback_url"],
        ),
        map_auth=MapAuthConfig(
            allowed_github_orgs=tuple(raw.get("map_auth", {}).get("allowed_github_orgs", ["scooter-club"])),
            callback_url=raw.get("map_auth", {}).get(
                "callback_url", "https://data.scooter.fyi/map-auth/callback"
            ),
            allowed_return_origins=tuple(raw.get("map_auth", {}).get("allowed_return_origins", [])),
            token_ttl_hours=int(raw.get("map_auth", {}).get("token_ttl_hours", 8)),
        ),
        device_tracking=DeviceTrackingConfig(
            stationary_threshold_meters=float(
                raw.get("device_tracking", {}).get("stationary_threshold_meters", 16.0)
            ),
        ),
        spatial=SpatialConfig(
            denver_core_buffer_meters=float(
                raw.get("spatial", {}).get("denver_core_buffer_meters", 200.0)
            ),
        ),
        log_level=raw.get("logging", {}).get("level", "INFO"),
    )


# Secrets (always env-only) ----------------------------------------------------
def pg_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    pwd = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_HOST", "denver_spatial_db")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ["POSTGRES_DB"]
    return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"


def sentry_dsn() -> str | None:
    return os.environ.get("SENTRY_DSN") or None


def r2_credentials() -> dict[str, str] | None:
    keys = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    if not all(os.environ.get(k) for k in keys):
        return None
    return {
        "account_id": os.environ["R2_ACCOUNT_ID"],
        "access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "bucket": os.environ.get("R2_BUCKET_NAME") or load().r2.bucket_name,
    }


def oidc_credentials() -> dict[str, str] | None:
    cid = os.environ.get("OIDC_CLIENT_ID")
    cs = os.environ.get("OIDC_CLIENT_SECRET")
    if not cid or not cs:
        return None
    return {"client_id": cid, "client_secret": cs}


def map_oidc_credentials() -> dict[str, str] | None:
    """Credentials for the SEPARATE OAuth app used by the elevated-map flow."""
    cid = os.environ.get("MAP_OIDC_CLIENT_ID")
    cs = os.environ.get("MAP_OIDC_CLIENT_SECRET")
    if not cid or not cs:
        return None
    return {"client_id": cid, "client_secret": cs}


def allowed_github_orgs() -> tuple[str, ...]:
    env = os.environ.get("AUTH_ALLOWED_GITHUB_ORGS", "")
    env_orgs = tuple(o.strip() for o in env.split(",") if o.strip())
    return env_orgs or load().auth.allowed_github_orgs


def allowed_map_github_orgs() -> tuple[str, ...]:
    env = os.environ.get("MAP_AUTH_ALLOWED_GITHUB_ORGS", "")
    env_orgs = tuple(o.strip() for o in env.split(",") if o.strip())
    return env_orgs or load().map_auth.allowed_github_orgs


def session_secret() -> str:
    return os.environ.get("SESSION_SECRET", "dev-only-do-not-use-in-prod")


def session_https_only() -> bool:
    """Whether the session cookie should have the Secure flag.

    Default True (production runs behind Cloudflare Tunnel = HTTPS).
    Set SESSION_HTTPS_ONLY=false locally if you need to test the OAuth
    flow over plain HTTP.
    """
    return os.environ.get("SESSION_HTTPS_ONLY", "true").lower() not in ("false", "0", "no")
