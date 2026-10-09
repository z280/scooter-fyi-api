"""The equity cut: rentals unlocked inside vs outside the official Equity
Areas, from rental_outcomes_hourly's per-POINT equity_area (sql/092)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from src import fleet_equity
from src.fleet_equity import cluster_ratio, summarize_areas
from src.fleet_outcomes import MIN_RENTALS_FOR_RATE as FLOOR


def spread(area, rentals, no_gos, cells=40, first=1, no_gos_max=0, max_known=0):
    """A side spread evenly over `cells` r9 cells (ids from `first`)."""
    return [
        (area, first + c, rentals // cells, no_gos // cells, no_gos_max // cells, max_known // cells)
        for c in range(cells)
    ]


def test_cluster_variance_matches_a_hand_computation(monkeypatch):
    monkeypatch.setattr(fleet_equity, "MIN_CLUSTERS", 2)
    p, var = cluster_ratio([(1, 2), (3, 4)])
    # p = 4/6; residuals -1/3, +1/3; 2/1 * (2/9) / 36
    assert round(p, 6) == round(4 / 6, 6)
    assert round(var, 6) == round(2 * (2 / 9) / 36, 6)


def test_identical_cells_have_no_between_cell_variance():
    p, var = cluster_ratio([(6, 50)] * 40)
    assert p == 0.12 and var == 0


def test_too_few_cells_is_no_interval():
    assert cluster_ratio([(6, 50)] * 29) is None


def test_every_equity_area_counts_inside_and_is_listed():
    rows = spread("EQ_003", 6000, 720, first=1) + spread("EQ_018", 4000, 480, first=100) \
        + spread("outside", 40000, 2400, first=1000)
    out = summarize_areas(rows)
    assert out["inside"]["rentals"] == 10000 and out["inside"]["ended_within_radius"] == 1200
    assert out["inside"]["ended_within_radius_rate"] == 0.12
    assert out["inside"]["cells"] == 80
    assert out["inside"]["areas_represented"] == 2
    assert [a["area"] for a in out["by_area"]] == ["EQ_003", "EQ_018"]
    assert out["outside"]["ended_within_radius_rate"] == 0.06
    assert out["difference_points"] == 6.0
    # Perfectly even cells: zero between-cell variance, a point interval.
    assert out["difference_points_ci95"] == [6.0, 6.0]
    assert out["difference_distinguishable"] is True


def test_clustered_rentals_widen_the_interval_until_it_spans_zero():
    """Same pooled rates (12% vs 10%), but inside's no-gos are piled into a
    few cells: a naive interval would call this a difference; the cluster-
    robust one does not."""
    inside = [("EQ_001", c, 100, 60 if c < 20 else 0, 0, 0) for c in range(100)]   # 1200/10000
    outside = [("outside", 1000 + c, 100, 10, 0, 0) for c in range(100)]           # 1000/10000
    out = summarize_areas(inside + outside)
    assert out["difference_points"] == 2.0
    lo, hi = out["difference_points_ci95"]
    assert lo < 0 < hi
    assert out["difference_distinguishable"] is False


def test_never_left_radius_is_reported_beside_ended_within_radius():
    out = summarize_areas(spread("EQ_001", 4000, 480, no_gos_max=80, max_known=3200)
                          + spread("outside", 4000, 240, first=500))
    assert out["inside"]["never_left_radius_rate"] == 0.025  # 80 / 3200 known
    assert out["inside"]["ended_within_radius_rate"] == 0.12


def test_the_difference_needs_both_sides_over_the_floor():
    out = summarize_areas([("EQ_001", 1, FLOOR - 1, 9, 0, 0)] + spread("outside", 4000, 240, first=500))
    assert out["inside"]["ended_within_radius_rate"] is None
    assert out["inside"]["rentals"] == FLOOR - 1, "counts stay when the rate is withheld"
    assert out["difference_points"] is None and out["difference_points_ci95"] is None
    assert out["difference_distinguishable"] is None


def test_few_cells_get_a_difference_but_no_interval_or_verdict():
    out = summarize_areas([("EQ_001", 1, 1000, 120, 0, 0), ("outside", 2, 1000, 60, 0, 0)])
    assert out["difference_points"] == 6.0
    assert out["difference_points_ci95"] is None and out["difference_distinguishable"] is None


def test_unknown_outside_city_and_unrecorded_are_excluded_exactly():
    out = summarize_areas([
        ("EQ_001", 1, 300, 30, 0, 0),
        ("unknown", 1, 17, 5, 0, 0),
        ("outside_city", 9, 40, 2, 0, 0),
        ("unrecorded", 2, 452, 26, 7, 452),
        ("outside", 2, 300, 15, 0, 0),
    ])
    assert out["excluded"] == {"unknown_origin": 17, "outside_city": 40, "unrecorded": 452}
    assert out["inside"]["rentals"] == 300 and out["outside"]["rentals"] == 300
    assert out["_degraded"] is False


def test_mostly_unknown_origins_is_degraded():
    out = summarize_areas([("EQ_001", 1, 10, 1, 0, 0), ("unknown", 1, 500, 5, 0, 0)])
    assert out["_degraded"] is True


def test_the_difference_is_taken_from_counts_not_rounded_rates():
    # 1/3 vs 1/7 rounds to 0.3333 / 0.1429; the raw difference is 19.05 pp.
    out = summarize_areas([("EQ_001", 1, 300, 100, 0, 0), ("outside", 2, 700, 100, 0, 0)])
    assert out["difference_points"] == 19.05


def _fake(rows, since, *, raise_on_execute=False):
    class _Cur:
        def __init__(self): self.n = 0
        def execute(self, sql, params=None):
            if raise_on_execute:
                raise RuntimeError("db down")
            self.n += 1
            if self.n == 1:
                assert "GROUP BY equity_area" in sql and params[2] == 25.0
        def fetchall(self): return rows
        def fetchone(self): return (since,)
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    @contextmanager
    def _conn():
        yield _Conn()
    return _conn


def test_the_payload_states_window_coverage_sample_radius_and_method(monkeypatch):
    since = datetime(2026, 10, 7, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(fleet_equity, "connection", _fake([], since))
    out = fleet_equity.summarize("28d", now=datetime(2026, 10, 10, 12, 30, tzinfo=timezone.utc))
    assert out["status"] == "ok"
    assert out["window"] == "28d"
    assert out["window_start"] == "2026-09-12T12:00:00+00:00"
    assert out["window_end"] == "2026-10-10T12:00:00+00:00"
    assert out["data_since"] == "2026-10-07T05:00:00+00:00"
    assert out["hours_covered"] == 3 * 24 + 7
    assert out["hours_in_window"] == 28 * 24
    assert out["window_complete"] is False
    assert out["radius_meters"] == 25.0
    assert (out["definition"], out["attribution"]) == ("end_displacement", "unlock_point")
    assert "looped back" in out["caveats"] and "does not say" in out["caveats"]


def test_a_full_window_is_complete(monkeypatch):
    since = datetime(2026, 10, 7, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(fleet_equity, "connection", _fake([], since))
    out = fleet_equity.summarize("7d", now=datetime(2026, 10, 20, 0, tzinfo=timezone.utc))
    assert out["window_complete"] is True and out["hours_covered"] == 168


def test_a_broken_boundary_file_reads_degraded_not_ok(monkeypatch):
    # The SQL's shape: (..., max_known, stayed, stayed_known).
    rows = [("unknown", 1, 900, 50, 0, 0, 0, 0), ("outside", 2, 100, 5, 0, 0, 0, 0)]
    monkeypatch.setattr(fleet_equity, "connection", _fake(rows, None))
    assert fleet_equity.summarize("7d")["status"] == "degraded"


def test_a_failure_says_unavailable_rather_than_looking_like_a_quiet_week(monkeypatch):
    monkeypatch.setattr(fleet_equity, "connection", _fake([], None, raise_on_execute=True))
    out = fleet_equity.summarize("7d")
    assert out["status"] == "unavailable"
    assert out["inside"]["rentals"] == 0 and out["difference_points"] is None


def test_the_route_rejects_an_unknown_window():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src import api_public
    app = FastAPI()
    app.include_router(api_public.router)
    assert TestClient(app).get("/api/v1/fleet/outcomes/equity?window=1y").status_code == 400


# --- sql/098: never left the spot -------------------------------------------

def test_stayed_is_reported_over_the_rentals_that_recorded_it():
    """Rows from before sql/098 hold stayed = stayed_known = 0 ("not
    recorded"): they count as rentals but never dilute the stayed rate."""
    recorded = [("outside", 1000 + c, 100, 10, 4, 100, 2, 100) for c in range(40)]  # 80/4000
    before = [("outside", 1000 + c, 100, 10, 4, 100) for c in range(40)]              # no stayed
    out = summarize_areas(recorded + before)
    side = out["outside"]
    assert side["rentals"] == 8000
    assert (side["stayed_known"], side["stayed"], side["stayed_rate"]) == (4000, 80, 0.02)
    assert side["stayed_ci95"] == [0.02, 0.02]
    # The 25 m figures are untouched.
    assert side["ended_within_radius_rate"] == 0.1


def test_stayed_rate_is_withheld_under_the_floor():
    out = summarize_areas([("EQ_001", c, 100, 10, 0, 100, 1, 4) for c in range(40)])
    assert out["inside"]["stayed_known"] == 160 < FLOOR
    assert out["inside"]["stayed_rate"] is None and out["inside"]["stayed_ci95"] is None


def test_the_payload_states_when_stayed_started_and_its_radius(monkeypatch):
    since = datetime(2026, 10, 9, 20, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(fleet_equity, "connection", _fake([], since))
    out = fleet_equity.summarize("7d", now=datetime(2026, 10, 10, 12, 30, tzinfo=timezone.utc))
    assert out["stayed_counted_since"] == "2026-10-09T20:30:00+00:00"
    assert out["stayed_hours_covered"] == 15
    assert out["stayed_radius_meters"] == 50.0
    assert out["stayed_definition"].startswith("never left the spot")
    for side in ("inside", "outside"):
        assert {"stayed", "stayed_known", "stayed_rate", "stayed_ci95"} <= set(out[side])
