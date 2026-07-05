"""Encoded-polyline decoding for ride GeoJSON exports."""

from __future__ import annotations

import pytest

from src.polyline import PolylineError, decode

# The worked example from Google's polyline algorithm documentation.
_GOOGLE_EXAMPLE = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
_GOOGLE_POINTS = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]


def test_google_documented_example():
    assert decode(_GOOGLE_EXAMPLE) == _GOOGLE_POINTS


def test_empty_string_decodes_to_no_points():
    assert decode("") == []


def test_single_point():
    # 0,0 encodes to "??"
    assert decode("??") == [(0.0, 0.0)]


def test_truncated_input_raises():
    with pytest.raises(PolylineError):
        decode(_GOOGLE_EXAMPLE[:-1] + "\x7f")  # continuation bit never terminates


def test_out_of_range_character_raises():
    with pytest.raises(PolylineError):
        decode("_p~iF~ps|U\x1f")


def test_odd_delta_count_raises():
    """A lat delta with no matching lon delta is corrupt."""
    with pytest.raises(PolylineError):
        decode("_p~iF")
