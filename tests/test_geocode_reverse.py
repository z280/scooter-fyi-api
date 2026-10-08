"""GET /api/v1/geocode/reverse — reverse geocoding through our own Photon.

Photon and Postgres are faked. Pinned here: the response contract (label
building, null fields), the Colorado gate, 404 / 503 / 422 / 429, the
Cache-Control header, that the Denver address-point enrichment is used only
when Photon has no house number, and that no coordinate reaches any log line
— the handler's own, uvicorn's access log, httpx's request line, or Sentry.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src import addresses, api_geocode, log_redaction

LAT, LNG = 39.737912, -104.989861       # Bannock St at the Civic Center
LAT_S, LNG_S = "39.737912", "-104.989861"


def _feature(props: dict) -> dict:
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "Point", "coordinates": [LNG, LAT]}}


def _collection(*props: dict) -> dict:
    return {"type": "FeatureCollection", "features": [_feature(p) for p in props]}


HOUSE = {"osm_key": "building", "osm_value": "yes", "type": "house",
         "housenumber": "1550", "street": "Bannock Street",
         "district": "Golden Triangle", "city": "Denver", "postcode": "80202",
         "state": "Colorado", "country": "United States"}
STREET = {"osm_key": "highway", "osm_value": "residential", "type": "street",
          "name": "Bannock Street", "district": "Golden Triangle",
          "city": "Denver", "postcode": "80202"}
BUS_STOP = {"osm_key": "highway", "osm_value": "bus_stop", "type": "house",
            "name": "Colfax & Bannock", "street": "East Colfax Avenue",
            "city": "Denver"}
PARK = {"osm_key": "leisure", "osm_value": "park", "name": "Civic Center Park",
        "city": "Denver"}


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = "stub"

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture()
def env(monkeypatch):
    """Fake Photon + rate limiter + DB. Knobs live on the returned dict."""
    state = {"payload": _collection(HOUSE), "status": 200, "raises": None,
             "enabled": True, "enforce_raises": None, "address_point": None,
             "calls": [], "enforce_calls": [], "nearest_calls": []}

    def fake_get(url, params=None, timeout=None):
        state["calls"].append((url, dict(params or {}), timeout))
        if state["raises"] is not None:
            raise state["raises"]
        return _FakeResponse(state["payload"], state["status"])

    def fake_enforce(cur, **kw):
        state["enforce_calls"].append(kw)
        if state["enforce_raises"] is not None:
            raise state["enforce_raises"]

    class _Cur:
        def execute(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

    @contextmanager
    def fake_connection():
        yield _Conn()

    def fake_nearest(lat, lon, max_meters=addresses.NEAREST_MAX_METERS):
        state["nearest_calls"].append((lat, lon))
        return state["address_point"]

    monkeypatch.setattr(api_geocode.httpx, "get", fake_get)
    monkeypatch.setattr(api_geocode, "enforce", fake_enforce)
    monkeypatch.setattr(api_geocode, "connection", fake_connection)
    monkeypatch.setattr(api_geocode, "geocode_settings",
                        lambda: ("http://photon-test:2322", state["enabled"]))
    monkeypatch.setattr(api_geocode.addresses, "nearest", fake_nearest)
    app = FastAPI()
    app.include_router(api_geocode.router)
    state["client"] = TestClient(app)
    return state


def _get(env, **params):
    params = params or {"lat": LAT_S, "lng": LNG_S}
    return env["client"].get("/api/v1/geocode/reverse", params=params)


# --- contract ----------------------------------------------------------------

def test_house_contract_and_upstream_call(env):
    r = _get(env)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {
        "address": "1550 Bannock Street", "name": None, "housenumber": "1550",
        "street": "Bannock Street", "locality": "Golden Triangle",
        "city": "Denver", "postcode": "80202",
    }
    (url, params, timeout), = env["calls"]
    assert url == "http://photon-test:2322/reverse"
    assert params["lat"] == LAT and params["lon"] == LNG
    assert params["limit"] == api_geocode.REVERSE_FETCH_LIMIT
    assert timeout == api_geocode.SIDECAR_TIMEOUT_SECONDS
    # Photon had the house number: the city index is not consulted.
    assert env["nearest_calls"] == []


def test_street_label_is_qualified_by_the_part_of_town(env):
    env["payload"] = _collection(STREET)
    body = _get(env).json()
    assert body["address"] == "Bannock Street, Golden Triangle"
    assert body["street"] == "Bannock Street"   # a street names itself
    assert body["housenumber"] is None


def test_denver_address_point_supplies_the_missing_house_number(env):
    env["payload"] = _collection(STREET)
    env["address_point"] = {"housenumber": "1437", "street": "Bannock St",
                            "distance_m": 12.0}
    body = _get(env).json()
    assert body["address"] == "1437 Bannock St"
    assert (body["housenumber"], body["street"]) == ("1437", "Bannock St")
    assert body["locality"] == "Golden Triangle" and body["city"] == "Denver"
    assert len(env["nearest_calls"]) == 1


def test_street_furniture_is_skipped_for_what_it_stands_on(env):
    env["payload"] = _collection(BUS_STOP, STREET)
    assert _get(env).json()["address"] == "Bannock Street, Golden Triangle"


def test_furniture_alone_is_still_an_answer(env):
    env["payload"] = _collection(BUS_STOP)
    assert _get(env).json()["address"] == "East Colfax Avenue, Denver"


def test_named_place_without_a_street(env):
    env["payload"] = _collection(PARK)
    body = _get(env).json()
    assert body["address"] == "Civic Center Park, Denver"
    assert body["name"] == "Civic Center Park"
    assert body["street"] is None and body["postcode"] is None


@pytest.mark.parametrize("fields,label", [
    ({"housenumber": "1550", "street": "Bannock Street", "locality": "X"},
     "1550 Bannock Street"),
    ({"street": "Bannock Street", "city": "Denver"}, "Bannock Street, Denver"),
    ({"street": "Bannock Street"}, "Bannock Street"),
    ({"locality": "Golden Triangle", "city": "Denver"}, "Golden Triangle, Denver"),
    ({"city": "Denver"}, "Denver"),
    ({"postcode": "80202"}, "80202"),
    ({}, None),
])
def test_reverse_label(fields, label):
    assert api_geocode.reverse_label(fields) == label


# --- not found / unavailable -------------------------------------------------

def test_nothing_found_is_404(env):
    env["payload"] = _collection()
    r = _get(env)
    assert r.status_code == 404
    assert r.json() == {"detail": {"error": "not_found"}}
    assert r.headers["cache-control"] == "no-store"


def test_nothing_from_photon_but_an_address_point_is_an_answer(env):
    env["payload"] = _collection()
    env["address_point"] = {"housenumber": "1437", "street": "Bannock St"}
    body = _get(env).json()
    assert body["address"] == "1437 Bannock St" and body["city"] is None


def test_unlabellable_feature_is_404(env):
    env["payload"] = _collection({"osm_key": "natural", "osm_value": "tree"})
    assert _get(env).status_code == 404


@pytest.mark.parametrize("kw", [
    {"raises": httpx.ConnectError("refused")},
    {"raises": httpx.ReadTimeout("slow")},
    {"status": 500},
    {"payload": ValueError("not json")},
    {"enabled": False},
])
def test_photon_down_or_disabled_is_503(env, kw):
    env.update(kw)
    r = _get(env)
    assert r.status_code == 503
    assert r.json() == {"detail": {"error": "geocoder_unavailable"}}
    assert r.headers["cache-control"] == "no-store"


# --- validation ----------------------------------------------------------------

@pytest.mark.parametrize("params", [
    {"lat": "91", "lng": LNG_S},
    {"lat": LAT_S, "lng": "-181"},
    {"lat": "abc", "lng": LNG_S},
    {"lat": "nan", "lng": LNG_S},
    {"lat": LAT_S},
    {"lng": LNG_S},
    {"lat": LAT_S, "lon": LNG_S},   # the search's spelling is not accepted here
])
def test_bad_points_are_422_and_never_reach_photon(env, params):
    assert _get(env, **params).status_code == 422
    assert env["calls"] == []


@pytest.mark.parametrize("lat,lng", [
    ("40.7128", "-74.0060"),   # New York
    ("41.20", "-104.80"),      # Cheyenne, just north of the state line
    ("39.74", "-110.00"),      # Utah
    ("0", "0"),
])
def test_points_outside_the_photon_index_are_400(env, lat, lng):
    r = _get(env, lat=lat, lng=lng)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "outside_coverage"
    assert env["calls"] == [] and env["enforce_calls"] == []


@pytest.mark.parametrize("lat,lng", [("37.0", "-109.0"), ("40.99", "-102.05"),
                                     ("39.06", "-108.55")])   # corners + Grand Junction
def test_points_inside_colorado_are_served(env, lat, lng):
    assert _get(env, lat=lat, lng=lng).status_code == 200


# --- rate limit ----------------------------------------------------------------

def test_rate_limit_bucket_and_429(env):
    _get(env)
    (kw,) = env["enforce_calls"]
    assert kw["bucket"] == "geocode_reverse_ip"
    assert (kw["limit"], kw["window_seconds"]) == api_geocode._LIMIT_REVERSE_PER_IP == (60, 60)

    env["enforce_raises"] = HTTPException(429, "rate limited",
                                          headers={"Retry-After": "17"})
    env["calls"].clear()
    r = _get(env)
    assert r.status_code == 429
    assert r.headers["retry-after"] == "17"
    assert r.headers["cache-control"] == "no-store"
    assert env["calls"] == []          # a refused request costs Photon nothing


def test_no_response_cache(env):
    _get(env)
    _get(env)
    assert len(env["calls"]) == 2


# --- never log coordinates -----------------------------------------------------

def _server_log(caplog) -> str:
    # The TestClient's own httpx line is the client side, not the server.
    return "\n".join(r.getMessage() for r in caplog.records if r.name != "httpx")


@pytest.mark.parametrize("kw", [{}, {"raises": httpx.ConnectError("refused to 39.7379")},
                                {"status": 502}, {"payload": _collection()},
                                {"payload": ValueError("x")}])
def test_handler_never_logs_the_coordinate(env, caplog, kw):
    caplog.set_level(logging.DEBUG)
    env.update(kw)
    _get(env)
    text = _server_log(caplog)
    assert "39.73" not in text and "104.98" not in text


def _record(logger: str, msg: str, args: tuple) -> logging.LogRecord:
    return logging.LogRecord(logger, logging.INFO, __file__, 1, msg, args, None)


def _filtered(logger_name: str, rec: logging.LogRecord) -> str:
    for f in logging.getLogger(logger_name).filters:
        f.filter(rec)
    return rec.getMessage()


def test_access_log_redacts_this_route_only():
    import src.main  # noqa: F401 — installs the filters

    def line(path):
        return _filtered("uvicorn.access", _record(
            "uvicorn.access", '%s - "%s %s HTTP/%s" %d',
            ("203.0.113.9:5555", "GET", path, "1.1", 200)))

    out = line(f"/api/v1/geocode/reverse?lat={LAT_S}&lng={LNG_S}")
    assert out.endswith('"GET /api/v1/geocode/reverse?lat=[redacted]&lng=[redacted] HTTP/1.1" 200')
    assert "39.73" not in out and "104.98" not in out
    # Order and extra params do not matter.
    out = line(f"/api/v1/geocode/reverse?lng={LNG_S}&x=1&lat={LAT_S}")
    assert "lng=[redacted]&x=1&lat=[redacted]" in out
    # Plates are still covered by the same filter.
    assert "1025543" not in line("/api/v1/vehicles/resolve?plate=1025543")
    # Every other route is untouched.
    assert "lat=39.7&lon=-105.0" in line("/api/v1/geocode/search?q=x&lat=39.7&lon=-105.0")


def test_httpx_upstream_line_is_redacted():
    import src.main  # noqa: F401

    url = httpx.URL(f"http://photon:2322/reverse?lat={LAT_S}&lon={LNG_S}&limit=5&lang=en")
    out = _filtered("httpx", _record("httpx", 'HTTP Request: %s %s "%s %d %s"',
                                     ("GET", url, "HTTP/1.1", 200, "OK")))
    assert "39.73" not in out and "104.98" not in out
    assert "lat=[redacted]&lon=[redacted]&limit=5&lang=en" in out
    # The forward search's upstream URL is not this rule's business.
    url = httpx.URL("http://photon:2322/api?q=x&lat=39.74&lon=-104.99")
    out = _filtered("httpx", _record("httpx", "HTTP Request: %s %s", ("GET", url)))
    assert "lat=39.74" in out


def test_install_is_idempotent():
    log_redaction.install()
    log_redaction.install()
    for name in ("uvicorn.access", "httpx"):
        n = sum(isinstance(f, log_redaction.RedactSensitiveQuery)
                for f in logging.getLogger(name).filters)
        assert n == 1


def test_sentry_event_and_breadcrumb_scrubbing():
    event = {
        "request": {"url": "https://data.scooter.fyi/api/v1/geocode/reverse",
                    "query_string": f"lat={LAT_S}&lng={LNG_S}"},
        "breadcrumbs": {"values": [{
            "category": "httplib",
            "data": {"url": "http://photon:2322/reverse",
                     "http.query": f"lat={LAT_S}&lon={LNG_S}&limit=5"}}]},
    }
    out = log_redaction.scrub_sentry_event(event)
    assert out["request"]["query_string"] == "lat=[redacted]&lng=[redacted]"
    assert out["breadcrumbs"]["values"][0]["data"]["http.query"] == \
        "lat=[redacted]&lon=[redacted]&limit=5"
    other = {"request": {"url": "https://x/api/v1/route", "query_string": "from=1,2"}}
    assert log_redaction.scrub_sentry_event(other)["request"]["query_string"] == "from=1,2"


def test_vehicle_plates_keeps_its_filter_name():
    from src import api_vehicle_plates
    assert api_vehicle_plates.RedactPlateQuery is log_redaction.RedactSensitiveQuery
