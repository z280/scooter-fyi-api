"""Google encoded-polyline decode/encode (precision 5 by default) for ride
paths.

The legacy `rides` table only ever needed decode() — the client sends an
already-encoded polyline after the fact (§4.2), and decoding happens only
at GeoJSON export time. encode() was added for tracked_rides
(sql/027_tracked_rides.sql): waypoints arrive one at a time with
client-supplied timestamps and can arrive out of order, so
path_polyline is rebuilt from scratch (encode() over the full ordered
point list) on every waypoint insert rather than appended incrementally,
which would silently corrupt the path on any out-of-order POST.
Algorithm: https://developers.google.com/maps/documentation/utilities/polylinealgorithm
"""

from __future__ import annotations


class PolylineError(ValueError):
    pass


def decode(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decode to a list of (lat, lon) tuples. Raises PolylineError on a
    truncated or corrupt string."""
    factor = 10 ** precision
    coords: list[tuple[float, float]] = []
    index = lat = lon = 0
    length = len(encoded)

    def _next_delta() -> int:
        nonlocal index
        shift = result = 0
        while True:
            if index >= length:
                raise PolylineError("truncated polyline")
            b = ord(encoded[index]) - 63
            if b < 0 or b > 63:
                raise PolylineError(f"invalid polyline character at {index}")
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        return ~(result >> 1) if result & 1 else result >> 1

    while index < length:
        lat += _next_delta()
        lon += _next_delta()
        coords.append((lat / factor, lon / factor))
    return coords


def encode(points: list[tuple[float, float]], precision: int = 5) -> str:
    """Encode (lat, lon) tuples to a Google polyline string. Inverse of
    decode() — round-trips exactly at the given precision."""
    factor = 10 ** precision

    def _encode_delta(d: int) -> str:
        d = ~(d << 1) if d < 0 else (d << 1)
        chunks = []
        while d >= 0x20:
            chunks.append(chr((0x20 | (d & 0x1F)) + 63))
            d >>= 5
        chunks.append(chr(d + 63))
        return "".join(chunks)

    out: list[str] = []
    prev_lat = prev_lon = 0
    for lat, lon in points:
        lat_i = round(lat * factor)
        lon_i = round(lon * factor)
        out.append(_encode_delta(lat_i - prev_lat))
        out.append(_encode_delta(lon_i - prev_lon))
        prev_lat, prev_lon = lat_i, lon_i
    return "".join(out)
