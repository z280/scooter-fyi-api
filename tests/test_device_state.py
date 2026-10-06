"""Distance math + state-transition logic for the per-scooter tracker.

The DB-touching path (update_for_cycle) needs a real Postgres and is
exercised in the live container. These tests cover the pure-function
distance computation and confirm the threshold default.
"""

from __future__ import annotations

import math

from src import device_state
from src.config import load


def test_distance_zero_for_same_point():
    d = device_state._distance_meters(39.74, -104.99, 39.74, -104.99)
    assert d == 0.0


def test_distance_one_degree_latitude_is_111km():
    # 1 degree of latitude ≈ 111,320 m, regardless of longitude
    d = device_state._distance_meters(39.0, -105.0, 40.0, -105.0)
    assert math.isclose(d, 111_320.0, rel_tol=1e-3)


def test_distance_at_denver_latitude_for_small_displacement():
    # ~10 m north of (39.74, -104.99) — should compute close to 10 m
    delta_lat = 10.0 / 111_320.0
    d = device_state._distance_meters(39.74, -104.99, 39.74 + delta_lat, -104.99)
    assert math.isclose(d, 10.0, rel_tol=1e-3)


def test_distance_at_denver_latitude_for_east_displacement():
    # 1 degree of longitude at 39.74° N ≈ 85,479 m
    expected = 111_320.0 * math.cos(math.radians(39.74))
    d = device_state._distance_meters(39.74, -104.99, 39.74, -104.99 + 1.0)
    assert math.isclose(d, expected, rel_tol=1e-3)


def test_threshold_is_the_standardised_25m():
    """The one ring that was standardised, and the one that must not drift.

    16 m until 2026-10-06. It is what `rentals_no_go` counts against and what
    the dwell clock resets on, and sql/072's validation — the 9.1% headline —
    was computed at 25 m while the ingest counted at 16 m. The two now agree.

    This is deliberately NOT the same number as `IN_PLACE_RADIUS_M` or
    `JITTER_RADIUS_M` (50 m), which answer a different question and carry
    their own measured basis; `device_state.py` says why at length.
    """
    assert load().device_tracking.stationary_threshold_meters == 25.0


def test_distance_just_under_and_over_threshold():
    """Just inside the threshold is 'stationary'; just outside it is 'moved'.

    Reads the configured value rather than restating it, so the standardisation
    only has to be changed in one place.
    """
    threshold = load().device_tracking.stationary_threshold_meters
    for meters, expected_moved in [(threshold - 0.1, False), (threshold + 0.1, True)]:
        delta_lat = meters / 111_320.0
        d = device_state._distance_meters(39.74, -104.99, 39.74 + delta_lat, -104.99)
        assert (d > threshold) is expected_moved, f"at {meters}m: got {d}"
