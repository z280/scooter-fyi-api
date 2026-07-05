"""Google encoded-polyline decoding (precision 5) for ride exports.

Rides arrive and are stored as encoded polylines (§4.2); decoding happens
only at GeoJSON export time. ~25 lines beats a dependency.
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
