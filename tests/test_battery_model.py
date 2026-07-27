"""Battery-burn model: anchor filter, SoC handling, fit, and adherence.

The regression itself is arithmetic; what these tests pin down are the
data-quality decisions that make it meaningful — measuring burn in state-of-
charge percent rather than vendor metres, excluding battery swaps, and refusing
to serve a number when no model has been fit.
"""

from __future__ import annotations

import pytest

from src import battery_model, valhalla
from src.quality import compute_battery_percent


# --- the SoC grid ------------------------------------------------------------

def test_range_metres_are_a_quantised_soc_grid():
    """Why burn is measured in percent, not metres.

    The feed emits 100 distinct range values fleet-wide; percent is the rank in
    that table. Regressing on raw metres would fit the vendor's nonlinear curve.
    """
    from src.quality import _soc_lut
    lut = _soc_lut()
    assert len(lut) == 100
    assert compute_battery_percent(lut[0]) == 0
    assert compute_battery_percent(lut[-1]) == 100
    # Monotonic: a larger range value never maps to a smaller percent.
    percents = [compute_battery_percent(v) for v in lut]
    assert percents == sorted(percents)


def test_burn_in_percent_is_not_proportional_to_burn_in_metres():
    """The two are genuinely different measurements — this is the whole reason
    §3B exists. If this ever becomes proportional, the LUT has been flattened."""
    from src.quality import _soc_lut
    lut = _soc_lut()
    lo_pair = (lut[10], lut[20])
    hi_pair = (lut[80], lut[90])
    # Same 10-point SoC drop at each end of the range...
    assert (compute_battery_percent(lo_pair[1]) - compute_battery_percent(lo_pair[0])
            == compute_battery_percent(hi_pair[1]) - compute_battery_percent(hi_pair[0]))
    # ...but a different number of metres.
    assert (lo_pair[1] - lo_pair[0]) != (hi_pair[1] - hi_pair[0])


# --- anchor filter -----------------------------------------------------------

def test_anchor_filter_thresholds_match_the_spec():
    assert battery_model.MIN_DURATION_S == 10 * 60
    assert battery_model.MAX_DURATION_S == 30 * 60
    assert battery_model.MIN_DISTANCE_METERS == pytest.approx(1609.34)
    assert battery_model.MIN_IMPLIED_MPH == 8.0


def test_straight_line_prefilter_is_below_the_routed_anchor():
    """The cheap pre-filter must not pre-empt the real 1-mile test.

    Straight-line distance always understates the routed path, so filtering at
    1 mile before routing would silently drop qualifying trips.
    """
    assert battery_model.MIN_STRAIGHT_LINE_METERS < battery_model.MIN_DISTANCE_METERS


def test_trips_are_derived_from_observation_gaps_not_trip_tables():
    """Guards the finding that forced this design.

    device_history.departed_at equals the next stop's snapshot_time at p50, p90
    and mean (measured over 1.37M stops), so it yields no duration and zero
    stops in the 10-30 min band. The extraction SQL must read the telemetry
    stream, not a trip table.
    """
    sql = battery_model._PAIRS_SQL
    assert "raw_telemetry_points" in sql
    assert "device_history" not in sql
    assert "trip_events" not in sql
    # The gap between consecutive observations IS the duration.
    assert "LEAD(snapshot_time)" in sql


def test_implied_speed_uses_routed_distance():
    # 2 miles in 12 minutes = 10 mph, which clears the 8 mph floor.
    mph = battery_model._implied_mph(2 * 1609.34, 12 * 60)
    assert mph == pytest.approx(10.0, abs=0.01)
    assert mph > battery_model.MIN_IMPLIED_MPH


def test_meandering_trip_fails_the_speed_floor():
    # 1.1 miles in 25 minutes = 2.6 mph — a ghost trip, not a ride.
    mph = battery_model._implied_mph(1.1 * 1609.34, 25 * 60)
    assert mph < battery_model.MIN_IMPLIED_MPH


def test_zero_duration_does_not_divide_by_zero():
    assert battery_model._implied_mph(1000.0, 0) == 0.0


def test_swap_threshold_excludes_recharges_not_rides():
    """A +20pp jump is a battery swap. Left in, it would appear as a large
    negative burn and drag the whole fit."""
    assert battery_model.SWAP_JUMP_PCT == 20.0


# --- pair acceptance ---------------------------------------------------------

def _pair(range_start, range_end):
    from src.quality import _soc_lut
    lut = _soc_lut()
    return {"range_start": lut[range_start], "range_end": lut[range_end]}


def _stats():
    return {"zero_delta": 0, "rejected_soc": 0, "rejected_swap": 0}


def test_normal_burn_is_accepted():
    st = _stats()
    out = battery_model._accept_pair(_pair(80, 70), st)
    assert out is not None
    assert out["burn"] == pytest.approx(10, abs=1)


def test_battery_swap_is_rejected_not_counted_as_negative_burn():
    st = _stats()
    assert battery_model._accept_pair(_pair(20, 90), st) is None
    assert st["rejected_swap"] == 1
    assert st["rejected_soc"] == 0


def test_zero_delta_is_counted_but_not_stored():
    """Quantization, not a real observation — storing it would drag the
    intercept toward zero burn, but it still has to be visible."""
    st = _stats()
    assert battery_model._accept_pair(_pair(50, 50), st) is None
    assert st["zero_delta"] == 1


def test_implausibly_large_burn_is_rejected():
    st = _stats()
    assert battery_model._accept_pair(_pair(99, 1), st) is None
    assert st["rejected_soc"] == 1


# --- serving -----------------------------------------------------------------

def test_no_estimate_before_a_model_is_fit(monkeypatch):
    """Must return None with a reason, never a fabricated default."""
    monkeypatch.setattr(battery_model, "latest_model", lambda refresh=False: None)
    out = battery_model.estimate_burn_percent(distance_meters=3000.0,
                                              elevation_gain_meters=40.0)
    assert out["percent"] is None
    assert out["source"] == "unavailable"
    assert out["reason"] == "no_model"


def test_estimate_applies_the_coefficients(monkeypatch):
    monkeypatch.setattr(battery_model, "latest_model", lambda refresh=False: {
        "intercept": 1.0,
        "beta_distance": 0.001,      # 1pp per km
        "beta_elevation": 0.02,      # 2pp per 100m climbed
        "beta_temperature": -0.05,
        "mean_temperature_c": 20.0,
        "r_squared": 0.6, "n_observations": 500, "fitted_at": None,
    })
    monkeypatch.setattr(battery_model.weather, "current_temperature_c", lambda: 20.0)

    out = battery_model.estimate_burn_percent(distance_meters=3000.0,
                                              elevation_gain_meters=50.0)
    # 1.0 + 3.0 + 1.0 - 1.0 = 4.0
    assert out["percent"] == pytest.approx(4.0)
    assert out["source"] == "regression"
    assert out["temperature_fallback"] is False


def test_falls_back_to_mean_training_temperature(monkeypatch):
    """A weather outage must not fail the route."""
    monkeypatch.setattr(battery_model, "latest_model", lambda refresh=False: {
        "intercept": 0.0, "beta_distance": 0.0, "beta_elevation": 0.0,
        "beta_temperature": 0.1, "mean_temperature_c": 18.0,
        "r_squared": None, "n_observations": 100, "fitted_at": None,
    })
    monkeypatch.setattr(battery_model.weather, "current_temperature_c", lambda: None)

    out = battery_model.estimate_burn_percent(distance_meters=1000.0,
                                              elevation_gain_meters=0.0)
    assert out["temperature_fallback"] is True
    assert out["percent"] == pytest.approx(1.8)


def test_negative_prediction_is_clamped(monkeypatch):
    monkeypatch.setattr(battery_model, "latest_model", lambda refresh=False: {
        "intercept": -50.0, "beta_distance": 0.0, "beta_elevation": 0.0,
        "beta_temperature": 0.0, "mean_temperature_c": 20.0,
        "r_squared": None, "n_observations": 100, "fitted_at": None,
    })
    monkeypatch.setattr(battery_model.weather, "current_temperature_c", lambda: 20.0)
    out = battery_model.estimate_burn_percent(1000.0, 0.0)
    assert out["percent"] == 0.0


def test_missing_elevation_is_treated_as_flat_not_dropped(monkeypatch):
    monkeypatch.setattr(battery_model, "latest_model", lambda refresh=False: {
        "intercept": 2.0, "beta_distance": 0.0, "beta_elevation": 0.05,
        "beta_temperature": 0.0, "mean_temperature_c": 20.0,
        "r_squared": None, "n_observations": 100, "fitted_at": None,
    })
    monkeypatch.setattr(battery_model.weather, "current_temperature_c", lambda: 20.0)
    out = battery_model.estimate_burn_percent(1000.0, None)
    assert out["percent"] == pytest.approx(2.0)


def test_no_distance_yields_no_estimate():
    out = battery_model.estimate_burn_percent(None, 10.0)
    assert out["percent"] is None
    assert out["reason"] == "no_distance"


# --- adherence (§3G) ---------------------------------------------------------

def test_adherence_is_length_weighted_over_way_ids(monkeypatch):
    monkeypatch.setattr(
        battery_model.valhalla, "trace_attributes",
        lambda *a, **kw: [
            {"way_id": 1, "length": 9.0},   # on the proposed route
            {"way_id": 7, "length": 1.0},   # a detour
        ])
    out = battery_model.route_adherence([(1.0, 1.0), (2.0, 2.0)], {1, 2, 3})
    assert out["fraction"] == pytest.approx(0.9)
    assert out["adherent"] is True


def test_adherence_below_threshold_is_not_adherent(monkeypatch):
    monkeypatch.setattr(
        battery_model.valhalla, "trace_attributes",
        lambda *a, **kw: [
            {"way_id": 1, "length": 8.0},
            {"way_id": 7, "length": 2.0},
        ])
    out = battery_model.route_adherence([(1.0, 1.0), (2.0, 2.0)], {1})
    assert out["fraction"] == pytest.approx(0.8)
    assert out["adherent"] is False


def test_failed_map_match_is_unknown_not_false(monkeypatch):
    """A matching failure must not be recorded as a non-adherent ride — that
    would poison the training set with false negatives."""
    def boom(*a, **kw):
        raise valhalla.ValhallaError("no match")

    monkeypatch.setattr(battery_model.valhalla, "trace_attributes", boom)
    out = battery_model.route_adherence([(1.0, 1.0), (2.0, 2.0)], {1})
    assert out["adherent"] is None
    assert out["reason"] == "match_failed"


def test_adherence_threshold_is_85_percent():
    assert battery_model.ADHERENCE_THRESHOLD == 0.85
