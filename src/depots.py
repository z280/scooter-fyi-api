"""Operator depots (docs/SERVICING_PLAN.md 1b), from data/depots.json.

A depot is where the operator takes vehicles off the street: to charge, to
repair, or for good. It shows up in the public feed because vehicles keep
reporting their position inside it. One depot is known (found 2026-10-10);
`python -m src.cli discover_depots` lists candidate others for a human to add.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .geo import distance_meters

_PATH = Path(__file__).resolve().parent.parent / "data" / "depots.json"


@lru_cache(maxsize=1)
def depots() -> tuple[dict[str, Any], ...]:
    return tuple(json.loads(_PATH.read_text())["depots"])


def depot_at(lat: float | None, lon: float | None) -> str | None:
    """The id of the depot whose geofence contains (lat, lon), else None.
    A cheap bounding check first: this runs for every vehicle every cycle."""
    if lat is None or lon is None:
        return None
    lat, lon = float(lat), float(lon)
    for d in depots():
        # ~0.01 deg is >= 850 m at Denver's latitude, well past any radius.
        if abs(lat - d["lat"]) > 0.01 or abs(lon - d["lon"]) > 0.013:
            continue
        if distance_meters(lat, lon, d["lat"], d["lon"]) <= d["radius_m"]:
            return d["id"]
    return None


def sql_inside(lat: str, lon: str) -> str:
    """A SQL predicate: (lat, lon) is inside some depot geofence."""
    parts = [f"geo_distance_m({lat}, {lon}, {d['lat']}, {d['lon']}) <= {d['radius_m']}"
             for d in depots()]
    return "(" + " OR ".join(parts) + ")" if parts else "FALSE"
