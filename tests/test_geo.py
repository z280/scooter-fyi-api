"""Point-in-polygon + region assignment for the report aggregates, plus
distance_meters (promoted from device_state.py — see
tests/test_device_state.py for the tests confirming its re-export under
that module's historical `_distance_meters` name behaves identically)."""

from __future__ import annotations

import math

import pytest

from src import boundaries, geo
from src.geo import distance_meters, geometry_contains, region_for_point, region_names

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
