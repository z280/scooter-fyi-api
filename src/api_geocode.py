"""Address search for Ride Mode: GET /api/v1/geocode/search.

Fronts the self-hosted **Photon** sidecar (`docker/photon/Dockerfile`), which
serves a Colorado-scoped index seeded from R2 by `src.cli fetch_photon_index`.
Photon runs expose-only on the compose network exactly like Valhalla: riders
reach it only through this endpoint, so the rate limit, the bbox filter and the
result shape are all enforced in one place and the upstream stays swappable by
config alone (`config.json` -> `"geocode": {"upstream": ..., "enabled": ...}`).

Three things this proxy does that a raw Photon passthrough would not:

* **Denver bbox filter.** Photon's `bbox` param takes `minLon,minLat,maxLon,
  maxLat` and is filled from the config `envelope.denver_core` bounds —
  deliberately WIDER than the routing `graph_bbox`. Filtering on `graph_bbox`
  itself would make every returned hit in-coverage and `in_coverage` below
  vacuous.
* **`in_coverage`**, which is membership in the routing `graph_bbox`
  (`load().valhalla.contains`, the same test `/api/v1/route` rejects on). The
  wizard greys out un-routable picks instead of letting Screen 4 fail.
* **A normalized, small result shape** — `label`/`lat`/`lon`/`kind`/
  `in_coverage`. Photon's GeoJSON carries ~15 properties per hit and its
  classification vocabulary is an implementation detail of the index build.

Failure is a clean 503 `{"error": "geocoder_unavailable"}` on every path
(timeout, connection refused, upstream error, disabled by config) — the client
degrades to "type an address, no suggestions" rather than blocking the ride.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from . import config as config_module
from .client_ip import real_client_ip
from .config import load
from .pg import connection
from .ratelimit import enforce

log = logging.getLogger(__name__)

router = APIRouter()

# Query parameter bounds (PLAN_RIDE_MODE_API.md, Phase A1).
Q_MIN_LEN = 2
Q_MAX_LEN = 100
MAX_LIMIT = 8
DEFAULT_LIMIT = 6

# Per-IP rate limit, as (limit, window_seconds). Bucket `geocode_ip`.
# 20/min is roughly four keystroke-debounced lookups per minute per rider with
# headroom; the in-process cache below absorbs repeated prefixes for free.
_LIMIT_GEOCODE_PER_IP = (20, 60)

# Photon is a JVM sidecar on the same compose network. 3s is generous for a
# prefix query against a Colorado-scoped index and short enough that a wedged
# or restarting container never holds the wizard's address field hostage.
SIDECAR_TIMEOUT_SECONDS = 3.0

# Used when config.json carries no "geocode" block at all — the compose service
# name, so a stock deployment works unconfigured.
_DEFAULT_UPSTREAM = "http://photon:2322"

CACHE_MAX_ENTRIES = 512
CACHE_TTL_SECONDS = 24 * 3600


# --- config ------------------------------------------------------------------

@lru_cache(maxsize=1)
def _raw_geocode_block() -> dict[str, Any]:
    """The `"geocode"` block straight out of config.json.

    `src/config.py` is expected to grow a typed `geocode` block; until then
    (and if a deployment's config.py ever lags its config.json) this reads the
    raw JSON so `enabled: false` is a real kill switch either way rather than
    silently ignored. Cached like `config.load()` — config.json is only read at
    boot in this codebase and is mounted read-only.
    """
    try:
        with open(config_module.CONFIG_PATH) as fh:
            block = json.load(fh).get("geocode")
    except (OSError, ValueError) as exc:  # noqa: BLE001
        log.warning("could not read the geocode config block from %s: %s",
                    config_module.CONFIG_PATH, exc)
        return {}
    return dict(block) if isinstance(block, dict) else {}


def geocode_settings() -> tuple[str, bool]:
    """`(upstream, enabled)` for the geocoder."""
    block = getattr(load(), "geocode", None)
    if block is not None:
        return (str(getattr(block, "upstream", "") or _DEFAULT_UPSTREAM),
                bool(getattr(block, "enabled", True)))
    raw = _raw_geocode_block()
    return (str(raw.get("upstream") or _DEFAULT_UPSTREAM),
            bool(raw.get("enabled", True)))


def denver_core_bbox() -> str:
    """Photon's `bbox` filter value: `minLon,minLat,maxLon,maxLat`.

    NOT the routing graph_bbox — see the module docstring. Photon's parameter
    order is lon-first, unlike every lat/lon pair elsewhere in this codebase,
    which is exactly the kind of thing that silently returns zero results.
    """
    box = load().denver_core
    return (f"{box.lon_min:.6f},{box.lat_min:.6f},"
            f"{box.lon_max:.6f},{box.lat_max:.6f}")


# --- cache -------------------------------------------------------------------

class _TTLCache:
    """Tiny LRU + TTL cache. `functools.lru_cache` has no TTL, and a stale
    address suggestion should not outlive an index refresh by more than a day.

    Entries also record the `limit` they were fetched with, so a hit can never
    under-serve a later request that asked for more results than the cached
    entry holds: a too-small entry is treated as a miss and replaced. That
    keeps the cache key exactly what the plan specifies — (normalized q,
    round(lat,2), round(lon,2)), with no `limit` component — while staying
    correct for callers that vary `limit`.

    Locked because uvicorn runs sync endpoints in a thread pool, so two
    requests really do touch this concurrently.
    """

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES,
                 ttl_seconds: float = CACHE_TTL_SECONDS) -> None:
        self._max = max_entries
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        # key -> (expires_at_monotonic, fetched_limit, results)
        self._data: OrderedDict[tuple, tuple[float, int, list[dict]]] = OrderedDict()

    def get(self, key: tuple, limit: int) -> list[dict] | None:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, fetched_limit, results = entry
            if expires_at <= now:
                del self._data[key]
                return None
            if fetched_limit < limit and len(results) >= fetched_limit:
                # Might be truncated relative to what this caller wants.
                return None
            self._data.move_to_end(key)
            # Copy: callers must never mutate a cached row.
            return [dict(r) for r in results[:limit]]

    def put(self, key: tuple, limit: int, results: list[dict]) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + self._ttl, limit,
                               [dict(r) for r in results])
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:  # tests / introspection
        with self._lock:
            return len(self._data)


_CACHE = _TTLCache()


def cache_key(q: str, lat: float | None, lon: float | None) -> tuple:
    """(normalized q, round(lat,2), round(lon,2)).

    2 decimal places is ~1.1 km of bias granularity — far finer than the effect
    a location bias has on ranking, and coarse enough that a moving rider's
    keystrokes keep hitting the same entry.
    """
    return (q.casefold(),
            None if lat is None else round(lat, 2),
            None if lon is None else round(lon, 2))


# --- normalization -----------------------------------------------------------

# Photon's `properties.type` is the authoritative classification when present;
# this collapses its vocabulary onto the four kinds the client renders.
#
# Verified against the pinned jar (photon 1.2.1, `de.komoot.photon.searcher.
# GeoJsonFields` / `nominatim.model.AddressType`): the emitted GeoJSON property
# is `type`, and its complete value set is house | street | locality | district |
# city | county | state | country | other. "other" is deliberately absent from
# the map below — it carries no information and falls through to the osm_key
# ladder in `kind_for`. `postcode` is not in that enum either; it is kept below
# because the postcode rows take a different path through Photon and cost
# nothing to accept. `layer` is read as a fallback for a fork or a future build
# that renames the field — 1.2.1 uses `layer` only as a request FILTER, never as
# a response property, so on this jar the fallback never fires.
_LAYER_KIND = {
    "house": "house",
    "street": "street",
    "locality": "locality",
    "city": "locality",
    "district": "locality",
    "county": "locality",
    "state": "locality",
    "country": "locality",
    "postcode": "locality",
}

# osm_key values that describe a place or an administrative area, not a POI.
_LOCALITY_KEYS = {"place", "boundary"}

# osm_key values that are an address rather than a named thing.
_HOUSE_KEYS = {"building", "addr"}

# highway=* values that are objects sitting ON a street, not the street itself.
_HIGHWAY_POI_VALUES = {
    "bus_stop", "platform", "crossing", "traffic_signals", "street_lamp",
    "elevator", "services", "rest_area", "stop", "give_way", "turning_circle",
    "milestone", "speed_camera",
}


def _clean(value: Any) -> str:
    return " ".join(str(value).split()) if value not in (None, "") else ""


def kind_for(props: dict[str, Any]) -> str:
    """Map one Photon feature's properties onto house|street|poi|locality.

    Ladder, most authoritative first:
      1. `type` / `layer`, when Photon supplies a value we recognise;
      2. `place=*` / `boundary=*` -> locality (`place=house` -> house);
      3. `highway=*` -> street, unless the value is a stop/crossing/etc.;
      4. `building=*` / `addr:*`, or an unnamed hit carrying a housenumber
         -> house (a POI has a name; a plain address does not);
      5. anything else -> poi.
    """
    declared = _clean(props.get("type") or props.get("layer")).lower()
    mapped = _LAYER_KIND.get(declared)
    if mapped:
        return mapped

    osm_key = _clean(props.get("osm_key")).lower()
    osm_value = _clean(props.get("osm_value")).lower()

    if osm_key in _LOCALITY_KEYS:
        return "house" if osm_value == "house" else "locality"
    if osm_key == "highway":
        return "poi" if osm_value in _HIGHWAY_POI_VALUES else "street"
    if osm_key in _HOUSE_KEYS:
        return "house"
    if props.get("housenumber") and not props.get("name"):
        return "house"
    return "poi"


def label_for(props: dict[str, Any], kind: str) -> str:
    """One human line per hit — "1701 Champa St, Denver".

    Composed rather than taken from a single Photon field because no single
    field reads naturally: an address has no `name`, a POI's `name` alone is
    ambiguous across a metro, and a locality wants its state.
    """
    name = _clean(props.get("name"))
    housenumber = _clean(props.get("housenumber"))
    street = _clean(props.get("street"))
    city = _clean(props.get("city")) or _clean(props.get("district"))
    state = _clean(props.get("state"))
    country = _clean(props.get("country"))
    postcode = _clean(props.get("postcode"))

    parts: list[str] = []
    street_line = " ".join(p for p in (housenumber, street) if p)
    if name:
        parts.append(name)
    if street_line and street_line != name:
        parts.append(street_line)

    if kind == "locality":
        # "Denver, Colorado": a bare city name is ambiguous, and localities are
        # exactly the rows a rider scans for disambiguation.
        for candidate in (city, state):
            if candidate and candidate not in parts:
                parts.append(candidate)
    else:
        # One locality qualifier is enough for an address or a POI; the whole
        # result set is inside the Denver envelope already.
        for candidate in (city, state, country):
            if candidate and candidate not in parts:
                parts.append(candidate)
                break

    if not parts:
        for candidate in (state, country, postcode):
            if candidate:
                parts.append(candidate)
                break
    return ", ".join(parts)


def normalize_results(payload: Any, limit: int) -> list[dict[str, Any]]:
    """Photon GeoJSON -> the endpoint's `results` list.

    Skips anything unusable (no coordinates, nothing to label) rather than
    surfacing a blank row: the client renders this list directly.
    """
    features = payload.get("features") if isinstance(payload, dict) else None
    graph = load().valhalla
    out: list[dict[str, Any]] = []

    for feat in features or []:
        if not isinstance(feat, dict):
            continue
        coords = ((feat.get("geometry") or {}) if isinstance(feat.get("geometry"), dict)
                  else {}).get("coordinates") or []
        if not isinstance(coords, (list, tuple)) or len(coords) < 2:
            continue
        try:
            # GeoJSON is [lon, lat] — the reverse of every other pair here.
            lon = round(float(coords[0]), 6)
            lat = round(float(coords[1]), 6)
        except (TypeError, ValueError):
            continue
        props = feat.get("properties")
        props = props if isinstance(props, dict) else {}
        kind = kind_for(props)
        label = label_for(props, kind)
        if not label:
            continue
        out.append({
            "label": label,
            "lat": lat,
            "lon": lon,
            "kind": kind,
            # Computed from the rounded values actually returned, so the flag
            # can never disagree with the coordinate the client routes on.
            "in_coverage": bool(graph.contains(lat, lon)),
        })
        if len(out) >= limit:
            break
    return out


# --- upstream ----------------------------------------------------------------

def _unavailable() -> HTTPException:
    return HTTPException(503, {"error": "geocoder_unavailable"})


def query_photon(upstream: str, q: str, lat: float | None, lon: float | None,
                 limit: int) -> list[dict[str, Any]]:
    """GET {upstream}/api and normalize, or raise the 503."""
    params: dict[str, Any] = {
        "q": q,
        "limit": limit,
        "bbox": denver_core_bbox(),
        # Pinned rather than inherited from the caller's Accept-Language: the
        # index is imported with English names and a per-request language would
        # make the cache key wrong.
        "lang": "en",
    }
    if lat is not None and lon is not None:
        # Rounded to the cache key's precision so a cached response is exactly
        # what any key-equal request would have received.
        params["lat"] = round(lat, 2)
        params["lon"] = round(lon, 2)

    url = f"{upstream.rstrip('/')}/api"
    try:
        resp = httpx.get(url, params=params, timeout=SIDECAR_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        log.warning("geocoder unreachable at %s: %s", url, exc)
        raise _unavailable() from exc

    if resp.status_code >= 400:
        # Includes Photon's own 400s: we validate `q` before getting here, so a
        # 4xx means this proxy built a request the sidecar rejected. Either way
        # the rider cannot fix it and the honest answer is "unavailable".
        log.error("geocoder returned HTTP %d for q=%r: %s",
                  resp.status_code, q, resp.text[:300])
        raise _unavailable()

    try:
        payload = resp.json()
    except ValueError as exc:
        log.error("geocoder returned non-JSON for q=%r: %s", q, resp.text[:200])
        raise _unavailable() from exc

    return normalize_results(payload, limit)


# --- endpoint ----------------------------------------------------------------

@router.get("/api/v1/geocode/search")
def geocode_search(
    request: Request,
    q: Annotated[str, Query(min_length=Q_MIN_LEN, max_length=Q_MAX_LEN,
                            description="Free-text address or place query")],
    lat: Annotated[float | None, Query(ge=-90, le=90,
                                       description="Optional bias latitude")] = None,
    lon: Annotated[float | None, Query(ge=-180, le=180,
                                       description="Optional bias longitude")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Public address search, Denver-scoped.

    Public on purpose: the wizard's address field runs before any sign-in
    prompt, and nothing here is account-specific. The per-IP bucket is the
    whole abuse control.
    """
    upstream, enabled = geocode_settings()
    if not enabled:
        # Same 503 as a dead sidecar: the client's degraded path is identical,
        # so an operator can turn the geocoder off without shipping a client.
        log.info("geocode search refused: geocoding disabled in config")
        raise _unavailable()

    q_norm = " ".join(q.split())
    if len(q_norm) < Q_MIN_LEN:
        # `min_length` counts raw characters, so "  " reaches here; querying
        # Photon with an empty q is a 400 from the sidecar.
        raise HTTPException(422, {"error": "bad_query",
                                  "detail": f"q must be {Q_MIN_LEN}-{Q_MAX_LEN} characters"})
    if (lat is None) != (lon is None):
        raise HTTPException(400, {"error": "bad_bias",
                                  "detail": "lat and lon must be supplied together"})

    # Counted before the cache is consulted: the bucket exists to bound load
    # from one caller, and a cache hit is still a request.
    ip = real_client_ip(request)
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="geocode_ip", key=ip or "?",
                    limit=_LIMIT_GEOCODE_PER_IP[0],
                    window_seconds=_LIMIT_GEOCODE_PER_IP[1])
        conn.commit()

    key = cache_key(q_norm, lat, lon)
    hit = _CACHE.get(key, limit)
    if hit is not None:
        return {"results": hit}

    results = query_photon(upstream, q_norm, lat, lon, limit)
    _CACHE.put(key, limit, results)
    return {"results": results[:limit]}
