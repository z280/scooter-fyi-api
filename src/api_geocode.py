"""Address search for Ride Mode: GET /api/v1/geocode/search.

Fronts the self-hosted **Photon** sidecar (`docker/photon/Dockerfile`), which
serves a Colorado-scoped index seeded from R2 by `src.cli fetch_photon_index`.
Photon runs expose-only on the compose network exactly like Valhalla: riders
reach it only through this endpoint, so the rate limit, the bbox filter and the
result shape are all enforced in one place and the upstream stays swappable by
config alone (`config.json` -> `"geocode": {"upstream": ..., "enabled": ...}`).

Four things this proxy does that a raw Photon passthrough would not:

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
* **House-number-aware ranking** (`rank_for_housenumber_query`). Photon does no
  address interpolation, so most residential Denver addresses match nothing and
  the leftover hits are transit stops named after intersections. Left alone
  that turns "no such indexed address" into a confident pick on the wrong side
  of town; see that function for the full reasoning.

Failure is a clean 503 `{"error": "geocoder_unavailable"}` on every path
(timeout, connection refused, upstream error, disabled by config) — the client
degrades to "type an address, no suggestions" rather than blocking the ride.
"""

from __future__ import annotations

import json
import logging
import re
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

# osm_key values for things that SIT ON the street network rather than being an
# address on it: transit stops, platforms, rail infrastructure. Denver's transit
# stops are named after the intersection they serve ("E 10th Ave & Monaco Pkwy"),
# so they score highly on any street-name query and are the single biggest
# source of confidently-wrong address hits. See `demote_on_street_furniture`.
_ON_STREET_KEYS = {"railway", "public_transport", "aeroway"}

# Abbreviation expansion for the street fallback ONLY (`query_photon`).
#
# This is not cosmetic, it decides whether the fallback works at all. OSM names
# streets in full ("East 1st Avenue") and names transit stops in abbreviated
# form ("E 1st Ave & Garfield St"). Measured against the live index on the
# denver_core bbox: "E 1st Ave" returns 16 hits, ALL of them bus stops, and the
# street does not appear at any depth; "East 1st Avenue" returns 11 hits, ALL of
# them street segments. Riders type the abbreviated form.
#
# Applied only after an address lookup has already failed, so a wrong expansion
# costs a second-chance query rather than a good result.
_DIRECTIONALS = {
    "e": "East", "w": "West", "n": "North", "s": "South",
    "ne": "Northeast", "nw": "Northwest", "se": "Southeast", "sw": "Southwest",
}
_STREET_TYPES = {
    "ave": "Avenue", "av": "Avenue", "st": "Street", "blvd": "Boulevard",
    "rd": "Road", "dr": "Drive", "ln": "Lane", "ct": "Court", "pl": "Place",
    "pkwy": "Parkway", "pkw": "Parkway", "cir": "Circle", "ter": "Terrace",
    "hwy": "Highway", "sq": "Square", "trl": "Trail", "wy": "Way",
}

# Generic words a rider appends to a place that OSM names bare. Photon requires
# EVERY query term to match, so one extra word does not merely rank a hit lower,
# it removes it: measured on the live index, "Knox" returns 5 hits including the
# railway/station, and "Knox Station" returns ZERO. Same for Perry. The RTD W
# Line platforms are named "Knox", "Perry", "Sheridan" — no "Station" suffix —
# while riders type the suffix, because that is what the station is called.
#
# Stripped only when a query returned NOTHING (see `query_photon`), so a query
# that already works is never touched: "Union Station" keeps its suffix because
# the OSM name really does contain it, and it never reaches this path.
_GENERIC_PLACE_SUFFIXES = {"station", "stn", "stop", "platform"}

# A leading house number in a free-text query: "1226 E 10th Ave" -> "1226".
# Accepts a trailing letter ("221B") because OSM house numbers do. Anchored, so
# "10th Avenue" is NOT read as house number 10 — a bare street query must not be
# treated as an address lookup that then "fails" to find a number.
_LEADING_HOUSENUMBER_RE = re.compile(r"^(\d+[a-zA-Z]?)\b")


def _clean(value: Any) -> str:
    return " ".join(str(value).split()) if value not in (None, "") else ""


def is_on_street_furniture(osm_key: str, osm_value: str) -> bool:
    """Does this hit describe a thing standing ON a street, not an address?"""
    return osm_key in _ON_STREET_KEYS or (
        osm_key == "highway" and osm_value in _HIGHWAY_POI_VALUES)


def strip_generic_suffix(q: str) -> str | None:
    """"Knox Station" -> "Knox". None when there is nothing to strip.

    Requires something to survive: "Station" on its own is a real query for a
    place called Station and must not be reduced to nothing.
    """
    tokens = q.split()
    if len(tokens) < 2:
        return None
    if tokens[-1].strip(".,").casefold() not in _GENERIC_PLACE_SUFFIXES:
        return None
    return " ".join(tokens[:-1])


def expand_street_abbreviations(street: str) -> str:
    """"E 10th Ave" -> "East 10th Avenue", for the street fallback.

    Position-sensitive on purpose: a directional only expands at the front and
    a street type only at the end. Expanding "st" anywhere would turn
    "St Anne Ave" into "Street Anne Avenue"; expanding "e" anywhere would
    maul any street whose name contains a bare letter.
    """
    tokens = street.split()
    if not tokens:
        return street
    head = tokens[0].strip(".,").casefold()
    if head in _DIRECTIONALS:
        tokens[0] = _DIRECTIONALS[head]
    tail = tokens[-1].strip(".,").casefold()
    if len(tokens) > 1 and tail in _STREET_TYPES:
        tokens[-1] = _STREET_TYPES[tail]
    return " ".join(tokens)


def leading_housenumber(q: str) -> str | None:
    """The house number a query opens with, or None if it is not an address."""
    match = _LEADING_HOUSENUMBER_RE.match(q.strip())
    return match.group(1) if match else None


def street_of_query(q: str) -> str | None:
    """The street a house-numbered query names: "1226 E 10th Ave" -> "E 10th Ave".

    None when the query does not open with a house number, since there is then
    no house-number/street pair to check against each other.
    """
    match = _LEADING_HOUSENUMBER_RE.match(q.strip())
    if not match:
        return None
    rest = q.strip()[match.end():].strip(" ,")
    return rest or None


def streets_match(query_street: str | None, feature_street: str | None) -> bool:
    """Do these name the same street, allowing for abbreviation?

    Both sides are expanded ("E 10th Ave" -> "East 10th Avenue") before
    comparison, because the query is whatever the rider typed and Photon's
    `street` is whatever OSM holds. Unknown on either side is NOT a match: a
    feature that cannot say which street it is on has not earned promotion
    over one that can.
    """
    if not query_street or not feature_street:
        return False
    a = expand_street_abbreviations(query_street.strip()).casefold()
    b = expand_street_abbreviations(feature_street.strip()).casefold()
    return a == b


def rank_for_housenumber_query(features: list[Any],
                               housenumber: str,
                               street: str | None = None) -> list[Any]:
    """Reorder/trim Photon features for a query that named a house number.

    Photon has no address interpolation: it indexes discrete objects only, so a
    house number that exists in OSM *only* as an `addr:interpolation` range —
    which is most of residential Denver — matches nothing. What comes back
    instead is whatever else scored on the street name, and in Denver that is
    reliably a transit stop, because the stops are NAMED after intersections.
    "1226 E 10th Ave" returned "E 10th Ave & Monaco Pkwy", a stop roughly five
    miles east of the block asked for.

    That is worse than an empty result. The rider gets a plausible-looking pick,
    the wizard reports `in_coverage: true` because the stop really is inside the
    graph, and Screen 4 then routes them confidently to the wrong side of town.
    A missing suggestion is recoverable; a wrong one is not noticed.

    So:

    * an exact house-number match is promoted to the top ONLY IF IT IS ON THE
      STREET THE RIDER NAMED. Photon ranks on text score, so a same-street
      neighbour can outrank the exact number and needs promoting — but the
      number alone is not enough. Denver repeats house numbers across its
      numbered avenues, so "1226 East 10th Avenue" matched "1226 East 22nd
      Avenue" on the number and was promoted to the top: the right number,
      twelve blocks north, returned with total confidence. That is the exact
      failure this function was written to prevent, reintroduced through the
      promotion rule itself. A number match on the wrong street is now worth
      LESS than no match, and falls through to the street-level answer below;
    * failing any exact match, on-street furniture is dropped and the named
      street itself is what the rider is offered — "East 10th Avenue, Denver"
      is honest and lands on the right street, which is the most this index can
      truthfully say about an interpolated address.

    Only furniture is dropped. A genuine POI keeps its place: someone typing
    "1000 Chopper Cir" who gets "Altitude Athletics, 1000 Chopper Circle" has
    been served well, and a cafe matching the street name is a fair guess.
    """
    wanted = housenumber.casefold()
    exact, rest = [], []
    for feat in features:
        props = feat.get("properties") if isinstance(feat, dict) else None
        props = props if isinstance(props, dict) else {}
        number_matches = _clean(props.get("housenumber")).casefold() == wanted
        # When the rider named a street, the number must be ON it. When they
        # did not, the number is all we have to go on and stands alone.
        on_named_street = (
            streets_match(street, _clean(props.get("street")) or None)
            if street else True)
        if number_matches and on_named_street:
            exact.append(feat)
        else:
            rest.append(feat)

    if exact:
        return exact + rest

    kept = []
    for feat in rest:
        props = feat.get("properties") if isinstance(feat, dict) else None
        props = props if isinstance(props, dict) else {}
        if is_on_street_furniture(_clean(props.get("osm_key")).lower(),
                                  _clean(props.get("osm_value")).lower()):
            continue
        kept.append(feat)
    return kept


def kind_for(props: dict[str, Any]) -> str:
    """Map one Photon feature's properties onto house|street|poi|locality.

    Ladder, most authoritative first:
      0. on-street furniture (transit stops, platforms, rail) -> poi, ALWAYS;
      1. `type` / `layer`, when Photon supplies a value we recognise;
      2. `place=*` / `boundary=*` -> locality (`place=house` -> house);
      3. `highway=*` -> street, unless the value is a stop/crossing/etc.;
      4. `building=*` / `addr:*`, or an unnamed hit carrying a housenumber
         -> house (a POI has a name; a plain address does not);
      5. anything else -> poi.

    Rung 0 outranks Photon's own `type` because that field is the *address
    granularity* of a hit, not its object class: a bus stop that carries a full
    street address is emitted as `type: house`, so trusting `type` first
    labelled "E 10th Ave & Monaco Pkwy" (a stop on Monaco, miles from the 1226
    block) as a house. Photon is authoritative about how precisely a result is
    addressed; it is not authoritative about what the result IS.
    """
    osm_key = _clean(props.get("osm_key")).lower()
    osm_value = _clean(props.get("osm_value")).lower()

    if is_on_street_furniture(osm_key, osm_value):
        return "poi"

    declared = _clean(props.get("type") or props.get("layer")).lower()
    mapped = _LAYER_KIND.get(declared)
    if mapped:
        return mapped

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


def normalize_results(payload: Any, limit: int,
                      requested_housenumber: str | None = None) -> list[dict[str, Any]]:
    """Photon GeoJSON -> the endpoint's `results` list.

    Skips anything unusable (no coordinates, nothing to label) rather than
    surfacing a blank row: the client renders this list directly.

    SAYING WHAT WAS NOT MATCHED. Photon cannot interpolate, so a numbered
    query routinely lands on the street rather than the address, and the label
    alone cannot be told apart from a successful match: ask for
    "1226 E 10th Ave", get "East 10th Avenue, Denver", and nothing distinguishes
    "we found it and shortened the label" from "we dropped your number and
    picked a point somewhere along five miles of avenue". Those have very
    different consequences for a rider.

    So every result of a numbered query carries `requested_housenumber` and
    `matched_housenumber`, and a street-level answer says in its own label
    that the number is missing. The structured pair is the contract; the label
    suffix is a sensible default for any client that just renders the string.
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
        matched_number = bool(
            requested_housenumber
            and _clean(props.get("housenumber")).casefold()
            == requested_housenumber.casefold())
        if requested_housenumber and not matched_number:
            # Only worth saying on a street: a POI or locality was never
            # claiming to be the address in the first place.
            if kind == "street":
                label = f"{label} (no number {requested_housenumber} in map data)"
        out.append({
            "label": label,
            "lat": lat,
            "lon": lon,
            "kind": kind,
            # Echoed so a client can render "1226" struck through, greyed, or
            # as a warning without re-parsing the query it just sent.
            "requested_housenumber": requested_housenumber,
            "matched_housenumber": matched_number if requested_housenumber else None,
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


def _photon_params(q: str, lat: float | None, lon: float | None,
                   limit: int) -> dict[str, Any]:
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
    return params


def _fetch(upstream: str, q: str, params: dict[str, Any]) -> Any:
    """One GET against the sidecar, or the 503."""
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
        return resp.json()
    except ValueError as exc:
        log.error("geocoder returned non-JSON for q=%r: %s", q, resp.text[:200])
        raise _unavailable() from exc


def _features(payload: Any) -> list[Any]:
    feats = payload.get("features") if isinstance(payload, dict) else None
    return feats if isinstance(feats, list) else []


def drop_on_street_furniture(features: list[Any]) -> list[Any]:
    """Remove transit stops and the like, keeping everything else in order."""
    kept = []
    for feat in features:
        props = feat.get("properties") if isinstance(feat, dict) else None
        props = props if isinstance(props, dict) else {}
        if is_on_street_furniture(_clean(props.get("osm_key")).lower(),
                                  _clean(props.get("osm_value")).lower()):
            continue
        kept.append(feat)
    return kept


def dedupe_by_label(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse rows that would render identically.

    A named street is many OSM ways, so the street fallback below otherwise
    returns "East 10th Avenue, Denver" four times with four different
    coordinates — which reads as four choices when it is really one.
    """
    seen, out = set(), []
    for row in results:
        if row["label"] in seen:
            continue
        seen.add(row["label"])
        out.append(row)
    return out


def query_photon(upstream: str, q: str, lat: float | None, lon: float | None,
                 limit: int) -> list[dict[str, Any]]:
    """GET {upstream}/api and normalize, or raise the 503."""
    housenumber = leading_housenumber(q)
    if not housenumber:
        results = normalize_results(
            _fetch(upstream, q, _photon_params(q, lat, lon, limit)), limit)
        if results:
            return results
        # Nothing matched. Photon ANDs its terms, so one word a rider added out
        # of habit can zero out a place that is indexed perfectly well —
        # "Knox Station" finds nothing while "Knox" finds the railway/station.
        # Retry once without that word. Gated on an empty result set, so a
        # query that already works is never rewritten.
        shorter = strip_generic_suffix(q)
        if shorter:
            log.info("geocode: %r matched nothing; retrying as %r", q, shorter)
            return normalize_results(
                _fetch(upstream, shorter, _photon_params(shorter, lat, lon, limit)),
                limit)
        return results

    # Over-fetch for an address query: `rank_for_housenumber_query` can drop
    # hits, and asking Photon for exactly `limit` would let that filtering
    # starve the list the rider actually sees. Costs nothing — the sidecar is
    # on the same compose network and the extra rows are trimmed below.
    fetch_limit = min(limit + 4, MAX_LIMIT * 2)
    payload = _fetch(upstream, q, _photon_params(q, lat, lon, fetch_limit))
    ranked = rank_for_housenumber_query(
        _features(payload), housenumber, street_of_query(q))
    results = normalize_results({"features": ranked}, limit, housenumber)
    if results:
        return results

    # Nothing survived: every hit was furniture. Measured against the live
    # index this is the NORMAL outcome for an interpolated address — on the
    # wide denver_core bbox the first page of "1226 E 10th Ave" is eight
    # transit stops, and the street itself does not place at all.
    #
    # So ask again for the street alone, drop the furniture from THAT too (the
    # stops are named "E 10th Ave & Monaco Pkwy", so a street query ranks them
    # first as well), and dedupe what is left. The result is one honest
    # "East 10th Avenue, Denver" of kind `street` — which the client can render
    # differently from a house, and which is the most this index can truthfully
    # say about an address that exists only as an interpolation range.
    street_q = q[len(housenumber):].strip(" ,")
    if not street_q or leading_housenumber(street_q) is not None:
        return results
    street_q = expand_street_abbreviations(street_q)

    log.info("geocode: no indexed address for %r; falling back to the street "
             "%r (photon has no address interpolation)", q, street_q)
    street_payload = _fetch(upstream, street_q,
                            _photon_params(street_q, lat, lon, MAX_LIMIT * 2))
    kept = drop_on_street_furniture(_features(street_payload))
    # The housenumber rides along: this is THE path that answers a numbered
    # query with a bare street, so it is where saying so matters most.
    street_results = dedupe_by_label(
        normalize_results({"features": kept}, MAX_LIMIT * 2, housenumber))
    # A long street leaves the graph: "East 10th Avenue" matches segments in
    # Aurora as well as Denver. Put the routable ones first — an un-routable
    # suggestion at the top of a fallback list is the least useful thing here.
    # Stable, so Photon's own ranking still orders within each group.
    street_results.sort(key=lambda row: not row["in_coverage"])
    return street_results[:limit]


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
