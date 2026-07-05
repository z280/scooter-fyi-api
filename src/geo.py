"""Pure-Python point-in-polygon against the baked-in boundary GeoJSON.

Used by the report aggregates (§3.3) to assign report coordinates to
regions at query time. The per-cycle device spatial join stays in DuckDB
(src/compute.py) — that path handles ~6k points against all five layers
every 10 minutes. This path handles a few thousand report points against
ONE layer on a cached 10-minute cadence, where spinning up a DuckDB
session per web request would cost more than the ray-cast itself.

Even-odd ray casting with a per-feature bbox prefilter. Handles Polygon
(exterior ring minus holes) and MultiPolygon. Points exactly on an edge
may land on either side — irrelevant at report-coordinate precision.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from . import boundaries


def _ring_contains(ring: list[list[float]], lon: float, lat: float) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _polygon_contains(coords: list, lon: float, lat: float) -> bool:
    """coords = [exterior_ring, hole_ring, ...]"""
    if not coords or not _ring_contains(coords[0], lon, lat):
        return False
    return not any(_ring_contains(hole, lon, lat) for hole in coords[1:])


def geometry_contains(geom: dict[str, Any], lon: float, lat: float) -> bool:
    gtype = geom.get("type")
    if gtype == "Polygon":
        return _polygon_contains(geom["coordinates"], lon, lat)
    if gtype == "MultiPolygon":
        return any(_polygon_contains(p, lon, lat) for p in geom["coordinates"])
    return False


def _bbox(geom: dict[str, Any]) -> tuple[float, float, float, float]:
    lons: list[float] = []
    lats: list[float] = []

    def walk(c):
        if isinstance(c[0], (int, float)):
            lons.append(c[0])
            lats.append(c[1])
        else:
            for sub in c:
                walk(sub)

    walk(geom["coordinates"])
    return min(lons), min(lats), max(lons), max(lats)


@lru_cache(maxsize=8)
def _indexed_layer(region_type: str) -> tuple[tuple[str, tuple[float, float, float, float], str], ...]:
    """(region_name, bbox, feature_index) per feature — bbox prefilter data.

    Geometries stay in the boundaries module's cached FeatureCollection;
    we only index into it (third element) to avoid duplicating coordinate
    arrays in memory.
    """
    fc = boundaries.get_layer(region_type)
    if fc is None:
        raise KeyError(f"unknown boundary layer: {region_type}")
    out = []
    for i, feat in enumerate(fc["features"]):
        out.append((feat["properties"]["region_name"], _bbox(feat["geometry"]), str(i)))
    return tuple(out)


def region_for_point(region_type: str, lon: float, lat: float) -> str | None:
    """The region_name containing (lon, lat), or None. First match wins
    (layers are non-overlapping by construction)."""
    fc = boundaries.get_layer(region_type)
    if fc is None:
        raise KeyError(f"unknown boundary layer: {region_type}")
    for name, (min_lon, min_lat, max_lon, max_lat), idx in _indexed_layer(region_type):
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            continue
        if geometry_contains(fc["features"][int(idx)]["geometry"], lon, lat):
            return name
    return None


def region_names(region_type: str) -> list[str]:
    """Every region_name in a layer — the aggregate endpoints emit all of
    them (zero-filled), matching the spatial-snapshot convention."""
    return [name for name, _, _ in _indexed_layer(region_type)]
