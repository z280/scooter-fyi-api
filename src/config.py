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
    # Public HTTPS base URL for R2_BUCKET_NAME once r2.dev access or a
    # custom domain is enabled for it (see src/device_photos.py) — that's
    # a one-time Cloudflare Dashboard step, not something this repo can
    # configure itself, and one that exposes every other prefix in the
    # bucket too. Optional: while it is None, device photo URLs are
    # presigned per response instead. Set it to get cacheable, permanent
    # URLs from a bucket that holds nothing but public objects.
    public_base_url: str | None = None

    def endpoint_url(self, account_id: str) -> str:
        return self.endpoint_template.format(account_id=account_id)


@dataclass(frozen=True)
class RouteProfile:
    """One rider-selectable routing profile.

    ``costing_options`` is passed straight through to Valhalla's ``bicycle``
    costing block, so the knobs live in config.json and can be retuned in
    production without rebuilding the image. ``rerank_by_shade`` turns on the
    alternates + canopy-scoring pass in api_route: Valhalla exposes no
    request-tunable shade lever, so shade is scored outside the graph.

    ``rerank_by_elevation`` exists for the same reason, and is not redundant
    with Valhalla's ``use_hills``: that knob is INERT on this graph. Measured
    on the live tiles across its whole 0.0-1.0 range, on five Denver pairs with
    up to 77 m of climb, it returns a byte-identical shape every time, while
    ``use_roads`` and ``bicycle_type`` visibly change the route in the same
    request. So hills, like shade, have to be ranked outside the graph.
    """
    key: str
    label: str
    costing_options: dict[str, Any]
    rerank_by_shade: bool = False
    rerank_by_elevation: bool = False
    alternates: int = 0


@dataclass(frozen=True)
class ValhallaConfig:
    base_url: str
    timeout_seconds: float
    # Bounding box of the routing graph as built by denver-map-prep. Requests
    # outside it are rejected up front rather than surfacing a raw Valhalla 400.
    bbox_west: float
    bbox_south: float
    bbox_east: float
    bbox_north: float
    # Metres between elevation samples along the returned shape; this is what
    # yields the elevation_gain the battery model regresses on.
    elevation_interval: int
    # Retry radius (metres) when a location fails to snap — HIN ways are
    # bicycle=no in the graph, so an address fronting an arterial can have no
    # routable edge within the default search radius.
    retry_radius_meters: int
    default_profile: str
    profiles: tuple[RouteProfile, ...]
    # Where the sidecar drops the routing assets (shared volume with Valhalla).
    custom_files_dir: str
    map_object_key: str
    canopy_object_key: str

    def contains(self, lat: float, lon: float) -> bool:
        return (self.bbox_south <= lat <= self.bbox_north
                and self.bbox_west <= lon <= self.bbox_east)

    @property
    def bbox(self) -> list[float]:
        return [self.bbox_west, self.bbox_south, self.bbox_east, self.bbox_north]

    def profile(self, key: str) -> RouteProfile | None:
        for p in self.profiles:
            if p.key == key:
                return p
        return None


@dataclass(frozen=True)
class GeocodeConfig:
    """The self-hosted Photon sidecar behind /api/v1/geocode/search.

    `upstream` is swappable by config alone: the proxy normalizes and
    rate-limits, so pointing it at a hosted geocoder is a config change rather
    than a code change. `enabled: false` makes the endpoint 503 with the same
    `geocoder_unavailable` a dead sidecar produces — the client's degraded path
    is already that one, so an operator can turn geocoding off without
    shipping a frontend.
    """
    upstream: str
    enabled: bool


@dataclass(frozen=True)
class AuthConfig:
    allowed_github_orgs: tuple[str, ...]
    callback_url: str


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
class AccountsConfig:
    # Template for the emailed magic-link sign-in URL; "{token}" is
    # substituted with the single-use token. Non-secret, so it lives here
    # rather than env-only — the env var MAGIC_LINK_URL_TEMPLATE still
    # overrides when set to a non-empty value (staging), but this is the
    # default so a blank/missing env var can't ship a linkless email.
    magic_link_url_template: str


@dataclass(frozen=True)
class PricingConfig:
    """What GET /api/v1/meta/pricing publishes for Ride Mode's cost breakdown.

    `tax_rate` is a FRACTION (0.0915), never a percentage (9.15) — a
    hundredfold tax produces no error, just a wrong number in front of a
    rider, so api_meta refuses an out-of-range value and serves its default.
    `as_of` is the rate's EFFECTIVE date, not a build date. Veo's rate PLANS
    stay client-side; only the legal tax rate lives here, because it changes on
    a city council's schedule rather than a deploy's.
    """
    tax_rate: float
    currency: str
    as_of: str


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
    valhalla: ValhallaConfig
    geocode: GeocodeConfig
    auth: AuthConfig
    accounts: AccountsConfig
    pricing: PricingConfig
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


def _valhalla(raw: dict[str, Any]) -> ValhallaConfig:
    bbox = raw.get("graph_bbox", {})
    profiles = tuple(
        RouteProfile(
            key=p["key"],
            label=p["label"],
            costing_options=dict(p.get("costing_options", {})),
            rerank_by_shade=bool(p.get("rerank_by_shade", False)),
            rerank_by_elevation=bool(p.get("rerank_by_elevation", False)),
            alternates=int(p.get("alternates", 0)),
        )
        for p in raw.get("profiles", [])
    )
    return ValhallaConfig(
        base_url=raw.get("base_url", "http://valhalla:8002"),
        timeout_seconds=float(raw.get("timeout_seconds", 10.0)),
        # Defaults mirror denver-map-prep/src/denver_map_prep/config.py. Keep
        # them in sync when that pipeline's clip window changes.
        bbox_west=float(bbox.get("west", -105.060)),
        bbox_south=float(bbox.get("south", 39.650)),
        bbox_east=float(bbox.get("east", -104.880)),
        bbox_north=float(bbox.get("north", 39.790)),
        elevation_interval=int(raw.get("elevation_interval", 30)),
        retry_radius_meters=int(raw.get("retry_radius_meters", 100)),
        default_profile=raw.get("default_profile", "safe"),
        profiles=profiles,
        custom_files_dir=raw.get("custom_files_dir", "/custom_files"),
        map_object_key=raw.get("map_object_key", "denver_scooter_custom.pbf"),
        canopy_object_key=raw.get("canopy_object_key", "denver_canopy_coverage.csv.gz"),
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
        valhalla=_valhalla(raw.get("valhalla", {})),
        # Defaults to the compose service name and enabled, so a config.json
        # that predates the block still boots and still reaches the sidecar.
        geocode=GeocodeConfig(
            upstream=(raw.get("geocode", {}).get("upstream")
                      or "http://photon:2322"),
            enabled=bool(raw.get("geocode", {}).get("enabled", True)),
        ),
        auth=AuthConfig(
            allowed_github_orgs=tuple(raw["auth"]["allowed_github_orgs"]),
            callback_url=raw["auth"]["callback_url"],
        ),
        accounts=AccountsConfig(
            magic_link_url_template=(
                raw.get("accounts", {}).get(
                    "magic_link_url_template", "https://denver.scooter.fyi/auth?ml={token}"
                )
            ),
        ),
        # Denver's combined rate as of 2025-01-01 (2.90 state + 1.00 RTD +
        # 0.10 SCFD + 5.15 city). api_meta validates it and falls back to the
        # same number, so a missing block and a nonsense one behave alike.
        pricing=PricingConfig(
            tax_rate=float(raw.get("pricing", {}).get("tax_rate", 0.0915)),
            currency=raw.get("pricing", {}).get("currency", "USD"),
            as_of=raw.get("pricing", {}).get("as_of", "2025-01-01"),
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


def r2_map_credentials() -> dict[str, str] | None:
    """R2 credentials for the routing-asset bucket that denver-map-prep writes.

    A DEDICATED token is required in practice. The archive token
    (R2_ACCESS_KEY_ID) is scoped to veo-audit's own buckets and returns 401 on
    denver-street-optimized-data — verified against the live bucket, not
    assumed. So R2_MAP_ACCESS_KEY_ID / R2_MAP_SECRET_ACCESS_KEY are read first
    and the archive credentials are only a fallback, which keeps a single
    account-wide token workable if one is ever issued.
    """
    account_id = os.environ.get("R2_ACCOUNT_ID")
    bucket = os.environ.get("R2_MAP_BUCKET")
    if not account_id or not bucket:
        return None
    access_key = (os.environ.get("R2_MAP_ACCESS_KEY_ID")
                  or os.environ.get("R2_ACCESS_KEY_ID"))
    secret_key = (os.environ.get("R2_MAP_SECRET_ACCESS_KEY")
                  or os.environ.get("R2_SECRET_ACCESS_KEY"))
    if not access_key or not secret_key:
        return None
    return {
        "account_id": account_id,
        "access_key_id": access_key,
        "secret_access_key": secret_key,
        "bucket": bucket,
    }


def oidc_credentials() -> dict[str, str] | None:
    cid = os.environ.get("OIDC_CLIENT_ID")
    cs = os.environ.get("OIDC_CLIENT_SECRET")
    if not cid or not cs:
        return None
    return {"client_id": cid, "client_secret": cs}


def allowed_github_orgs() -> tuple[str, ...]:
    env = os.environ.get("AUTH_ALLOWED_GITHUB_ORGS", "")
    env_orgs = tuple(o.strip() for o in env.split(",") if o.strip())
    return env_orgs or load().auth.allowed_github_orgs


def session_secret() -> str:
    return os.environ.get("SESSION_SECRET", "dev-only-do-not-use-in-prod")


def session_https_only() -> bool:
    """Whether the session cookie should have the Secure flag.

    Default True (production runs behind Cloudflare Tunnel = HTTPS).
    Set SESSION_HTTPS_ONLY=false locally if you need to test the OAuth
    flow over plain HTTP.
    """
    return os.environ.get("SESSION_HTTPS_ONLY", "true").lower() not in ("false", "0", "no")
