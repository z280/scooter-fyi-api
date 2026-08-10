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
    """Re-derived 2026-08-10 against 24,954 real reservation episodes when the
    anchor moved off observation gaps — see THE ANCHOR in the module docstring.
    The previous values (10-30 min, 1 mile, 8 mph) were calibrated for
    gap-anchored trips: against rentals the distance floor kept 33% and the
    speed floor kept 8%."""
    # One reserved sample bracketed by two available ones at the 2-min cadence.
    assert battery_model.MIN_DURATION_S == 240
    assert battery_model.MAX_DURATION_S == 60 * 60
    assert battery_model.MIN_DISTANCE_METERS == pytest.approx(400.0)
    assert battery_model.MIN_IMPLIED_MPH == 3.0


def test_no_straight_line_prefilter_survives():
    """A displacement pre-filter cannot be reintroduced: 6% of episodes are
    loops that end within 400 m of their origin while covering over 800 m of
    real riding, and those are exactly the observations it would discard."""
    assert not hasattr(battery_model, "MIN_STRAIGHT_LINE_METERS")
    assert "straight" not in battery_model._RENTAL_EPISODES_SQL.lower()


def test_trips_are_derived_from_reservation_episodes_not_trip_tables():
    """Guards the finding that forced this design.

    device_history.departed_at equals the next stop's snapshot_time at p50, p90
    and mean (measured over 1.37M stops), so it yields no duration at all. The
    extraction SQL must read the telemetry stream, not a trip table.
    """
    sql = battery_model._RENTAL_EPISODES_SQL
    assert "raw_telemetry_points" in sql
    assert "device_history" not in sql
    assert "trip_events" not in sql
    # The reservation flag is the anchor, not the gap between observations.
    assert "is_reserved" in sql
    assert "LEAD(snapshot_time)" not in sql


def test_extraction_samples_randomly_not_most_recent_first():
    """~25k episodes a day against a limit in the low thousands. Ordering by
    time would mine the same hours every run — a systematic time-of-day, and
    therefore temperature, bias in a model that regresses on temperature."""
    sql = battery_model._RENTAL_EPISODES_SQL
    assert "ORDER BY RANDOM()" in sql
    assert "ORDER BY o.snapshot_time DESC" not in sql


def test_implied_speed_uses_routed_distance():
    # 2 miles in 12 minutes = 10 mph, comfortably over the floor.
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


# --- operator rebalancing ----------------------------------------------------

def _cand(vid, lat, lon, lat2, lon2, ts):
    from datetime import datetime, timezone
    return {"vehicle_identifier": vid, "latitude": lat, "longitude": lon,
            "lat2": lat2, "lon2": lon2,
            "departed_at": datetime.fromtimestamp(ts, tz=timezone.utc)}


def test_van_rebalancing_is_detected():
    """A van moves several scooters between the same two places at once; the
    8 mph floor does not exclude it, so it would train the model on drain that
    has nothing to do with riding."""
    batch = [_cand(f"v{i}", 39.75, -104.99, 39.70, -104.95, 1_000_000)
             for i in range(battery_model.REBALANCE_MIN_GROUP)]
    flagged = battery_model._rebalance_keys(batch)
    assert all(battery_model._rebalance_key(c) in flagged for c in batch)


def test_a_lone_rider_on_the_same_route_is_not_flagged():
    solo = [_cand("v1", 39.75, -104.99, 39.70, -104.95, 1_000_000)]
    assert battery_model._rebalance_keys(solo) == set()


def test_same_vehicle_repeated_is_not_a_batch():
    """Grouping counts DISTINCT vehicles — one scooter doing the same trip
    repeatedly is a popular route, not a van."""
    repeats = [_cand("v1", 39.75, -104.99, 39.70, -104.95, 1_000_000) for _ in range(5)]
    assert battery_model._rebalance_keys(repeats) == set()


def test_disabled_vehicles_are_excluded_in_sql():
    """Filtered on the bracketing sample, NOT in the base scan: removing rows
    mid-run would split one reservation episode into several."""
    sql = battery_model._RENTAL_EPISODES_SQL
    assert "NOT pre.disabled" in sql
    assert "is_disabled" in sql


# --- temperature bounding ----------------------------------------------------

def test_temperature_lookup_is_bounded():
    """Unbounded, a hole in the cache hands a trip a reading from days away and
    beta_3 absorbs the error as if it were signal.

    The bounded query itself lives in _temperature_at_cur — PLAN_RIDE_MODE_API.md
    phase A2's donated-ride ingestion needed the same lookup over an
    already-open cursor (it has no `conn` of its own to hand
    _temperature_at), so the query was split out and _temperature_at
    became a thin `with conn.cursor()` wrapper over it; both are asserted
    here so a refactor that re-inlines the query without keeping the split
    still passes."""
    assert battery_model.MAX_TEMPERATURE_GAP_SECONDS == 2 * 3600
    src = __import__("inspect").getsource(battery_model._temperature_at_cur)
    assert "BETWEEN" in src
    wrapper_src = __import__("inspect").getsource(battery_model._temperature_at)
    assert "_temperature_at_cur" in wrapper_src


# --- per-model offsets -------------------------------------------------------

def _model_with_offsets(offsets):
    return {
        "intercept": 1.0, "beta_distance": 0.0, "beta_elevation": 0.0,
        "beta_temperature": 0.0, "mean_temperature_c": 20.0,
        "r_squared": None, "n_observations": 100, "fitted_at": None,
        "model_offsets": offsets,
    }


def test_named_vehicle_model_uses_its_own_offset(monkeypatch):
    monkeypatch.setattr(battery_model, "latest_model", lambda refresh=False:
                        _model_with_offsets({"Cosmo": 0.0, "Astro": 2.0, "_default": 0.5}))
    monkeypatch.setattr(battery_model.weather, "current_temperature_c", lambda: 20.0)
    out = battery_model.estimate_burn_percent(1000.0, 0.0, vehicle_model="Astro")
    assert out["percent"] == pytest.approx(3.0)
    assert out["model_offset"] == pytest.approx(2.0)


def test_unnamed_model_uses_the_fleet_weighted_default(monkeypatch):
    """The route endpoint prices a route, not a vehicle, so it usually cannot
    name a model."""
    monkeypatch.setattr(battery_model, "latest_model", lambda refresh=False:
                        _model_with_offsets({"Cosmo": 0.0, "Astro": 2.0, "_default": 0.5}))
    monkeypatch.setattr(battery_model.weather, "current_temperature_c", lambda: 20.0)
    out = battery_model.estimate_burn_percent(1000.0, 0.0)
    assert out["percent"] == pytest.approx(1.5)


def test_unknown_model_name_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(battery_model, "latest_model", lambda refresh=False:
                        _model_with_offsets({"Cosmo": 0.0, "_default": 0.5}))
    monkeypatch.setattr(battery_model.weather, "current_temperature_c", lambda: 20.0)
    out = battery_model.estimate_burn_percent(1000.0, 0.0, vehicle_model="Nonesuch")
    assert out["percent"] == pytest.approx(1.5)


# --- elevation ---------------------------------------------------------------

def test_partial_elevation_is_unknown_not_undercounted():
    """A leg without samples must not silently reduce the reported climb — the
    battery model would read that as a flat route."""
    trip = {"legs": [{"shape": "a", "elevation": [1600.0, 1620.0]}, {"shape": "b"}],
            "summary": {"length": 1.0, "time": 60.0}}
    assert valhalla.elevation_gain_meters(trip) is None


# --- temperature availability (found by Copilot review of 90aeda4) -----------

class _FakeCursor:
    def __init__(self, result): self.result = result
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a, **kw): pass
    def fetchone(self): return self.result


class _FakeConn:
    def __init__(self, temp_row): self.temp_row = temp_row
    def cursor(self): return _FakeCursor(self.temp_row)


def test_observation_is_skipped_when_no_temperature_is_available(monkeypatch):
    """A row stored with temperature_c NULL is permanently dead weight: train()
    filters it out and the (vehicle, departed_at) dedupe means a later run never
    revisits it. Skipping keeps the candidate eligible after a backfill."""
    routed = []
    monkeypatch.setattr(battery_model.valhalla, "route",
                        lambda *a, **kw: routed.append(1) or {"trip": {}})
    stats = {"rejected_no_temperature": 0, "no_route": 0,
             "rejected_distance": 0, "rejected_speed": 0}
    cand = {"departed_at": None, "latitude": 39.7, "longitude": -105.0,
            "lat2": 39.71, "lon2": -104.99, "duration_seconds": 900}

    stored = battery_model._route_and_store(_FakeConn(None), cand, stats)
    assert stored is False
    assert stats["rejected_no_temperature"] == 1
    # And it bailed BEFORE paying for a Valhalla call.
    assert routed == []


def test_temperature_is_checked_before_routing(monkeypatch):
    """Ordering matters: an unavailable temperature must not cost a route call
    on every subsequent run."""
    import inspect
    src = inspect.getsource(battery_model._route_and_store)
    assert src.index("_temperature_at") < src.index("_route_summary")
