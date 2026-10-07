"""The equity cut: rentals unlocked inside vs outside the official Equity
Areas, from rental_outcomes_hourly's per-POINT equity_area (sql/092)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from src import fleet_equity
from src.fleet_equity import newcombe, summarize_areas, wilson
from src.fleet_outcomes import MIN_RENTALS_FOR_RATE as FLOOR


def test_wilson_matches_a_textbook_value():
    lo, hi = wilson(81, 263)  # Newcombe (1998) example: 0.2553–0.3662
    assert round(lo, 4) == 0.2553 and round(hi, 4) == 0.3662
    assert wilson(0, 0) is None


def test_newcombe_matches_a_textbook_value():
    lo, hi = newcombe(56, 70, 48, 80)  # Newcombe (1998) #10: 0.0524–0.3339
    assert round(lo, 4) == 0.0524 and round(hi, 4) == 0.3339


def test_every_equity_area_counts_inside_and_is_listed():
    rows = [
        ("EQ_003", 600, 72, 30, 600),
        ("EQ_018", 400, 48, 20, 400),
        ("outside", 5000, 300, 150, 5000),
    ]
    out = summarize_areas(rows)
    assert out["inside"]["rentals"] == 1000
    assert out["inside"]["ended_within_radius"] == 120
    assert out["inside"]["ended_within_radius_rate"] == 0.12
    assert out["inside"]["areas_represented"] == 2
    assert out["inside"]["areas_official"] == 30
    assert [a["area"] for a in out["by_area"]] == ["EQ_003", "EQ_018"]
    assert out["outside"]["ended_within_radius_rate"] == 0.06
    assert out["difference_points"] == 6.0
    lo, hi = out["difference_points_ci95"]
    assert lo < 6.0 < hi and lo > 0
    assert out["difference_distinguishable"] is True


def test_never_left_radius_is_reported_beside_ended_within_radius():
    out = summarize_areas([("EQ_001", 500, 60, 10, 400), ("outside", 500, 30, 25, 500)])
    assert out["inside"]["never_left_radius_rate"] == 0.025  # 10 / 400 known
    assert out["inside"]["ended_within_radius_rate"] == 0.12


def test_an_overlapping_interval_is_not_called_a_difference():
    out = summarize_areas([("EQ_001", 250, 26, 0, 0), ("outside", 250, 24, 0, 0)])
    assert out["difference_points"] == 0.8
    assert out["difference_distinguishable"] is False


def test_the_difference_needs_both_sides_over_the_floor():
    out = summarize_areas([("EQ_001", FLOOR - 1, 9, 0, 0), ("outside", 1000, 60, 0, 0)])
    assert out["inside"]["ended_within_radius_rate"] is None
    assert out["inside"]["rentals"] == FLOOR - 1, "counts stay when the rate is withheld"
    assert out["inside"]["ended_within_radius_ci95"] is None
    assert out["difference_points"] is None and out["difference_points_ci95"] is None
    assert out["difference_distinguishable"] is None


def test_unknown_and_unrecorded_origins_are_excluded_exactly():
    out = summarize_areas([
        ("EQ_001", 300, 30, 0, 0),
        ("unknown", 17, 5, 0, 0),
        ("unrecorded", 452, 26, 7, 452),
        ("outside", 300, 15, 0, 0),
    ])
    assert out["excluded"] == {"unknown_origin": 17, "unrecorded": 452}
    assert out["inside"]["rentals"] == 300 and out["outside"]["rentals"] == 300
    assert [a["area"] for a in out["by_area"]] == ["EQ_001"]


def test_the_difference_is_taken_from_counts_not_rounded_rates():
    # 1/3 vs 1/7 rounds to 0.3333 / 0.1429; the raw difference is 19.05 pp.
    out = summarize_areas([("EQ_001", 300, 100, 0, 0), ("outside", 700, 100, 0, 0)])
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
