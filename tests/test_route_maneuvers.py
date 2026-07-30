"""Turn-by-turn passthrough on GET /api/v1/route + the per-IP rate limits.

Two things are locked in here.

**Shape-index re-offsetting.** Valhalla numbers `begin_shape_index` /
`end_shape_index` per LEG, while the endpoint returns one flattened LineString
whose duplicated leg-boundary vertices have been dropped — conditionally, only
where the boundary vertex actually repeats, and empty-shape legs are skipped
entirely. So the offsets cannot be "one dropped vertex per join": the fixture
below deliberately mixes a repeating boundary, an empty-shape leg and a
NON-repeating boundary, and every returned index is checked to address the
coordinate the leg-local index named. Getting this wrong silently misplaces
every turn cue in the nav HUD.

**Rate limits.** `route_ip` 30/min and `route_profiles_ip` 60/min per client IP.
`enforce` is recorded rather than driven, so no Postgres is needed.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src import api_route, ratelimit, valhalla
from src.polyline import encode as encode_polyline


# --- fixture: a four-leg trip that exercises every offset case ---------------
#
#  leg A  4 pts, indices 0..3                       -> offset 0
#  leg B  first pt REPEATS A's last  -> 2 new pts    -> offset 3 (not 4)
#  leg C  empty shape, skipped entirely              -> no offset, no maneuvers
#  leg D  first pt does NOT repeat   -> 2 new pts    -> offset 6
#
# Flattened shape is therefore 8 points long.

_LEG_A_PTS = [(39.7400, -104.9900), (39.7410, -104.9900),
              (39.7420, -104.9900), (39.7430, -104.9900)]
# Starts on A's final vertex: the duplicate that trip_shape() drops.
_LEG_B_PTS = [(39.7430, -104.9900), (39.7440, -104.9900), (39.7450, -104.9900)]
# Starts somewhere else entirely — nothing to drop (Valhalla does this on a
# leg that begins at a snapped waypoint offset from the previous leg's end).
_LEG_D_PTS = [(39.7460, -104.9910), (39.7470, -104.9910)]


def _enc(points):
    return encode_polyline(points, precision=6)


def _multileg_trip():
    """Trip + the per-leg point lists its maneuvers are numbered against."""
    legs = [
        {
            "shape": _enc(_LEG_A_PTS),
            "maneuvers": [
                {"instruction": "Head north on Champa Street", "type": 1,
                 "street_names": ["Champa Street"], "length": 0.412, "time": 96.0,
                 "begin_shape_index": 0, "end_shape_index": 2},
                {"instruction": "Turn right onto 17th Street", "type": 10,
                 "street_names": ["17th Street"], "length": 0.1, "time": 20.0,
                 "begin_shape_index": 2, "end_shape_index": 3},
            ],
        },
        {
            "shape": _enc(_LEG_B_PTS),
            "maneuvers": [
                {"instruction": "Continue on the Cherry Creek Trail", "type": 8,
                 "street_names": ["Cherry Creek Trail"], "length": 0.25, "time": 60.0,
                 "begin_shape_index": 0, "end_shape_index": 2},
            ],
        },
        {
            # Zero-length leg (back-to-back waypoints): no shape at all.
            "shape": "",
            "maneuvers": [
                {"instruction": "You have arrived at your 1st destination.", "type": 5,
                 "street_names": [], "length": 0.0, "time": 0.0,
                 "begin_shape_index": 0, "end_shape_index": 0},
            ],
        },
        {
            "shape": _enc(_LEG_D_PTS),
            "maneuvers": [
                {"instruction": "You have arrived at your destination.", "type": 4,
                 "length": 0.02, "time": 5.0,
                 "begin_shape_index": 0, "end_shape_index": 1},
            ],
        },
    ]
    trip = {"legs": legs, "summary": {"length": 0.782, "time": 181.0}}
    # None marks a leg that contributes no coordinates.
    leg_points = [_LEG_A_PTS, _LEG_B_PTS, None, _LEG_D_PTS]
    return trip, leg_points


# --- valhalla.trip_shape_with_leg_offsets ------------------------------------

def test_offsets_follow_trip_shapes_conditional_drop():
    trip, _ = _multileg_trip()
    points, offsets = valhalla.trip_shape_with_leg_offsets(trip)

    assert len(points) == 8
    # leg B's offset is 3, NOT 4: its local index 0 is the vertex already
    # emitted by leg A. leg D's is 6, NOT 5: its boundary vertex does not
    # repeat, so nothing was dropped there — the "one drop per join" formula
    # every other offset scheme reaches for is wrong on both counts.
    assert offsets == [0, 3, None, 6]


def test_trip_shape_delegates_so_the_two_can_never_disagree():
    """The flattened shape and the offsets must come from one pass."""
    trip, _ = _multileg_trip()
    points, _ = valhalla.trip_shape_with_leg_offsets(trip)
    assert valhalla.trip_shape(trip) == points


def test_empty_trip_has_no_shape_and_no_offsets():
    assert valhalla.trip_shape_with_leg_offsets({}) == ([], [])
    assert valhalla.trip_maneuvers({}) == []


# --- valhalla.trip_maneuvers -------------------------------------------------

def test_every_returned_index_addresses_the_named_coordinate():
    """The acceptance criterion: indices address the returned LineString.

    Checked structurally rather than against hardcoded numbers — for every
    maneuver, the coordinate at the rewritten index must be the coordinate the
    leg-local index named inside that leg's own shape.
    """
    trip, leg_points = _multileg_trip()
    shape, _ = valhalla.trip_shape_with_leg_offsets(trip)
    out = valhalla.trip_maneuvers(trip)

    expected: list[tuple[list, dict]] = []
    for pts, leg in zip(leg_points, trip["legs"]):
        if pts is None:                       # contributed no coordinates
            continue
        for native in leg["maneuvers"]:
            expected.append((pts, native))
    assert len(out) == len(expected)

    for got, (pts, native) in zip(out, expected):
        assert shape[got["begin_shape_index"]] == pts[native["begin_shape_index"]]
        assert shape[got["end_shape_index"]] == pts[native["end_shape_index"]]
        # Indices must stay inside the LineString.
        assert 0 <= got["begin_shape_index"] <= got["end_shape_index"] < len(shape)


def test_exact_rewritten_indices():
    """Belt and braces on the numbers themselves, so a regression in the
    conditional-drop logic can't be masked by a matching bug in the helper."""
    trip, _ = _multileg_trip()
    out = valhalla.trip_maneuvers(trip)
    assert [(m["begin_shape_index"], m["end_shape_index"]) for m in out] == [
        (0, 2),   # leg A, unshifted
        (2, 3),   # leg A
        (3, 5),   # leg B: +3, i.e. its first vertex is A's last
        (6, 7),   # leg D: +6, nothing dropped at this boundary
    ]


def test_maneuvers_from_a_shapeless_leg_are_dropped():
    """An index that addresses no coordinate is worse than a missing cue."""
    trip, _ = _multileg_trip()
    out = valhalla.trip_maneuvers(trip)
    assert not any("1st destination" in m["instruction"] for m in out)
    # ...but the leg that follows the skipped one still reports its maneuver.
    assert out[-1]["instruction"] == "You have arrived at your destination."


def test_lengths_are_converted_from_kilometers_to_meters():
    """route() pins directions_options.units=kilometers, so native `length` is
    km — exactly like summary.length in trip_summary()."""
    trip, _ = _multileg_trip()
    out = valhalla.trip_maneuvers(trip)
    assert out[0]["length_meters"] == pytest.approx(412.0)
    assert out[1]["length_meters"] == pytest.approx(100.0)
    assert out[2]["length_meters"] == pytest.approx(250.0)
    # `time` is already seconds and passes through.
    assert out[0]["time_seconds"] == pytest.approx(96.0)
    assert out[2]["time_seconds"] == pytest.approx(60.0)


def test_maneuver_shape_and_field_set():
    trip, _ = _multileg_trip()
    first = valhalla.trip_maneuvers(trip)[0]
    assert set(first) == {"instruction", "type", "street_names", "length_meters",
                          "time_seconds", "begin_shape_index", "end_shape_index"}
    assert first["instruction"] == "Head north on Champa Street"
    assert first["type"] == 1                # Valhalla's type passes through as-is
    assert first["street_names"] == ["Champa Street"]


def test_unnamed_way_yields_an_empty_street_name_list_not_null():
    """Clients render street_names directly; an alley simply has no name."""
    trip, _ = _multileg_trip()
    arrive = valhalla.trip_maneuvers(trip)[-1]
    assert "street_names" not in trip["legs"][3]["maneuvers"][0]
    assert arrive["street_names"] == []


def test_missing_length_or_time_stays_null():
    """A zero would read as an instantaneous, zero-length maneuver."""
    trip = {"legs": [{"shape": _enc(_LEG_A_PTS),
                      "maneuvers": [{"instruction": "Go", "type": 1,
                                     "begin_shape_index": 0, "end_shape_index": 1}]}]}
    out = valhalla.trip_maneuvers(trip)
    assert out[0]["length_meters"] is None
    assert out[0]["time_seconds"] is None


def test_single_leg_indices_pass_through_unchanged():
    trip = {"legs": [{"shape": _enc(_LEG_A_PTS),
                      "maneuvers": [{"instruction": "Go", "type": 1, "length": 1.0,
                                     "time": 2.0, "begin_shape_index": 1,
                                     "end_shape_index": 3}]}]}
    out = valhalla.trip_maneuvers(trip)
    assert (out[0]["begin_shape_index"], out[0]["end_shape_index"]) == (1, 3)


def test_a_leg_wholly_consumed_by_the_duplicate_drop_still_addresses_a_vertex():
    """The fourth offset case the multi-leg fixture above cannot reach.

    A leg whose ENTIRE shape is one vertex, and that vertex is the one the
    previous leg already emitted (Valhalla does this on a via point the router
    snapped onto the preceding leg's last shape point). The duplicate drop
    empties the leg, so it contributes zero new coordinates — yet it is NOT a
    shapeless leg: its local index 0 names a real vertex, the shared one.

    The distinction matters because the two adjacent bugs are silent. Treating
    it as shapeless would drop a legitimate "arrive at your 1st destination"
    cue; forgetting the `offset -= 1` would point that cue at the FOLLOWING
    leg's first vertex — an off-by-one that only ever appears at a via point.
    """
    tail = _LEG_A_PTS[-1]
    trip = {"legs": [
        {"shape": _enc(_LEG_A_PTS),
         "maneuvers": [{"instruction": "A", "type": 1, "length": 0.4, "time": 90.0,
                        "begin_shape_index": 0, "end_shape_index": 3}]},
        # One point, identical to leg A's last.
        {"shape": _enc([tail]),
         "maneuvers": [{"instruction": "VIA", "type": 5, "length": 0.0, "time": 0.0,
                        "begin_shape_index": 0, "end_shape_index": 0}]},
        {"shape": _enc(_LEG_D_PTS),
         "maneuvers": [{"instruction": "D", "type": 4, "length": 0.02, "time": 5.0,
                        "begin_shape_index": 0, "end_shape_index": 1}]},
    ]}

    points, offsets = valhalla.trip_shape_with_leg_offsets(trip)
    # The middle leg added nothing, so the flattened shape is A + D.
    assert points == _LEG_A_PTS + _LEG_D_PTS
    # ...but it still has an offset, and it is len(points_before) - 1.
    assert offsets == [0, 3, 4]

    out = valhalla.trip_maneuvers(trip)
    assert [m["instruction"] for m in out] == ["A", "VIA", "D"]
    by_name = {m["instruction"]: m for m in out}
    # The via cue addresses the shared vertex, not leg D's first point.
    assert points[by_name["VIA"]["begin_shape_index"]] == tail
    assert points[by_name["VIA"]["end_shape_index"]] == tail
    assert points[by_name["D"]["begin_shape_index"]] == _LEG_D_PTS[0]
    for m in out:
        assert 0 <= m["begin_shape_index"] <= m["end_shape_index"] < len(points)


# --- endpoint harness --------------------------------------------------------
#
# These go through TestClient rather than calling the handler directly: a
# direct call leaves `maneuvers` bound to its `Query(False)` marker object,
# which is TRUTHY, so "omitted unless asked for" is only testable over HTTP.

@pytest.fixture(autouse=True)
def _no_battery_model(monkeypatch):
    """Routes must serve before any model has been fit."""
    monkeypatch.setattr(
        api_route.battery_model, "estimate_burn_percent",
        lambda **kw: {"percent": None, "source": "unavailable", "reason": "no_model"},
    )


@pytest.fixture(autouse=True)
def _clear_canopy_cache():
    api_route._CANOPY = None
    yield
    api_route._CANOPY = None


class _FakeCursor:
    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return (0, None)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self):
        self.commits = 0

    def cursor(self):
        return _FakeCursor()

    def commit(self):
        self.commits += 1


def _app():
    app = FastAPI()
    app.include_router(api_route.router)
    return app


def _install(monkeypatch, enforce_impl=None):
    """Recording enforce() + a DB-free connection(). Returns the call list."""
    calls: list[dict] = []
    conns: list[_FakeConn] = []

    def fake_enforce(cur, **kw):
        # A misnamed kwarg would sail straight through a **kw fake and only
        # blow up in production, so bind the call against the real signature.
        inspect.signature(ratelimit.enforce).bind(cur, **kw)
        calls.append(kw)
        if enforce_impl is not None:
            enforce_impl(**kw)

    @contextmanager
    def fake_connection():
        conn = _FakeConn()
        conns.append(conn)
        yield conn

    monkeypatch.setattr(api_route, "enforce", fake_enforce)
    monkeypatch.setattr(api_route, "connection", fake_connection)
    return calls, conns


def _stub_valhalla(monkeypatch, trip):
    monkeypatch.setattr(api_route.valhalla, "route", lambda *a, **kw: {"trip": trip})


_QS = {"from": "39.74,-104.99", "to": "39.745,-104.99", "profile": "safe"}


# --- GET /api/v1/route?maneuvers= -------------------------------------------

def test_maneuvers_are_omitted_unless_asked_for(monkeypatch):
    """Opt-in: the route preview doesn't need them and they are not free."""
    _install(monkeypatch)
    trip, _ = _multileg_trip()
    _stub_valhalla(monkeypatch, trip)

    body = TestClient(_app()).get("/api/v1/route", params=_QS).json()
    assert "maneuvers" not in body["properties"]


def test_the_flag_default_is_a_real_false_not_a_query_marker():
    """`maneuvers: bool = Query(False)` defaults to the Query MARKER object,
    which is truthy — that form silently enables the passthrough (and the cost
    of decoding every leg) for any in-process caller. Hence the Annotated form."""
    assert inspect.signature(api_route.route).parameters["maneuvers"].default is False


def test_maneuvers_true_adds_indices_that_address_the_returned_linestring(monkeypatch):
    """The A1 acceptance criterion, end to end on a multi-leg route."""
    _install(monkeypatch)
    trip, _ = _multileg_trip()
    _stub_valhalla(monkeypatch, trip)

    body = TestClient(_app()).get(
        "/api/v1/route", params={**_QS, "maneuvers": "true"}).json()
    coords = body["geometry"]["coordinates"]
    mans = body["properties"]["maneuvers"]

    assert len(coords) == 8
    assert len(mans) == 4                      # the shapeless leg's cue is dropped
    for man in mans:
        for idx in (man["begin_shape_index"], man["end_shape_index"]):
            assert 0 <= idx < len(coords)
    # GeoJSON is (lon, lat). Leg B's cue begins on leg A's final vertex, which
    # is the vertex the flattening dropped from leg B.
    assert coords[mans[2]["begin_shape_index"]] == [_LEG_A_PTS[-1][1], _LEG_A_PTS[-1][0]]
    # Leg D's boundary does NOT repeat, so its cue begins on its own first
    # vertex — index 6, where a one-drop-per-join formula would have said 5.
    assert mans[3]["begin_shape_index"] == 6
    assert coords[mans[3]["begin_shape_index"]] == [_LEG_D_PTS[0][1], _LEG_D_PTS[0][0]]


def test_maneuvers_come_from_the_chosen_trip_not_the_first_one(monkeypatch):
    """On profile=shade the returned trip may be an alternate — the nav cues
    must describe the geometry actually returned."""
    _install(monkeypatch)
    plain, _ = _multileg_trip()
    shady, _ = _multileg_trip()
    plain["legs"][0]["maneuvers"][0]["instruction"] = "PLAIN"
    shady["legs"][0]["maneuvers"][0]["instruction"] = "SHADY"

    monkeypatch.setattr(api_route.valhalla, "route",
                        lambda *a, **kw: {"trip": plain, "alternates": [{"trip": shady}]})

    # The two trips have identical geometry, so score them by identity.
    monkeypatch.setattr(api_route, "shade_score",
                        lambda trip, costing_options, shape=None: 0.9 if trip is shady else 0.1)

    body = TestClient(_app()).get(
        "/api/v1/route",
        params={"from": "39.74,-104.99", "to": "39.745,-104.99",
                "profile": "shade", "maneuvers": "true"}).json()
    assert body["properties"]["maneuvers"][0]["instruction"] == "SHADY"


# --- per-IP rate limits ------------------------------------------------------

def test_route_is_limited_to_30_per_minute_per_ip(monkeypatch):
    calls, conns = _install(monkeypatch)
    trip, _ = _multileg_trip()
    _stub_valhalla(monkeypatch, trip)

    r = TestClient(_app()).get(
        "/api/v1/route", params={"from": "39.74,-104.99", "to": "39.745,-104.99"},
        headers={"CF-Connecting-IP": "203.0.113.9"})
    assert r.status_code == 200
    assert len(calls) == 1
    assert calls[0]["bucket"] == "route_ip"
    assert calls[0]["limit"] == 30
    assert calls[0]["window_seconds"] == 60
    # Keyed on the forwarded IP, not the cloudflared loopback.
    assert calls[0]["key"] == "203.0.113.9"
    # The recorded event has to commit or the bucket never fills.
    assert conns[0].commits == 1


def test_profiles_is_limited_to_60_per_minute_per_ip(monkeypatch):
    calls, _ = _install(monkeypatch)
    r = TestClient(_app()).get("/api/v1/route/profiles",
                               headers={"CF-Connecting-IP": "203.0.113.9"})
    assert r.status_code == 200
    assert len(calls) == 1
    assert calls[0]["bucket"] == "route_profiles_ip"
    assert calls[0]["limit"] == 60
    assert calls[0]["window_seconds"] == 60
    assert calls[0]["key"] == "203.0.113.9"


def test_unknown_ip_collapses_to_the_shared_bucket_key(monkeypatch):
    calls, _ = _install(monkeypatch)
    monkeypatch.setattr(api_route, "real_client_ip", lambda request: None)
    r = TestClient(_app()).get("/api/v1/route/profiles")
    assert r.status_code == 200
    assert calls[0]["key"] == "?"


def test_429_carries_retry_after(monkeypatch):
    def blow_up(**kw):
        raise HTTPException(429, detail="rate limit exceeded — try again later",
                            headers={"Retry-After": "37"})

    calls, _ = _install(monkeypatch, enforce_impl=blow_up)
    trip, _ = _multileg_trip()
    _stub_valhalla(monkeypatch, trip)

    r = TestClient(_app()).get(
        "/api/v1/route", params={"from": "39.74,-104.99", "to": "39.745,-104.99"})
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "37"


def test_the_limit_runs_even_when_the_request_is_malformed(monkeypatch):
    """Garbage input must still cost quota, or the bucket is trivial to dodge."""
    calls, _ = _install(monkeypatch)
    r = TestClient(_app()).get("/api/v1/route",
                               params={"from": "nonsense", "to": "39.745,-104.99"})
    assert r.status_code == 400
    assert [c["bucket"] for c in calls] == ["route_ip"]
