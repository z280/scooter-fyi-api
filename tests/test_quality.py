"""Quality designation rules + reliability tier + daylight-hours math."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.quality import (
    compute_quality_designation,
    compute_reliability_tier,
    daylight_hours_between,
)

DENVER = ZoneInfo("America/Denver")


def _denver(yyyy_mm_dd: str, h: int, m: int = 0) -> datetime:
    y, mo, d = (int(x) for x in yyyy_mm_dd.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=DENVER)


# Defaults: a healthy, freshly-observed scooter in non-reserved state.
_BASE = dict(
    is_disabled=False,
    is_reserved=False,
    number_failed_starts=0,
    first_observed_at_location=_denver("2026-06-01", 10),  # 10 AM Denver
    has_negative_report=False,
    now=_denver("2026-06-01", 11),                          # 1h dwell
)


# ---------- N/A overrides --------------------------------------------------
def test_disabled_is_na():
    out = compute_quality_designation(
        current_range_meters=50000,
        **{**_BASE, "is_disabled": True},
    )
    assert out == "N/A"


def test_reserved_is_na():
    out = compute_quality_designation(
        current_range_meters=50000,
        **{**_BASE, "is_reserved": True},
    )
    assert out == "N/A"


def test_no_range_is_na():
    out = compute_quality_designation(
        current_range_meters=None, **_BASE,
    )
    assert out == "N/A"


# ---------- Baseline tiers -------------------------------------------------
def test_baseline_great_at_75_pct_battery():
    """40,000 m ≈ 88% SoC (above the 75% 'great' floor)."""
    assert compute_quality_designation(
        current_range_meters=40_000, **_BASE,
    ) == "great"


def test_baseline_good_at_15mi_absolute():
    assert compute_quality_designation(
        current_range_meters=24_500, **_BASE,
    ) == "good"


def test_baseline_acceptable_at_8mi_absolute():
    assert compute_quality_designation(
        current_range_meters=13_000, **_BASE,
    ) == "acceptable"


def test_baseline_poor_below_8mi():
    assert compute_quality_designation(
        current_range_meters=5_000, **_BASE,
    ) == "poor"


def test_baseline_great_reachable_at_full_charge():
    """§7.1 regression: 45,293 m is the true full-charge value for EVERY
    type (bicycles included). Under the old rated-max rule no bicycle
    could ever reach 'great' (75% of 67,000 m > the 45,293 m cap)."""
    assert compute_quality_designation(
        current_range_meters=45_293, **_BASE,
    ) == "great"


# ---------- Hard-override demerits -----------------------------------------
def test_negative_report_forces_poor():
    """Even a 'great' baseline gets dragged to poor by a live report."""
    out = compute_quality_designation(
        current_range_meters=50_000,
        **{**_BASE, "has_negative_report": True},
    )
    assert out == "poor"


def test_two_failed_starts_forces_poor():
    out = compute_quality_designation(
        current_range_meters=50_000,
        **{**_BASE, "number_failed_starts": 2},
    )
    assert out == "poor"


# ---------- Soft demerits --------------------------------------------------
def test_one_failed_start_knocks_great_to_good():
    out = compute_quality_designation(
        current_range_meters=50_000,
        **{**_BASE, "number_failed_starts": 1},
    )
    assert out == "good"


def test_dwell_12_hours_knocks_one_tier():
    out = compute_quality_designation(
        current_range_meters=50_000,
        **{**_BASE,
           "first_observed_at_location": _denver("2026-06-01", 10),
           "now": _denver("2026-06-01", 22)},  # 12h elapsed
    )
    assert out == "good"


def test_dwell_24_hours_knocks_two_tiers():
    out = compute_quality_designation(
        current_range_meters=50_000,
        **{**_BASE,
           "first_observed_at_location": _denver("2026-06-01", 10),
           "now": _denver("2026-06-02", 10)},  # 24h elapsed
    )
    assert out == "acceptable"


def test_six_daylight_hours_knocks_one_tier_even_if_total_under_12h():
    """8am→3pm Denver = 7 daylight-hours; below 12h wall-clock total → daylight demerit kicks in."""
    out = compute_quality_designation(
        current_range_meters=50_000,
        **{**_BASE,
           "first_observed_at_location": _denver("2026-06-01", 8),
           "now": _denver("2026-06-01", 15)},  # 7h, all daylight
    )
    assert out == "good"


def test_overnight_hours_dont_count_as_daylight():
    """8pm→4am Denver = 8 wall-clock hours, ZERO daylight-hours."""
    out = compute_quality_designation(
        current_range_meters=50_000,
        **{**_BASE,
           "first_observed_at_location": _denver("2026-06-01", 20),
           "now": _denver("2026-06-02", 4)},  # 8h overnight, no daylight
    )
    # 8h < 12h dwell, 0h daylight → no demerit, stays at "great"
    assert out == "great"


# ---------- Stacking demerits ----------------------------------------------
def test_failed_start_and_long_dwell_stack():
    """failed_starts==1 (-1) + dwell≥24h (-2) → start from 'great', drop 3 → 'poor'."""
    out = compute_quality_designation(
        current_range_meters=50_000,
        **{**_BASE,
           "number_failed_starts": 1,
           "first_observed_at_location": _denver("2026-06-01", 10),
           "now": _denver("2026-06-02", 10)},
    )
    assert out == "poor"


# ---------- Reliability tier (API_REQUIREMENTS.md §1.2) ---------------------
# Defaults: a healthy, state-tracked scooter that arrived an hour ago.
_REL_BASE = dict(
    number_failed_starts=0,
    first_observed_at_location=_denver("2026-06-01", 10),
    quality_designation="good",
    has_negative_report=False,
    now=_denver("2026-06-01", 11),
)


def test_reliability_ok_for_healthy_tracked_device():
    assert compute_reliability_tier(**_REL_BASE) == "ok"


def test_reliability_negative_report_is_high_risk():
    out = compute_reliability_tier(**{**_REL_BASE, "has_negative_report": True})
    assert out == "high_risk"


def test_reliability_two_failed_starts_is_high_risk():
    out = compute_reliability_tier(**{**_REL_BASE, "number_failed_starts": 2})
    assert out == "high_risk"


def test_reliability_one_failed_start_is_unknown():
    """One bike_id rotation can be a rebalancing scan, so it's not enough
    alone for high_risk — but it's no longer a clean "ok" either."""
    out = compute_reliability_tier(**{**_REL_BASE, "number_failed_starts": 1})
    assert out == "unknown"


def test_reliability_one_failed_start_plus_24h_dwell_is_high_risk():
    out = compute_reliability_tier(**{
        **_REL_BASE,
        "number_failed_starts": 1,
        "now": _denver("2026-06-02", 10),  # 24h dwell
    })
    assert out == "high_risk"


def test_reliability_72h_idle_alone_is_high_risk():
    """The ghost-scooter rule: 3 days untouched, zero failed starts.

    Recalibrated from 96h against the 2026-07-06 snapshot (48h was already
    p90 citywide). The exact boundary is pinned in
    tests/test_dwell_outliers.py::test_ghost_rule_72h_boundary.
    """
    out = compute_reliability_tier(**{
        **_REL_BASE,
        "now": _denver("2026-06-04", 10),  # 72h dwell
    })
    assert out == "high_risk"


def test_reliability_untracked_device_is_unknown():
    """No plate upstream → no device_state row → both inputs None."""
    out = compute_reliability_tier(**{
        **_REL_BASE,
        "number_failed_starts": None,
        "first_observed_at_location": None,
    })
    assert out == "unknown"


def test_reliability_quality_na_is_unknown():
    """Disabled/reserved/rangeless devices can't be assessed."""
    out = compute_reliability_tier(**{**_REL_BASE, "quality_designation": "N/A"})
    assert out == "unknown"


def test_reliability_failure_signals_beat_na():
    """A disabled scooter with 2 failed starts is still high_risk, not unknown."""
    out = compute_reliability_tier(**{
        **_REL_BASE,
        "quality_designation": "N/A",
        "number_failed_starts": 2,
    })
    assert out == "high_risk"


def test_reliability_poor_quality_alone_stays_ok():
    """Low battery is not an unlock-failure signal — range never demotes."""
    out = compute_reliability_tier(**{**_REL_BASE, "quality_designation": "poor"})
    assert out == "ok"


# ---------- Reliability tier: peer-median dwell ratio (unknown) -------------
def test_reliability_dwell_2x_peer_median_is_unknown():
    """Below the high_risk outlier's 3x/p90/48h bar, but still a meaningful
    peer-relative outlier — reads as unknown rather than a clean "ok"."""
    out = compute_reliability_tier(**{
        **_REL_BASE,
        "peer_median_dwell_hours": 5.0,
        "now": _denver("2026-06-01", 20),  # 10h dwell = exactly 2x
    })
    assert out == "unknown"


def test_reliability_dwell_under_2x_peer_median_stays_ok():
    out = compute_reliability_tier(**{
        **_REL_BASE,
        "peer_median_dwell_hours": 5.1,
        "now": _denver("2026-06-01", 20),  # 10h dwell < 2 * 5.1
    })
    assert out == "ok"


def test_reliability_zero_peer_median_does_not_false_positive():
    """A peer median of exactly 0h must not make every dwell an 'outlier'."""
    out = compute_reliability_tier(**{**_REL_BASE, "peer_median_dwell_hours": 0.0})
    assert out == "ok"


def test_reliability_missing_peer_median_has_no_effect():
    """Sparse peer sets (None) never trigger the ratio rule."""
    out = compute_reliability_tier(**{
        **_REL_BASE,
        "peer_median_dwell_hours": None,
        "now": _denver("2026-06-02", 10),  # 24h dwell, still under the 72h ghost rule
    })
    assert out == "ok"


# ---------- Daylight-hours math --------------------------------------------
def test_daylight_overlap_fully_inside_window():
    """Start 10am, end 2pm = 4 daylight-hours."""
    assert daylight_hours_between(
        _denver("2026-06-01", 10), _denver("2026-06-01", 14),
    ) == 4.0


def test_daylight_overlap_starting_before_8am():
    """Start 6am, end 10am = 2 daylight-hours (8am-10am)."""
    assert daylight_hours_between(
        _denver("2026-06-01", 6), _denver("2026-06-01", 10),
    ) == 2.0


def test_daylight_overlap_ending_after_8pm():
    """Start 6pm, end 10pm = 2 daylight-hours (6pm-8pm)."""
    assert daylight_hours_between(
        _denver("2026-06-01", 18), _denver("2026-06-01", 22),
    ) == 2.0


def test_daylight_multi_day():
    """Start 10am Mon, end 10am Wed = 2 full daylight days (24h) + 2h Wed = 26h."""
    out = daylight_hours_between(
        _denver("2026-06-01", 10), _denver("2026-06-03", 10),
    )
    # Mon: 10a-8p = 10h
    # Tue: 8a-8p = 12h
    # Wed: 8a-10a = 2h
    # Total = 24
    assert out == 24.0


def test_daylight_zero_for_pure_overnight():
    """8pm Mon to 8am Tue = 12h, all overnight = 0 daylight."""
    assert daylight_hours_between(
        _denver("2026-06-01", 20), _denver("2026-06-02", 8),
    ) == 0.0
