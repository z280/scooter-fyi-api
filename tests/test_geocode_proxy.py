"""GET /api/v1/geocode/search — the Photon proxy.

Photon itself is stubbed: what is pinned here is the contract the client codes
against (normalized shape, `kind` vocabulary, `in_coverage`), the bbox/bias
params sent upstream, the per-IP bucket, the cache, and the single 503 every
failure collapses to. Photon's own ranking quality is verified against the live
sidecar, not here.
"""

from __future__ import annotations

from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src import api_geocode
from src.config import load

# Inside the routing graph_bbox (west -105.060 / south 39.650 / east -104.880 /
# north 39.790) -> in_coverage true.
_IN_LAT, _IN_LON = 39.747, -104.992
# Inside envelope.denver_core (the bbox we FILTER on) but outside the routing
# graph — the whole reason `in_coverage` exists.
_OUT_LAT, _OUT_LON = 39.860, -105.150


def _feature(props: dict, lat: float = _IN_LAT, lon: float = _IN_LON) -> dict:
    # GeoJSON is [lon, lat].
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def _collection(*features: dict) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


_HOUSE = _feature({"type": "house", "housenumber": "1701",
                   "street": "Champa Street", "city": "Denver",
                   "state": "Colorado", "postcode": "80202",
                   "osm_key": "place", "osm_value": "house"})


class _FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or "stub"

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture(autouse=True)
def _clear_cache():
    api_geocode._CACHE.clear()
    yield
    api_geocode._CACHE.clear()


def _app():
    app = FastAPI()
    app.include_router(api_geocode.router)
    return app


def _install(monkeypatch, payload=None, *, raises=None, status=200,
             enforce_raises=None, enabled=True,
             upstream="http://photon-test:2322"):
    """Stub the sidecar + the rate limiter. Returns (calls, enforce_calls).

    `calls` collects (url, params, timeout) per upstream request, which is how
    the cache tests count fetches.
    """
    calls: list[tuple[str, dict, float]] = []
    enforce_calls: list[dict] = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, dict(params or {}), timeout))
        if raises is not None:
            raise raises
        return _FakeResponse(payload if payload is not None else _collection(),
                             status_code=status)

    def fake_enforce(cur, **kw):
        enforce_calls.append(kw)
        if enforce_raises is not None:
            raise enforce_raises

    class _FakeCursor:
        def execute(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        def commit(self):
            pass

    @contextmanager
    def fake_connection():
        yield _FakeConn()

    monkeypatch.setattr(api_geocode.httpx, "get", fake_get)
    monkeypatch.setattr(api_geocode, "enforce", fake_enforce)
    monkeypatch.setattr(api_geocode, "connection", fake_connection)
    monkeypatch.setattr(api_geocode, "geocode_settings", lambda: (upstream, enabled))
    return calls, enforce_calls


# --- kind mapping (pure) -----------------------------------------------------

@pytest.mark.parametrize("props,expected", [
    ({"type": "house"}, "house"),
    ({"type": "street"}, "street"),
    ({"type": "city"}, "locality"),
    ({"type": "locality"}, "locality"),
    ({"type": "district"}, "locality"),
    ({"type": "state"}, "locality"),
    # `layer` is what newer photon builds emit; both are honored.
    ({"layer": "house"}, "house"),
    # A named POI: photon reports type "other", so the osm tags decide.
    ({"type": "other", "name": "Union Station",
      "osm_key": "building", "osm_value": "train_station"}, "house"),
    ({"type": "other", "name": "Rosenberg's",
      "osm_key": "amenity", "osm_value": "bakery"}, "poi"),
    ({"type": "other", "osm_key": "highway", "osm_value": "residential"}, "street"),
    # An object sitting ON a street is not the street.
    ({"name": "16th & Champa", "osm_key": "highway", "osm_value": "bus_stop"}, "poi"),
    ({"osm_key": "place", "osm_value": "suburb", "name": "Five Points"}, "locality"),
    ({"osm_key": "place", "osm_value": "house", "housenumber": "12"}, "house"),
    # No type, no osm tags: an unnamed hit with a housenumber is an address.
    ({"housenumber": "1701", "street": "Champa Street"}, "house"),
    ({}, "poi"),
])
def test_kind_mapping(props, expected):
    assert api_geocode.kind_for(props) == expected


# --- label composition (pure) ------------------------------------------------

def test_label_reads_like_an_address():
    props = {"housenumber": "1701", "street": "Champa St", "city": "Denver",
             "state": "Colorado", "postcode": "80202"}
    assert api_geocode.label_for(props, "house") == "1701 Champa St, Denver"


def test_label_keeps_poi_name_and_one_qualifier():
    props = {"name": "Union Station", "housenumber": "1701",
             "street": "Wewatta St", "city": "Denver", "state": "Colorado"}
    assert api_geocode.label_for(props, "poi") == "Union Station, 1701 Wewatta St, Denver"


def test_label_does_not_repeat_the_street_as_name():
    props = {"name": "Champa St", "street": "Champa St", "city": "Denver"}
    assert api_geocode.label_for(props, "street") == "Champa St, Denver"


def test_locality_label_carries_the_state():
    props = {"name": "Denver", "city": "Denver", "state": "Colorado",
             "country": "United States"}
    assert api_geocode.label_for(props, "locality") == "Denver, Colorado"


def test_label_falls_back_when_there_is_no_name_or_street():
    assert api_geocode.label_for({"state": "Colorado"}, "poi") == "Colorado"
    assert api_geocode.label_for({}, "poi") == ""


# --- normalization -----------------------------------------------------------

def test_normalizes_to_the_documented_shape(monkeypatch):
    calls, _ = _install(monkeypatch, _collection(_HOUSE))
    r = TestClient(_app()).get("/api/v1/geocode/search", params={"q": "1701 Champa"})
    assert r.status_code == 200
    assert r.json() == {"results": [{
        "label": "1701 Champa Street, Denver",
        "lat": _IN_LAT, "lon": _IN_LON,
        "kind": "house", "in_coverage": True,
    }]}


def test_in_coverage_is_false_outside_the_routing_graph(monkeypatch):
    """A hit inside the (wider) search envelope but outside the graph must be
    returned and flagged, not dropped — the client greys it out."""
    graph = load().valhalla
    assert graph.contains(_IN_LAT, _IN_LON)
    assert not graph.contains(_OUT_LAT, _OUT_LON)

    payload = _collection(
        _HOUSE,
        _feature({"type": "city", "name": "Golden", "state": "Colorado"},
                 lat=_OUT_LAT, lon=_OUT_LON),
    )
    _install(monkeypatch, payload)
    body = TestClient(_app()).get(
        "/api/v1/geocode/search", params={"q": "golden"}).json()
    assert [(x["kind"], x["in_coverage"]) for x in body["results"]] == [
        ("house", True), ("locality", False)]


def test_unusable_features_are_skipped_not_blanked(monkeypatch):
    payload = _collection(
        {"type": "Feature", "properties": {"type": "house"}},          # no geometry
        _feature({"type": "house"}),                                    # nothing to label
        {"type": "Feature", "properties": {"name": "x"},
         "geometry": {"type": "Point", "coordinates": ["a", "b"]}},     # junk coords
        _HOUSE,
    )
    _install(monkeypatch, payload)
    body = TestClient(_app()).get(
        "/api/v1/geocode/search", params={"q": "champa"}).json()
    assert len(body["results"]) == 1


def test_results_are_truncated_to_the_requested_limit(monkeypatch):
    payload = _collection(*[
        _feature({"type": "house", "housenumber": str(n), "street": "Champa St",
                  "city": "Denver"}) for n in range(1, 9)
    ])
    _install(monkeypatch, payload)
    body = TestClient(_app()).get(
        "/api/v1/geocode/search", params={"q": "champa", "limit": 3}).json()
    assert len(body["results"]) == 3


# --- upstream request shape --------------------------------------------------

def test_bbox_is_denver_core_in_photon_lon_first_order(monkeypatch):
    calls, _ = _install(monkeypatch, _collection(_HOUSE))
    TestClient(_app()).get("/api/v1/geocode/search", params={"q": "champa"})
    url, params, timeout = calls[0]
    assert url == "http://photon-test:2322/api"
    box = load().denver_core
    assert params["bbox"] == (f"{box.lon_min:.6f},{box.lat_min:.6f},"
                              f"{box.lon_max:.6f},{box.lat_max:.6f}")
    # Wider than the routing graph on every side, deliberately: filtering on
    # graph_bbox would make in_coverage vacuous.
    graph = load().valhalla
    assert box.lon_min < graph.bbox_west and box.lon_max > graph.bbox_east
    assert box.lat_min < graph.bbox_south and box.lat_max > graph.bbox_north
    assert timeout == api_geocode.SIDECAR_TIMEOUT_SECONDS == 3.0


def test_bias_and_limit_are_passed_through(monkeypatch):
    calls, _ = _install(monkeypatch, _collection(_HOUSE))
    r = TestClient(_app()).get("/api/v1/geocode/search", params={
        "q": "  champa  st ", "lat": 39.7523, "lon": -104.9911, "limit": 4})
    assert r.status_code == 200
    _, params, _ = calls[0]
    # Rounded to the cache key's 2dp so a cached response is exactly what any
    # key-equal request would have received.
    assert params["lat"] == 39.75
    assert params["lon"] == -104.99
    assert params["limit"] == 4
    # Whitespace-collapsed, which is also what the cache keys on.
    assert params["q"] == "champa st"


def test_no_bias_sends_no_lat_lon(monkeypatch):
    calls, _ = _install(monkeypatch, _collection(_HOUSE))
    TestClient(_app()).get("/api/v1/geocode/search", params={"q": "champa"})
    _, params, _ = calls[0]
    assert "lat" not in params and "lon" not in params


def test_default_limit_is_six(monkeypatch):
    calls, _ = _install(monkeypatch, _collection(_HOUSE))
    TestClient(_app()).get("/api/v1/geocode/search", params={"q": "champa"})
    assert calls[0][1]["limit"] == 6


# --- validation --------------------------------------------------------------

@pytest.mark.parametrize("q", ["", "a", " x ", "x" * 101])
def test_q_length_is_validated(monkeypatch, q):
    calls, _ = _install(monkeypatch, _collection(_HOUSE))
    r = TestClient(_app()).get("/api/v1/geocode/search", params={"q": q})
    assert r.status_code == 422
    assert calls == []


def test_q_at_the_bounds_is_accepted(monkeypatch):
    _install(monkeypatch, _collection(_HOUSE))
    c = TestClient(_app())
    assert c.get("/api/v1/geocode/search", params={"q": "ab"}).status_code == 200
    assert c.get("/api/v1/geocode/search",
                 params={"q": "x" * 100}).status_code == 200


def test_limit_above_eight_is_rejected(monkeypatch):
    _install(monkeypatch, _collection(_HOUSE))
    r = TestClient(_app()).get("/api/v1/geocode/search",
                               params={"q": "champa", "limit": 9})
    assert r.status_code == 422


def test_half_a_bias_is_rejected(monkeypatch):
    calls, _ = _install(monkeypatch, _collection(_HOUSE))
    r = TestClient(_app()).get("/api/v1/geocode/search",
                               params={"q": "champa", "lat": 39.75})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_bias"
    assert calls == []


# --- rate limit --------------------------------------------------------------

def test_rate_limited_20_per_minute_per_ip(monkeypatch):
    _, enforce_calls = _install(monkeypatch, _collection(_HOUSE))
    r = TestClient(_app()).get("/api/v1/geocode/search", params={"q": "champa"},
                               headers={"cf-connecting-ip": "203.0.113.7"})
    assert r.status_code == 200
    assert len(enforce_calls) == 1
    assert enforce_calls[0] == {"bucket": "geocode_ip", "key": "203.0.113.7",
                                "limit": 20, "window_seconds": 60}


def test_missing_client_ip_falls_back_to_the_question_mark_key(monkeypatch):
    """`real_client_ip` can return None; the bucket must still be keyed."""
    _, enforce_calls = _install(monkeypatch, _collection(_HOUSE))
    monkeypatch.setattr(api_geocode, "real_client_ip", lambda request: None)
    TestClient(_app()).get("/api/v1/geocode/search", params={"q": "champa"})
    assert enforce_calls[0]["key"] == "?"


def test_429_propagates_and_no_upstream_call_is_made(monkeypatch):
    calls, _ = _install(
        monkeypatch, _collection(_HOUSE),
        enforce_raises=HTTPException(429, detail="rate limit exceeded — try again later",
                                     headers={"Retry-After": "37"}))
    r = TestClient(_app()).get("/api/v1/geocode/search", params={"q": "champa"})
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "37"
    assert calls == []


def test_cache_hits_still_count_against_the_bucket(monkeypatch):
    calls, enforce_calls = _install(monkeypatch, _collection(_HOUSE))
    c = TestClient(_app())
    c.get("/api/v1/geocode/search", params={"q": "champa"})
    c.get("/api/v1/geocode/search", params={"q": "champa"})
    assert len(calls) == 1          # cached
    assert len(enforce_calls) == 2  # but still counted


# --- sidecar failures --------------------------------------------------------

@pytest.mark.parametrize("exc", [
    httpx.ReadTimeout("timed out"),
    httpx.ConnectTimeout("timed out"),
    httpx.ConnectError("connection refused"),
])
def test_sidecar_timeout_or_refusal_is_503_geocoder_unavailable(monkeypatch, exc):
    _install(monkeypatch, raises=exc)
    r = TestClient(_app()).get("/api/v1/geocode/search", params={"q": "champa"})
    assert r.status_code == 503
    assert r.json()["detail"] == {"error": "geocoder_unavailable"}


def test_sidecar_error_status_is_503(monkeypatch):
    _install(monkeypatch, {"message": "no index"}, status=500)
    r = TestClient(_app()).get("/api/v1/geocode/search", params={"q": "champa"})
    assert r.status_code == 503
    assert r.json()["detail"] == {"error": "geocoder_unavailable"}


def test_sidecar_non_json_is_503(monkeypatch):
    _install(monkeypatch, ValueError("not json"))
    r = TestClient(_app()).get("/api/v1/geocode/search", params={"q": "champa"})
    assert r.status_code == 503
    assert r.json()["detail"] == {"error": "geocoder_unavailable"}


def test_a_failed_lookup_is_not_cached(monkeypatch):
    calls, _ = _install(monkeypatch, raises=httpx.ReadTimeout("x"))
    c = TestClient(_app())
    c.get("/api/v1/geocode/search", params={"q": "champa"})
    c.get("/api/v1/geocode/search", params={"q": "champa"})
    assert len(calls) == 2


def test_disabled_by_config_is_the_same_503(monkeypatch):
    """`"geocode": {"enabled": false}` must be indistinguishable from a dead
    sidecar so the client's degraded path is the same one."""
    calls, enforce_calls = _install(monkeypatch, _collection(_HOUSE), enabled=False)
    r = TestClient(_app()).get("/api/v1/geocode/search", params={"q": "champa"})
    assert r.status_code == 503
    assert r.json()["detail"] == {"error": "geocoder_unavailable"}
    assert calls == [] and enforce_calls == []


def test_upstream_defaults_to_the_compose_service_name():
    """config.json may carry no geocode block yet; the endpoint must still
    point at the sidecar rather than crashing at import or request time."""
    upstream, enabled = api_geocode.geocode_settings()
    assert upstream.startswith("http://")
    assert isinstance(enabled, bool)


# --- cache -------------------------------------------------------------------

def test_identical_query_is_served_from_cache(monkeypatch):
    calls, _ = _install(monkeypatch, _collection(_HOUSE))
    c = TestClient(_app())
    first = c.get("/api/v1/geocode/search", params={"q": "1701 Champa"}).json()
    second = c.get("/api/v1/geocode/search", params={"q": "  1701   champa "}).json()
    assert first == second
    # Normalized + case-folded key: one upstream call for both spellings.
    assert len(calls) == 1


def test_bias_rounded_to_two_decimals_shares_a_cache_entry(monkeypatch):
    calls, _ = _install(monkeypatch, _collection(_HOUSE))
    c = TestClient(_app())
    c.get("/api/v1/geocode/search",
          params={"q": "champa", "lat": 39.7501, "lon": -104.9899})
    c.get("/api/v1/geocode/search",
          params={"q": "champa", "lat": 39.7549, "lon": -104.9904})
    assert len(calls) == 1
    # ...but a materially different bias does not.
    c.get("/api/v1/geocode/search",
          params={"q": "champa", "lat": 39.6801, "lon": -104.9899})
    assert len(calls) == 2


def test_a_larger_limit_refetches_rather_than_under_serving(monkeypatch):
    """The cache key carries no `limit` (per the plan), so an entry fetched at
    limit=2 must not be allowed to answer limit=6 with two results."""
    payload = _collection(*[
        _feature({"type": "house", "housenumber": str(n), "street": "Champa St",
                  "city": "Denver"}) for n in range(1, 7)
    ])
    calls, _ = _install(monkeypatch, payload)
    c = TestClient(_app())
    small = c.get("/api/v1/geocode/search",
                  params={"q": "champa", "limit": 2}).json()
    assert len(small["results"]) == 2
    big = c.get("/api/v1/geocode/search",
                params={"q": "champa", "limit": 6}).json()
    assert len(big["results"]) == 6
    assert len(calls) == 2
    # And the reverse direction IS a hit — a bigger entry can serve a slice.
    again = c.get("/api/v1/geocode/search",
                  params={"q": "champa", "limit": 2}).json()
    assert again["results"] == small["results"]
    assert len(calls) == 2


def test_a_short_result_set_serves_a_larger_limit_from_cache(monkeypatch):
    """Photon returned fewer hits than asked for, so there is nothing more to
    fetch — a larger limit must not re-query the sidecar."""
    calls, _ = _install(monkeypatch, _collection(_HOUSE))
    c = TestClient(_app())
    c.get("/api/v1/geocode/search", params={"q": "champa", "limit": 3})
    c.get("/api/v1/geocode/search", params={"q": "champa", "limit": 6})
    assert len(calls) == 1


def test_cache_evicts_least_recently_used_at_512_entries():
    cache = api_geocode._TTLCache(max_entries=3, ttl_seconds=60)
    for i in range(3):
        cache.put((f"q{i}", None, None), 6, [{"label": str(i)}])
    cache.get(("q0", None, None), 6)          # q0 becomes most-recent
    cache.put(("q3", None, None), 6, [{"label": "3"}])
    assert len(cache) == 3
    assert cache.get(("q1", None, None), 6) is None   # evicted
    assert cache.get(("q0", None, None), 6) is not None
    assert api_geocode.CACHE_MAX_ENTRIES == 512


def test_cache_entries_expire_after_the_ttl(monkeypatch):
    cache = api_geocode._TTLCache(max_entries=8, ttl_seconds=24 * 3600)
    now = [1000.0]
    monkeypatch.setattr(api_geocode.time, "monotonic", lambda: now[0])
    cache.put(("q", None, None), 6, [{"label": "x"}])
    now[0] += 24 * 3600 - 1
    assert cache.get(("q", None, None), 6) is not None
    now[0] += 2
    assert cache.get(("q", None, None), 6) is None
    assert api_geocode.CACHE_TTL_SECONDS == 24 * 3600


def test_cached_rows_cannot_be_mutated_by_a_caller():
    cache = api_geocode._TTLCache(max_entries=4, ttl_seconds=60)
    cache.put(("q", None, None), 6, [{"label": "orig"}])
    got = cache.get(("q", None, None), 6)
    got[0]["label"] = "tampered"
    assert cache.get(("q", None, None), 6)[0]["label"] == "orig"


# --- house-number queries ----------------------------------------------------
#
# Photon does no address interpolation, so most residential Denver house
# numbers match nothing and the leftover hits are transit stops named after
# intersections. These pin the behaviour that keeps that from becoming a
# confident pick on the wrong side of town.

@pytest.mark.parametrize("q,expected", [
    ("1226 E 10th Ave", "1226"),
    ("1226", "1226"),
    ("221B Baker St", "221B"),
    ("  1500 Champa  ", "1500"),
    # NOT an address query: an ordinal street must not be read as a house
    # number, or every "10th Avenue" search would be treated as a failed
    # address lookup and filtered accordingly.
    ("10th Avenue", None),
    ("1st Ave Denver", None),
    ("Union Station", None),
    ("Cheesman Park", None),
])
def test_leading_housenumber(q, expected):
    assert api_geocode.leading_housenumber(q) == expected


def test_transit_stop_is_never_a_house_even_when_photon_says_so():
    """Photon's `type` is address granularity, not object class.

    A stop carrying a full street address is emitted as `type: house`; trusting
    that labelled "E 10th Ave & Monaco Pkwy" a house.
    """
    props = {"type": "house", "name": "E 10th Ave & Monaco Pkwy",
             "street": "East 10th Avenue", "city": "Denver",
             "osm_key": "highway", "osm_value": "bus_stop"}
    assert api_geocode.kind_for(props) == "poi"

    rail = {"type": "house", "name": "RTD D, E & H Lines",
            "osm_key": "railway", "osm_value": "light_rail"}
    assert api_geocode.kind_for(rail) == "poi"


def _stop():
    return _feature({"type": "house", "name": "E 10th Ave & Monaco Pkwy",
                     "street": "East 10th Avenue", "city": "Denver",
                     "osm_key": "highway", "osm_value": "bus_stop"})


def _street():
    return _feature({"type": "street", "name": "East 10th Avenue",
                     "city": "Denver", "osm_key": "highway",
                     "osm_value": "residential"})


def _exact():
    return _feature({"type": "house", "housenumber": "1226",
                     "street": "East 10th Avenue", "city": "Denver",
                     "osm_key": "building", "osm_value": "house"})


def test_exact_housenumber_is_promoted_over_a_better_scoring_neighbour():
    ranked = api_geocode.rank_for_housenumber_query(
        [_stop(), _street(), _exact()], "1226")
    assert ranked[0]["properties"]["housenumber"] == "1226"
    # Nothing is dropped once an exact match exists.
    assert len(ranked) == 3


def test_without_an_exact_match_furniture_is_dropped_and_the_street_kept():
    ranked = api_geocode.rank_for_housenumber_query([_stop(), _street()], "1226")
    assert [f["properties"].get("osm_value") for f in ranked] == ["residential"]


def test_a_genuine_poi_survives_a_housenumber_query():
    """Only on-street furniture is dropped, not every non-matching hit."""
    cafe = _feature({"type": "other", "name": "Rosenberg's Bagels",
                     "housenumber": "725", "street": "18th Street",
                     "osm_key": "amenity", "osm_value": "bakery"})
    ranked = api_geocode.rank_for_housenumber_query([_stop(), cafe], "1226")
    assert [f["properties"]["name"] for f in ranked] == ["Rosenberg's Bagels"]


def test_interpolated_address_returns_the_street_not_a_distant_stop(monkeypatch):
    """The reported failure: 1226 E 10th Ave -> a stop ~5 miles east."""
    _install(monkeypatch, _collection(_stop(), _street()))
    resp = TestClient(_app()).get("/api/v1/geocode/search",
                                  params={"q": "1226 E 10th Ave"})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert all("Monaco" not in r["label"] for r in results), results
    assert results and results[0]["kind"] == "street"


def test_a_housenumber_query_overfetches_so_filtering_cannot_starve_it(monkeypatch):
    calls, _ = _install(monkeypatch, _collection(_street()))
    client = TestClient(_app())
    client.get("/api/v1/geocode/search", params={"q": "1226 E 10th Ave", "limit": 6})
    assert calls[-1][1]["limit"] > 6

    # A non-address query is untouched: no filtering, so no over-fetch.
    api_geocode._CACHE.clear()
    client.get("/api/v1/geocode/search", params={"q": "Cheesman Park", "limit": 6})
    assert calls[-1][1]["limit"] == 6


def test_a_non_address_query_keeps_transit_stops(monkeypatch):
    """Someone searching for the stop itself should still find it."""
    _install(monkeypatch, _collection(_stop()))
    resp = TestClient(_app()).get("/api/v1/geocode/search",
                                  params={"q": "E 10th Ave & Monaco Pkwy"})
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 1


# --- street fallback ---------------------------------------------------------

@pytest.mark.parametrize("street,expected", [
    ("E 10th Ave", "East 10th Avenue"),
    ("E 1st Ave", "East 1st Avenue"),
    ("W Colfax Ave", "West Colfax Avenue"),
    ("N Broadway", "North Broadway"),
    ("Champa St", "Champa Street"),
    ("Chopper Cir", "Chopper Circle"),
    # Position matters: a street type only expands at the end, a directional
    # only at the front. Otherwise "St Anne Ave" becomes "Street Anne Avenue".
    ("St Anne Ave", "St Anne Avenue"),
    ("Blake Street", "Blake Street"),
    ("Larimer", "Larimer"),
])
def test_expand_street_abbreviations(street, expected):
    assert api_geocode.expand_street_abbreviations(street) == expected


def _install_sequence(monkeypatch, payloads, upstream="http://photon-test:2322"):
    """Like `_install`, but serves a different payload per upstream call."""
    calls: list[tuple[str, dict, float]] = []
    queue = list(payloads)

    def fake_get(url, params=None, timeout=None):
        calls.append((url, dict(params or {}), timeout))
        return _FakeResponse(queue.pop(0) if queue else _collection())

    class _FakeCursor:
        def execute(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        def commit(self):
            pass

    @contextmanager
    def fake_connection():
        yield _FakeConn()

    monkeypatch.setattr(api_geocode.httpx, "get", fake_get)
    monkeypatch.setattr(api_geocode, "enforce", lambda cur, **kw: None)
    monkeypatch.setattr(api_geocode, "connection", fake_connection)
    monkeypatch.setattr(api_geocode, "geocode_settings", lambda: (upstream, True))
    return calls


def test_all_furniture_falls_back_to_the_expanded_street(monkeypatch):
    """The live shape of the reported bug.

    Photon's first page for "1226 E 10th Ave" is entirely transit stops, and
    the street does not appear at any depth for the ABBREVIATED form — only
    for the expanded one. So the fallback must re-query, expanded.
    """
    calls = _install_sequence(monkeypatch, [
        _collection(_stop(), _stop()),   # page 1: nothing but furniture
        _collection(_street()),          # the expanded street query
    ])
    resp = TestClient(_app()).get("/api/v1/geocode/search",
                                  params={"q": "1226 E 10th Ave"})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["kind"] for r in results] == ["street"]
    assert "Monaco" not in results[0]["label"]

    assert len(calls) == 2, "expected a second, street-only query"
    assert calls[0][1]["q"] == "1226 E 10th Ave"
    assert calls[1][1]["q"] == "East 10th Avenue"


def test_the_street_fallback_puts_routable_segments_first(monkeypatch):
    """A long street leaves the graph; an un-routable row must not lead."""
    far = _feature({"type": "street", "name": "East 10th Avenue",
                    "osm_key": "highway", "osm_value": "residential"},
                   lat=_OUT_LAT, lon=_OUT_LON)
    near = _feature({"type": "street", "name": "East 10th Avenue",
                     "city": "Denver", "osm_key": "highway",
                     "osm_value": "residential"})
    calls = _install_sequence(monkeypatch, [
        _collection(_stop()),
        _collection(far, near),   # photon ranks the un-routable one first
    ])
    resp = TestClient(_app()).get("/api/v1/geocode/search",
                                  params={"q": "1226 E 10th Ave"})
    results = resp.json()["results"]
    assert results[0]["in_coverage"] is True
    assert len(calls) == 2


def test_identical_street_segments_collapse_to_one_row(monkeypatch):
    """A named street is many OSM ways; four identical labels read as four
    choices when it is really one."""
    _install_sequence(monkeypatch, [
        _collection(_stop()),
        _collection(_street(), _street(), _street(), _street()),
    ])
    resp = TestClient(_app()).get("/api/v1/geocode/search",
                                  params={"q": "1226 E 10th Ave"})
    assert len(resp.json()["results"]) == 1


def test_no_fallback_when_the_address_itself_resolved(monkeypatch):
    calls = _install_sequence(monkeypatch, [_collection(_exact())])
    resp = TestClient(_app()).get("/api/v1/geocode/search",
                                  params={"q": "1226 E 10th Ave"})
    assert resp.json()["results"][0]["kind"] == "house"
    assert len(calls) == 1, "a resolved address must not trigger a second query"
