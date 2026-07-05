"""Point-in-polygon + region assignment for the report aggregates."""

from __future__ import annotations

import pytest

from src import boundaries, geo
from src.geo import geometry_contains, region_for_point, region_names

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
