"""Point-in-polygon + region assignment for the report aggregates, plus
distance_meters (promoted from device_state.py — see
tests/test_device_state.py for the tests confirming its re-export under
that module's historical `_distance_meters` name behaves identically)."""

from __future__ import annotations

import math

import pytest

from src import boundaries, geo
from src.geo import (
    distance_meters,
    geometry_contains,
    path_length_meters,
    region_for_point,
    region_names,
)

# Unit square with a hole in the middle quarter.
_SQUARE_WITH_HOLE = {
    "type": "Polygon",
    "coordinates": [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]],
    ],
}

_MULTI = {
    "type": "MultiPolygon",
    "coordinates": [
        [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
        [[[5, 5], [7, 5], [7, 7], [5, 7], [5, 5]]],
    ],
}


def test_polygon_contains_interior_point():
    assert geometry_contains(_SQUARE_WITH_HOLE, 1.0, 1.0)


def test_polygon_excludes_exterior_point():
    assert not geometry_contains(_SQUARE_WITH_HOLE, 11.0, 5.0)


def test_polygon_excludes_point_in_hole():
    assert not geometry_contains(_SQUARE_WITH_HOLE, 5.0, 5.0)


def test_multipolygon_hits_either_part():
    assert geometry_contains(_MULTI, 1.0, 1.0)
    assert geometry_contains(_MULTI, 6.0, 6.0)
    assert not geometry_contains(_MULTI, 3.5, 3.5)


def test_unsupported_geometry_is_never_contained():
    assert not geometry_contains({"type": "Point", "coordinates": [1, 1]}, 1.0, 1.0)


# ---------- region assignment against a fake layer ---------------------------
_FAKE_FC = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"region_name": "NB_West"},
            "geometry": {"type": "Polygon",
                         "coordinates": [[[0, 0], [5, 0], [5, 10], [0, 10], [0, 0]]]},
        },
        {
            "type": "Feature",
            "properties": {"region_name": "NB_East"},
            "geometry": {"type": "Polygon",
                         "coordinates": [[[5, 0], [10, 0], [10, 10], [5, 10], [5, 0]]]},
        },
    ],
}


@pytest.fixture
def fake_layer(monkeypatch):
    monkeypatch.setattr(
        boundaries, "get_layer",
        lambda rt: _FAKE_FC if rt == "fake" else None,
    )
    geo._indexed_layer.cache_clear()
    yield
    geo._indexed_layer.cache_clear()


def test_region_for_point_assigns_correct_region(fake_layer):
    assert region_for_point("fake", 2.0, 5.0) == "NB_West"
    assert region_for_point("fake", 8.0, 5.0) == "NB_East"


def test_region_for_point_none_outside_all_regions(fake_layer):
    assert region_for_point("fake", 20.0, 20.0) is None


def test_region_names_enumerates_layer(fake_layer):
    assert region_names("fake") == ["NB_West", "NB_East"]


def test_unknown_layer_raises(fake_layer):
    with pytest.raises(KeyError):
        region_for_point("nope", 1.0, 1.0)


# ---------- distance_meters --------------------------------------------------

def test_distance_zero_for_same_point():
    assert distance_meters(39.74, -104.99, 39.74, -104.99) == 0.0


def test_distance_one_degree_latitude_is_111km():
    d = distance_meters(39.0, -105.0, 40.0, -105.0)
    assert math.isclose(d, 111_320.0, rel_tol=1e-3)


def test_distance_within_20m_threshold_used_by_gbfs_trip_validation():
    """src/points.py:credit_gbfs_validation_points pays a bonus when the
    GBFS reappearance is within 20m of the reported end location — sanity
    check the boundary at that specific scale."""
    delta_lat = 19.0 / 111_320.0
    d = distance_meters(39.74, -104.99, 39.74 + delta_lat, -104.99)
    assert d < 20.0
    delta_lat = 21.0 / 111_320.0
    d = distance_meters(39.74, -104.99, 39.74 + delta_lat, -104.99)
    assert d > 20.0


# ---------- path_length_meters -----------------------------------------------

def test_path_length_empty_and_single_point_are_zero():
    assert path_length_meters([]) == 0.0
    assert path_length_meters([(39.74, -104.99)]) == 0.0


def test_path_length_sums_consecutive_legs():
    step = 100.0 / 111_320.0  # ~100 m of latitude
    pts = [(39.74 + i * step, -104.99) for i in range(4)]  # 3 legs
    assert math.isclose(path_length_meters(pts), 300.0, rel_tol=1e-6)


def test_path_length_counts_backtracking_rather_than_displacement():
    """A rider who goes out and comes back rode the whole way. Distance is
    path length, not start->end displacement — the property that makes the
    waypoint measurement worth more than the straight-line fallback."""
    step = 100.0 / 111_320.0
    out_and_back = [(39.74, -104.99), (39.74 + step, -104.99), (39.74, -104.99)]
    assert math.isclose(path_length_meters(out_and_back), 200.0, rel_tol=1e-6)
    assert distance_meters(*out_and_back[0], *out_and_back[-1]) == 0.0


def test_path_length_never_below_straight_line_between_endpoints():
    """The triangle inequality, stated as the invariant badges rely on:
    the straight-line fallback can only ever UNDERcount, never overcount."""
    pts = [(39.74, -104.99), (39.75, -104.97), (39.73, -104.95), (39.76, -104.94)]
    assert path_length_meters(pts) >= distance_meters(*pts[0], *pts[-1])
