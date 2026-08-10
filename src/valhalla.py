"""Thin HTTP client for the Valhalla routing container.

Valhalla runs alongside this service on the compose network with no host port;
it is never exposed publicly — riders reach it only through /api/v1/route.

Two endpoints are used:

* ``/route``             — bicycle routing, optionally with alternates.
* ``/trace_attributes``  — snap a shape back onto the graph to recover the
                           OSM way ids it traverses. That is how shade is scored
                           (§2C) and how ride GPS traces are checked for
                           adherence (§3G); Valhalla exposes no way id on a
                           plain route response.

Responses use Valhalla's native JSON rather than ``format: "osrm"``: the native
shape is an encoded polyline at precision 6, which ``src/polyline.py`` already
decodes, and it keeps the ``summary``/``elevation`` fields the battery model
needs. ``directions_type: "geojson"`` is not a real Valhalla option — GeoJSON is
assembled here from the decoded shape.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import load
from .polyline import decode as decode_polyline

log = logging.getLogger(__name__)

# Valhalla error codes we handle specifically.
# See valhalla/docs/api/turn-by-turn — 171 is raised when no routable edge sits
# within the search radius, which happens often here because denver-map-prep
# tags High Injury Network ways bicycle=no.
ERR_NO_SUITABLE_EDGES = {171, 172}
ERR_NO_PATH = {442}


class ValhallaError(RuntimeError):
    """A Valhalla request failed in a way the caller should translate."""

    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status

    @property
    def no_suitable_edges(self) -> bool:
        return self.code in ERR_NO_SUITABLE_EDGES

    @property
    def no_path(self) -> bool:
        return self.code in ERR_NO_PATH


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    cfg = load().valhalla
    url = f"{cfg.base_url.rstrip('/')}{path}"
    try:
        resp = httpx.post(url, json=payload, timeout=cfg.timeout_seconds)
    except httpx.HTTPError as exc:
        raise ValhallaError(f"valhalla unreachable at {url}: {exc}") from exc

    if resp.status_code >= 400:
        code, message = None, resp.text[:500]
        try:
            body = resp.json()
            code = body.get("error_code")
            message = body.get("error", message)
        except ValueError:
            pass
        raise ValhallaError(message, code=code, status=resp.status_code)
    return resp.json()


def _locations(points: list[tuple[float, float]], radius: int | None) -> list[dict[str, Any]]:
    out = []
    for lat, lon in points:
        loc: dict[str, Any] = {"lat": lat, "lon": lon}
        if radius:
            loc["radius"] = radius
        out.append(loc)
    return out


def route(points: list[tuple[float, float]],
          costing_options: dict[str, Any],
          alternates: int = 0,
          radius: int | None = None,
          with_elevation: bool = True) -> dict[str, Any]:
    """Request a bicycle route through ``points`` (list of (lat, lon))."""
    cfg = load().valhalla
    payload: dict[str, Any] = {
        "locations": _locations(points, radius),
        "costing": "bicycle",
        "costing_options": {"bicycle": dict(costing_options)},
        # Kilometres throughout; the shade score is a length-weighted ratio so
        # units cancel there, and distances are converted to metres on output.
        "directions_options": {"units": "kilometers"},
    }
    if alternates:
        payload["alternates"] = alternates
    if with_elevation:
        payload["elevation_interval"] = cfg.elevation_interval
    return _post("/route", payload)


#: What `trace_attributes` asks for when the caller doesn't say. Way id and
#: length are what shade scoring needs; everything else is wasted payload.
DEFAULT_TRACE_ATTRIBUTES = ("edge.way_id", "edge.length")


def trace_attributes(shape: list[tuple[float, float]],
                     costing_options: dict[str, Any],
                     shape_match: str = "walk_or_snap",
                     attributes: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """Snap ``shape`` onto the graph and return its edges.

    ``walk_or_snap`` tries the cheap exact edge walk first and falls back to map
    matching. Plain ``edge_walk`` is not safe even for shapes Valhalla itself
    produced — the returned polyline is quantised to 6 decimal places, and on
    longer routes that rounding is enough to make the exact match fail outright
    ("edge_walk algorithm failed to find exact route match"). ``map_snap`` is
    the right choice for raw GPS breadcrumbs.

    Only way id and length are requested by default; ``attributes`` overrides
    that for a caller needing more (the night profile wants ``edge.use`` to tell
    a street from an off-street path).
    """
    payload = {
        "shape": [{"lat": lat, "lon": lon} for lat, lon in shape],
        "costing": "bicycle",
        "costing_options": {"bicycle": dict(costing_options)},
        "shape_match": shape_match,
        "directions_options": {"units": "kilometers"},
        "filters": {
            "attributes": list(attributes or DEFAULT_TRACE_ATTRIBUTES),
            "action": "include",
        },
    }
    body = _post("/trace_attributes", payload)
    return body.get("edges", []) or []


def status() -> dict[str, Any]:
    """Liveness/version probe used by the health endpoint."""
    cfg = load().valhalla
    url = f"{cfg.base_url.rstrip('/')}/status"
    resp = httpx.get(url, timeout=cfg.timeout_seconds)
    resp.raise_for_status()
    return resp.json()


# --- Response helpers --------------------------------------------------------

def trip_shape_with_leg_offsets(
    trip: dict[str, Any],
) -> tuple[list[tuple[float, float]], list[int | None]]:
    """Flatten every leg's shape AND report each leg's index offset.

    Returns ``(points, offsets)`` where ``points`` is the concatenated
    (lat, lon) shape and ``offsets[i]`` is the number to add to a leg-i-local
    shape index (Valhalla's ``begin_shape_index`` / ``end_shape_index`` are
    per-leg) to address the same vertex in ``points``. ``offsets[i]`` is None
    when leg i contributed nothing at all, so its local indices address no
    coordinate in the flattened shape.

    Shape flattening and offset computation MUST happen in one pass: the
    duplicated vertex between consecutive legs is dropped **conditionally**
    (only when the boundary vertex actually repeats) and legs with no shape are
    skipped entirely, so "one dropped vertex per join" is wrong. Two separate
    passes would eventually disagree and silently misplace every turn cue —
    hence ``trip_shape()`` is a thin wrapper over this function rather than its
    own loop.

    Valhalla encodes route shapes at precision 6, not the precision 5 used for
    stored ride polylines — hence the explicit argument.
    """
    points: list[tuple[float, float]] = []
    offsets: list[int | None] = []
    for leg in trip.get("legs", []) or []:
        encoded = leg.get("shape")
        if not encoded:
            offsets.append(None)
            continue
        leg_points = decode_polyline(encoded, precision=6)
        if not leg_points:
            offsets.append(None)
            continue
        offset = len(points)
        # Consecutive legs repeat the shared vertex; drop the duplicate.
        if points and points[-1] == leg_points[0]:
            leg_points = leg_points[1:]
            # This leg's local index 0 is the vertex already emitted as
            # points[-1], so its indices shift one FURTHER back.
            offset -= 1
        offsets.append(offset)
        points.extend(leg_points)
    return points, offsets


def trip_shape(trip: dict[str, Any]) -> list[tuple[float, float]]:
    """Decode and concatenate every leg's shape into (lat, lon) pairs."""
    points, _ = trip_shape_with_leg_offsets(trip)
    return points


def elevation_gain_meters(trip: dict[str, Any]) -> float | None:
    """Total positive elevation change along the trip, in metres.

    Valhalla returns sampled heights per leg (``elevation``) rather than a gain
    summary, so the ascent is integrated here. Returns None when the graph was
    built without elevation data.
    """
    legs = trip.get("legs", []) or []
    if not legs:
        return None
    total = 0.0
    for leg in legs:
        samples = leg.get("elevation")
        if not samples:
            # A partially-elevated response would silently UNDERCOUNT the climb,
            # which the battery model would read as a flat route. Unknown is the
            # honest answer; the caller treats None and 0.0 very differently.
            return None
        for prev, cur in zip(samples, samples[1:]):
            if prev is None or cur is None:
                continue
            delta = cur - prev
            if delta > 0:
                total += delta
    return round(total, 1)


def trip_summary(trip: dict[str, Any]) -> dict[str, Any]:
    summary = trip.get("summary", {}) or {}
    length_km = summary.get("length")
    return {
        "distance_meters": round(length_km * 1000.0, 1) if length_km is not None else None,
        "duration_seconds": round(summary.get("time", 0.0), 1),
        "elevation_gain_meters": elevation_gain_meters(trip),
    }


def trip_maneuvers(trip: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn-by-turn maneuvers, flattened across legs, for the nav HUD.

    Shape indices are rewritten to address the flattened shape returned by
    ``trip_shape()`` — Valhalla numbers them per leg, and the flattening drops
    duplicated boundary vertices, so the raw values would misplace turn cues on
    any multi-leg route. Maneuvers belonging to a leg that contributed no shape
    are dropped: an index that addresses no coordinate is worse than a missing
    instruction.

    ``length`` arrives in KILOMETRES (``route()`` pins
    ``directions_options.units: "kilometers"``) and is converted to metres the
    same way ``trip_summary()`` converts ``summary.length``; ``time`` is already
    seconds. Both stay None when Valhalla omitted them rather than reading as a
    zero-length, instantaneous maneuver.
    """
    _, offsets = trip_shape_with_leg_offsets(trip)
    out: list[dict[str, Any]] = []
    # strict=: offsets carries exactly one entry per leg, including the skipped
    # ones — a mismatch would mean the helper stopped pairing 1:1 with legs and
    # every index after the gap would be silently wrong.
    for leg, offset in zip(trip.get("legs", []) or [], offsets, strict=True):
        if offset is None:
            continue
        for man in leg.get("maneuvers", []) or []:
            length_km = man.get("length")
            time_s = man.get("time")
            begin = man.get("begin_shape_index")
            end = man.get("end_shape_index")
            out.append({
                "instruction": man.get("instruction"),
                "type": man.get("type"),
                # Always a list: an unnamed way (alley, cycleway link) simply
                # has no street_names, and clients render the list directly.
                "street_names": list(man.get("street_names") or []),
                "length_meters": round(length_km * 1000.0, 1) if length_km is not None else None,
                "time_seconds": round(time_s, 1) if time_s is not None else None,
                "begin_shape_index": begin + offset if begin is not None else None,
                "end_shape_index": end + offset if end is not None else None,
            })
    return out


def to_geojson(points: list[tuple[float, float]]) -> dict[str, Any]:
    """GeoJSON LineString from (lat, lon) pairs — note GeoJSON is (lon, lat)."""
    return {
        "type": "LineString",
        "coordinates": [[lon, lat] for lat, lon in points],
    }


def all_trips(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The primary trip plus any alternates, in Valhalla's ranking order."""
    trips = []
    if body.get("trip"):
        trips.append(body["trip"])
    for alt in body.get("alternates", []) or []:
        if alt.get("trip"):
            trips.append(alt["trip"])
    return trips
