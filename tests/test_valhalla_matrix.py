"""The /sources_to_targets wrapper (frontend plan Phase 2 §2.3).

Mocked at `_post`, because the whole content of `matrix()` is the payload it
builds: which endpoint, which costing, which units, and what it deliberately
does NOT ask for. A live-Valhalla test would exercise Valhalla.
"""

import pytest

from src import valhalla


@pytest.fixture
def captured(monkeypatch):
    seen: dict = {}

    def fake_post(path, payload):
        seen["path"] = path
        seen["payload"] = payload
        return {"sources_to_targets": []}

    monkeypatch.setattr(valhalla, "_post", fake_post)
    return seen


SRC = [(39.75, -104.99), (39.76, -104.98)]
DST = [(39.77, -104.97), (39.78, -104.96), (39.79, -104.95)]


def test_it_asks_sources_to_targets(captured):
    valhalla.matrix(SRC, DST, {})
    assert captured["path"] == "/sources_to_targets"


def test_it_sends_sources_and_targets_separately(captured):
    # Not a flat `locations` list: a matrix request that collapses the two sides
    # into one list is an all-pairs request, which is a different (and larger)
    # question than the planner asks.
    valhalla.matrix(SRC, DST, {})
    p = captured["payload"]
    assert len(p["sources"]) == 2
    assert len(p["targets"]) == 3
    assert "locations" not in p
    assert p["sources"][0] == {"lat": 39.75, "lon": -104.99}


def test_it_walks_by_default(captured):
    # The first caller's question is "how far to each candidate scooter".
    valhalla.matrix(SRC, DST, {})
    assert captured["payload"]["costing"] == "pedestrian"


def test_it_can_ride_instead(captured):
    # Riding legs use the same mode `route` will later draw the shape with, so
    # the ranked figure and the displayed route come from one costing.
    valhalla.matrix(SRC, DST, {"use_roads": 0.2}, costing="bicycle")
    p = captured["payload"]
    assert p["costing"] == "bicycle"
    assert p["costing_options"] == {"bicycle": {"use_roads": 0.2}}


def test_it_copies_the_costing_options(captured):
    # The caller's dict must not be aliased into the payload — a profile's
    # options are shared config, and a mutation here would leak across requests.
    opts = {"use_roads": 0.2}
    valhalla.matrix(SRC, DST, opts, costing="bicycle")
    captured["payload"]["costing_options"]["bicycle"]["use_roads"] = 99
    assert opts["use_roads"] == 0.2


def test_it_does_not_pay_for_elevation_or_maneuvers(captured):
    # A matrix returns scalars. There is no shape to sample and no maneuvers to
    # generate, so asking would buy graph work for fields that never come back.
    valhalla.matrix(SRC, DST, {})
    p = captured["payload"]
    assert "elevation_interval" not in p
    assert "alternates" not in p
    assert p["directions_options"] == {"units": "kilometers"}


def test_radius_is_applied_to_both_sides(captured):
    valhalla.matrix(SRC, DST, {}, radius=35)
    p = captured["payload"]
    assert all(loc["radius"] == 35 for loc in p["sources"])
    assert all(loc["radius"] == 35 for loc in p["targets"])


# --- matrix_pairs ----------------------------------------------------------


def _resp(cells):
    return {"sources_to_targets": cells}


def test_pairs_convert_km_to_metres():
    pairs = valhalla.matrix_pairs(
        _resp([[{"from_index": 0, "to_index": 1, "time": 300, "distance": 1.25}]])
    )
    assert pairs == {(0, 1): (300.0, 1250.0)}


def test_unreachable_pairs_are_omitted_not_zeroed():
    # THE ONE THAT MATTERS. Valhalla nulls time/distance for a pair it could not
    # connect. Reading that as "no distance" would rank a scooter across a river
    # as the nearest one.
    pairs = valhalla.matrix_pairs(
        _resp([[
            {"from_index": 0, "to_index": 0, "time": None, "distance": None},
            {"from_index": 0, "to_index": 1, "time": 120, "distance": 0.4},
        ]])
    )
    assert (0, 0) not in pairs
    assert pairs[(0, 1)] == (120.0, 400.0)


def test_pairs_use_valhallas_own_indices():
    # Not enumeration order: the two agree only while the response is dense,
    # and nothing documents that it must be.
    pairs = valhalla.matrix_pairs(
        _resp([[
            {"from_index": 1, "to_index": 2, "time": 60, "distance": 0.1},
            {"from_index": 0, "to_index": 0, "time": 90, "distance": 0.2},
        ]])
    )
    assert set(pairs) == {(1, 2), (0, 0)}


def test_pairs_tolerate_an_empty_or_missing_body():
    assert valhalla.matrix_pairs({}) == {}
    assert valhalla.matrix_pairs({"sources_to_targets": None}) == {}
    assert valhalla.matrix_pairs(_resp([])) == {}


def test_pairs_skip_junk_cells():
    pairs = valhalla.matrix_pairs(
        _resp([[None, "nope", {"from_index": 0, "to_index": 0, "time": 1, "distance": 0.0}]])
    )
    assert pairs == {(0, 0): (1.0, 0.0)}


def test_pairs_accept_a_bare_cell_row():
    pairs = valhalla.matrix_pairs(
        _resp([{"from_index": 0, "to_index": 0, "time": 10, "distance": 0.02}])
    )
    assert pairs == {(0, 0): (10.0, 20.0)}


# --- status(verbose) -------------------------------------------------------


def test_verbose_status_sends_a_json_BOOLEAN_in_the_body(captured):
    # `?verbose=true` sends the STRING "true", which Valhalla's option parser
    # does not accept as a boolean — the flag was silently ignored, so the
    # matrix pre-flight could never answer the question it exists to ask.
    valhalla.status(verbose=True)
    assert captured["path"] == "/status"
    assert captured["payload"] == {"verbose": True}
    # A real bool, not the string the query-string form would have sent.
    assert isinstance(captured["payload"]["verbose"], bool)


def test_verbose_status_surfaces_errors_as_ValhallaError(monkeypatch):
    # Going through `_post` also brings this call under the module's own error
    # type, instead of letting an httpx.HTTPStatusError escape.
    def boom(path, payload):
        raise valhalla.ValhallaError("nope", code=1, status=400)

    monkeypatch.setattr(valhalla, "_post", boom)
    try:
        valhalla.status(verbose=True)
    except valhalla.ValhallaError:
        pass
    else:
        raise AssertionError("expected ValhallaError")


def test_status_asks_for_available_actions_only_when_verbose(monkeypatch):
    seen = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"version": "3.5.1"}

    def fake_get(url, params=None, timeout=None):
        seen["params"] = params
        return FakeResp()

    monkeypatch.setattr(valhalla.httpx, "get", fake_get)

    valhalla.status()
    assert seen["params"] is None, "the health probe must not pay for a tile walk"

    # ...and the verbose call does not go through GET at all any more.
