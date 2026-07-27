"""Tests for src/polyline.py's encode() — inverse of decode(), added for
tracked_rides (sql/027). Reuses the same canonical Google example
constants as tests/test_polyline.py."""

from __future__ import annotations

from src.polyline import decode, encode

_GOOGLE_EXAMPLE = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
_GOOGLE_POINTS = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]


def test_matches_the_google_documented_example():
    assert encode(_GOOGLE_POINTS) == _GOOGLE_EXAMPLE


def test_round_trips_through_decode():
    assert decode(encode(_GOOGLE_POINTS)) == _GOOGLE_POINTS


def test_empty_point_list_encodes_to_empty_string():
    assert encode([]) == ""


def test_single_point_at_origin():
    assert encode([(0.0, 0.0)]) == "??"


def test_single_point_round_trips():
    points = [(39.73924, -104.99025)]  # 5 decimal places, matching the default precision
    assert decode(encode(points)) == points


def test_negative_deltas_round_trip():
    points = [(10.0, 10.0), (5.0, 5.0), (10.0, 10.0)]
    assert decode(encode(points)) == points


def test_precision_6_round_trips_and_differs_from_precision_5():
    points = [(39.739236, -104.990251), (39.740001, -104.991000)]
    encoded_5 = encode(points, precision=5)
    encoded_6 = encode(points, precision=6)
    assert encoded_5 != encoded_6
    assert decode(encoded_6, precision=6) == points
    # precision 5 loses the 6th decimal digit (~1cm) — round-trip is
    # close but not exact at that precision.
    decoded_5 = decode(encoded_5, precision=5)
    for (lat, lon), (dlat, dlon) in zip(points, decoded_5):
        assert abs(lat - dlat) < 1e-4
        assert abs(lon - dlon) < 1e-4
