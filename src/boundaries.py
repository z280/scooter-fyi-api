"""Load + cache boundary GeoJSON files for HTTP serving.

Source of truth for each layer's `region_name` derivation is the same
function used by the DuckDB compute path, so the polygons served here
match the rows in `regional_metrics_narrow` 1:1 by name.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any

from .compute import _name_from_props
from .config import BoundaryLayer, load

log = logging.getLogger(__name__)

_LOCK = Lock()
_CACHE: dict[str, dict[str, Any]] = {}


def _layer_by_type(region_type: str) -> BoundaryLayer | None:
    for b in load().boundaries:
        if b.region_type == region_type:
            return b
    return None


def _load_layer_geojson(layer: BoundaryLayer) -> dict[str, Any]:
    """Read the layer's GeoJSON file, apply the same filter + naming
    convention as the compute pipeline, and return a clean
    FeatureCollection with `id`, `properties.region_*`, and the original
    geometry."""
    path = Path(layer.file)
    if not path.exists():
        raise FileNotFoundError(f"boundary file missing: {path}")

    with path.open() as f:
        raw = json.load(f)

    features_out: list[dict] = []
    minx = miny = float("inf")
    maxx = maxy = float("-inf")

    for ordinal, feat in enumerate(raw.get("features", []), start=1):
        props = feat.get("properties") or {}
        # Apply the same filter the compute pipeline uses
        if layer.filter_nonnull_field and props.get(layer.filter_nonnull_field) is None:
            continue
        name = _name_from_props(layer, props, ordinal)
        geom = feat.get("geometry")
        if not geom:
            continue
        features_out.append({
            "type": "Feature",
            "id": name,
            "geometry": geom,
            "properties": {
                "region_category": layer.region_category,
                "region_type": layer.region_type,
                "region_name": name,
            },
        })
        # Track bbox cheaply by walking coordinates
        for x, y in _iter_coords(geom):
            if x < minx: minx = x
            if y < miny: miny = y
            if x > maxx: maxx = x
            if y > maxy: maxy = y

    bbox = None if minx == float("inf") else [minx, miny, maxx, maxy]

    return {
        "type": "FeatureCollection",
        "metadata": {
            "region_category": layer.region_category,
            "region_type": layer.region_type,
            "feature_count": len(features_out),
            "bbox": bbox,
        },
        "features": features_out,
    }


def _iter_coords(geom: dict):
    """Yield (x, y) pairs from any GeoJSON geometry."""
    t = geom.get("type")
    c = geom.get("coordinates")
    if c is None:
        return
    if t == "Point":
        yield c[0], c[1]
    elif t == "MultiPoint" or t == "LineString":
        for p in c:
            yield p[0], p[1]
    elif t == "MultiLineString" or t == "Polygon":
        for ring in c:
            for p in ring:
                yield p[0], p[1]
    elif t == "MultiPolygon":
        for poly in c:
            for ring in poly:
                for p in ring:
                    yield p[0], p[1]


def get_layer(region_type: str) -> dict[str, Any] | None:
    """Return the cached FeatureCollection for a layer. Loads on first
    access, then keeps in memory (boundaries don't change at runtime)."""
    if region_type in _CACHE:
        return _CACHE[region_type]
    layer = _layer_by_type(region_type)
    if not layer:
        return None
    with _LOCK:
        if region_type in _CACHE:  # double-check inside lock
            return _CACHE[region_type]
        log.info("loading boundary layer %s from %s", region_type, layer.file)
        fc = _load_layer_geojson(layer)
        _CACHE[region_type] = fc
        return fc


def list_layers() -> list[dict[str, Any]]:
    """Return summary metadata for every configured layer, loading any
    that haven't been read yet."""
    summaries: list[dict[str, Any]] = []
    for layer in load().boundaries:
        try:
            fc = get_layer(layer.region_type)
        except FileNotFoundError as e:
            log.warning("boundary layer %s unavailable: %s", layer.region_type, e)
            continue
        if not fc:
            continue
        meta = fc["metadata"]
        summaries.append({
            "region_category": meta["region_category"],
            "region_type": meta["region_type"],
            "feature_count": meta["feature_count"],
            "bbox": meta["bbox"],
            "url": f"/api/v1/boundaries/{layer.region_type}",
        })
    return summaries
