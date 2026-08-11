"""Serving the rider a distance and a band, not a bare percentage.

The reported charge is the least trustworthy number the feed publishes -
frozen while parked, sagging under load, optimistic at rest - so the map and
route payloads translate it into what a rider actually needs: roughly how far
this goes, how much to trust the reading, and how much of a route's cost is
the hill.
"""

from __future__ import annotations

import pytest

from src import battery_model as bm


# --- usable range ------------------------------------------------------------

def test_range_comes_from_observed_discharges_not_the_regression():
    """Inverting beta_distance gives ~113 km on a full charge, on a fleet rated
    67 km that derates hard in service. The slope describes burn WITHIN a ride,
    where the intercept absorbs a large fixed offset; extrapolating it to a
    whole battery is not what it measures.

    The constant here is measured instead: 210 vehicles followed from >=95% to
    <=5% SoC, median 32.7 km of routed distance, i.e. 36.4 km per 100 points."""
    assert bm.OBSERVED_METERS_PER_SOC_POINT == pytest.approx(364.0)
    full = bm.usable_range_meters(100)
    assert 30_000 < full < 45_000, "a full charge should read like a real charge"
    # ...and nothing like the regression's extrapolation.
    from_slope = 100.0 / (0.000885 * 1000) * 1000
    assert full < from_slope / 2


def test_range_scales_and_floors():
    assert bm.usable_range_meters(50) == bm.usable_range_meters(100) // 2
    assert bm.usable_range_meters(0) == 0
    assert bm.usable_range_meters(None) is None
    assert bm.usable_range_meters(-5) == 0     # a negative charge is not a hole


# --- reading confidence ------------------------------------------------------

def test_a_long_parked_reading_is_flagged_stale():
    """99.4% of parked 2-minute steps show no change: the value is frozen, so
    its age is the only guide to whether it still means anything. Vehicles
    parked over an hour show measurably more apparent burn per km (1.81 pp/km
    under 15 min vs 2.73 pp/km beyond 12 h, at the same distances)."""
    assert bm.reading_confidence(30 * 60) == "fresh"
    assert bm.reading_confidence(6 * 3600) == "stale"
    assert bm.reading_confidence(None) == "unknown"


def test_staleness_threshold_matches_what_the_model_will_train_on():
    """One definition of 'fresh'. If serving and training disagreed, the app
    would show a reading the model itself refuses to learn from."""
    assert bm.STALE_READING_SECONDS == bm.TRAIN_MAX_PARKED_SECONDS


# --- the estimate's shape ----------------------------------------------------

def _fake_model(monkeypatch, **over):
    model = {"intercept": 4.1, "beta_distance": 0.000885, "beta_elevation": 0.0717,
             "beta_temperature": -0.05, "mean_temperature_c": 28.0, "r_squared": 0.30,
             "n_observations": 5570, "fitted_at": "2026-08-11T04:37:43+00:00",
             "model_offsets": {"Cosmo": 0.0, "_default": 1.0},
             "residual_std": 7.95, "beta_parked_seconds": 0.00015}
    model.update(over)
    monkeypatch.setattr(bm, "latest_model", lambda refresh=False: model)
    monkeypatch.setattr(bm.weather, "current_temperature_c", lambda: 28.0)
    return model


def test_estimate_carries_a_band_not_just_a_point(monkeypatch):
    """Held-out MAE is ~5.7 pp. A bare number reads as a promise."""
    _fake_model(monkeypatch)
    out = bm.estimate_burn_percent(5000.0, 40.0)
    assert out["percent_low"] < out["percent"] < out["percent_high"]
    assert out["percent_low"] >= 0.0


def test_the_climb_is_reported_separately(monkeypatch):
    """0.0717 pp per metre, and an Apollo costs ~2.2x a Cosmo per metre. A
    rider has no other way to learn this, so the share is surfaced rather than
    buried in a total."""
    _fake_model(monkeypatch)
    flat = bm.estimate_burn_percent(5000.0, 0.0)
    hilly = bm.estimate_burn_percent(5000.0, 100.0)
    assert flat["from_elevation_percent"] == 0.0
    assert hilly["from_elevation_percent"] == pytest.approx(7.17, abs=0.05)
    assert hilly["from_elevation_share"] > flat["from_elevation_share"]
    # The climb really is a large share of a hilly route's cost.
    assert hilly["from_elevation_share"] > 0.3


def test_a_band_is_never_negative_or_over_100(monkeypatch):
    _fake_model(monkeypatch)
    tiny = bm.estimate_burn_percent(50.0, 0.0)
    assert tiny["percent_low"] >= 0.0
    huge = bm.estimate_burn_percent(200_000.0, 3000.0)
    assert huge["percent_high"] <= 100.0
