"""Phase 1b / Phase 2 rules that need no database: which reports a condition
check asks about, the feed-confirmation window, the request model, the
points constants, and what an admin SMS watch counts as a change."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src import admin_watch, condition_checks, fleet_reports, points
from src.api_condition_checks import ConditionCheckIn

T = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
W = condition_checks.FEED_WINDOW


def test_the_negative_rideability_set_is_the_suppressing_set_minus_not_found():
    assert set(fleet_reports.CONDITION_CHECK_TYPES) == (
        set(fleet_reports.SUPPRESSION_REASON_PRIORITY) - {"not_found"})
    assert "improperly_parked" not in fleet_reports.CONDITION_CHECK_TYPES
    assert fleet_reports.FOUND_ON_CHECK_TYPES == ("not_found",)


def test_points_are_10_plus_40_and_even():
    assert points.POINTS_CONDITION_CHECK == 10
    assert points.POINTS_CONDITION_CHECK_FEED_CONFIRMED == 40
    assert points.POINTS_CONDITION_CHECK_MAX == 50
    assert points.CONDITION_CHECK_COOLDOWN_HOURS == 24
    assert points.CONDITION_CHECK_DAILY_CAP == 10


def test_the_window_is_20_minutes():
    assert condition_checks.FEED_WINDOW_MINUTES == 20


def _sig(**kw):
    base = dict(submitted_at=T, parked_since_at_check=T - timedelta(days=3),
                rental_started_at_check=None, parked_since_now=T - timedelta(days=3),
                rental_started_now=None)
    base.update(kw)
    return condition_checks.feed_signal(**base)


@pytest.mark.parametrize("offset, expected", [
    (timedelta(minutes=-19), "reserved"),
    (timedelta(minutes=5), "reserved"),
    (timedelta(minutes=20), "reserved"),
    (timedelta(minutes=21), None),
    (timedelta(minutes=-21), None),
])
def test_a_rental_counts_only_inside_the_window(offset, expected):
    assert _sig(rental_started_now=T + offset) == expected


def test_a_move_after_the_check_counts_inside_the_window():
    assert _sig(parked_since_now=T + timedelta(minutes=7)) == "moved"
    assert _sig(parked_since_now=T + W + timedelta(minutes=1)) is None


def test_evidence_seen_at_submission():
    assert _sig(rental_started_at_check=T - timedelta(minutes=6)) == "rental_before_check"
    assert _sig(rental_started_at_check=T - timedelta(hours=2)) is None
    assert _sig(parked_since_at_check=T - timedelta(minutes=3),
                parked_since_now=T - timedelta(minutes=3)) == "moved_before_check"


def test_nothing_is_no_signal():
    assert _sig() is None


def test_the_request_needs_proof_and_unique_answers():
    with pytest.raises(ValidationError):
        ConditionCheckIn(answers=[], test_ride=True)
    with pytest.raises(ValidationError):
        ConditionCheckIn(answers=[{"report_id": 1, "still_a_problem": True},
                                  {"report_id": 1, "still_a_problem": False}],
                         test_ride=True, submitted_plate="1")
    ok = ConditionCheckIn(answers=[{"report_id": 1, "still_a_problem": False}],
                          test_ride=False, feature_report_id=9)
    assert ok.test_ride is False


# ---------------------------------------------------------------------------
# Admin SMS watch: what counts as a change
# ---------------------------------------------------------------------------

def _prev(**kw):
    p = {"in_feed": True, "reserved": False, "disabled": False,
         "lat": 39.7392, "lon": -104.9903}
    p.update(kw)
    return p


def _now(**kw):
    o = dict(in_feed=True, reserved=False, disabled=False, lat=39.7392, lon=-104.9903)
    o.update(kw)
    return admin_watch.Observed(**o)


def test_the_first_cycle_is_a_silent_baseline():
    assert admin_watch.changes(_prev(in_feed=None), _now()) == []


def test_state_changes_are_reported():
    assert admin_watch.changes(_prev(), _now(in_feed=False, lat=None, lon=None)) == [
        "left the feed"]
    assert admin_watch.changes(_prev(in_feed=False), _now()) == ["is back in the feed"]
    assert admin_watch.changes(_prev(), _now(reserved=True)) == ["rental started"]
    assert admin_watch.changes(_prev(disabled=True), _now()) == ["no longer disabled"]


def test_jitter_is_not_a_move_and_rental_positions_are_ignored():
    assert admin_watch.changes(_prev(), _now(lat=39.7393)) == []      # ~11 m
    assert admin_watch.changes(_prev(reserved=True), _now(reserved=True, lat=39.75)) == []
    moved = admin_watch.changes(_prev(), _now(lat=39.7492))
    assert len(moved) == 1 and moved[0].startswith("moved ")
