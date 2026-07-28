"""Tests for src/points.py's ledger primitives against a fake cursor with
a scripted fetchone() queue."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.points import (
    REPORT_TYPE_POINTS,
    credit_gbfs_validation_points,
    credit_points,
    credit_qr_scan_points,
    credit_report_points,
    credit_waypoint_points,
    h3_8_index_for,
    maybe_credit_profile_completion,
)

_NOW = datetime.now(timezone.utc)


class _FakeCursor:
    def __init__(self, fetches):
        self._fetches = list(fetches)
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._fetches.pop(0)


# ---------- h3_8_index_for ----------------------------------------------------

def test_h3_8_index_for_returns_an_int():
    idx = h3_8_index_for(39.74, -104.99)
    assert isinstance(idx, int)
    assert idx > 0


# ---------- credit_points (the primitive) -------------------------------------

def test_credit_points_inserts_and_returns_summary():
    cur = _FakeCursor([(42, _NOW)])
    result = credit_points(cur, account_id=1, action="qr_scan", points=100, lat=39.74, lng=-104.99)
    assert result == {"id": 42, "action": "qr_scan", "points": 100, "created_at": _NOW.isoformat()}
    sql, params = cur.executed[0]
    assert "INSERT INTO user_points" in sql
    assert "ON CONFLICT" in sql


def test_credit_points_returns_none_on_conflict_dedupe():
    # (0,) is the per-ride cap's headroom probe, which now precedes the
    # INSERT for any award attributed to a ride.
    cur = _FakeCursor([(0,), None])
    result = credit_points(cur, account_id=1, action="waypoint", points=2, lat=39.74, lng=-104.99,
                            source_table="tracked_rides", source_id="abc-123")
    assert result is None


def test_credit_points_computes_h3_and_passes_all_fields():
    cur = _FakeCursor([(1, _NOW)])
    credit_points(cur, account_id=7, action="qr_scan", points=100, lat=39.74, lng=-104.99,
                  vehicle_identifier="aaaa000000000000")
    _, params = cur.executed[0]
    account_id, action, points, lat, lng, h3_8, vid, source_table, source_id = params
    assert (account_id, action, points, lat, lng, vid) == (7, "qr_scan", 100, 39.74, -104.99, "aaaa000000000000")
    assert isinstance(h3_8, int)
    assert source_table is None and source_id is None


# ---------- credit_report_points -----------------------------------------------

def test_credit_report_points_maps_every_points_eligible_type():
    for report_type, (action, points) in REPORT_TYPE_POINTS.items():
        cur = _FakeCursor([(1, _NOW)])
        result = credit_report_points(cur, account_id=1, report_type=report_type,
                                       lat=39.74, lng=-104.99,
                                       vehicle_identifier="aaaa000000000000", report_id=99)
        assert result["action"] == action
        assert result["points"] == points


def test_credit_report_points_skips_dead_battery():
    """dead_battery is absent from the user's points list — preserved
    faithfully, not guessed at."""
    cur = _FakeCursor([])
    result = credit_report_points(cur, account_id=1, report_type="dead_battery",
                                   lat=39.74, lng=-104.99,
                                   vehicle_identifier="aaaa000000000000", report_id=99)
    assert result is None
    assert cur.executed == []  # never touches the DB for a non-eligible type


def test_credit_report_points_skips_when_no_location():
    cur = _FakeCursor([])
    result = credit_report_points(cur, account_id=1, report_type="not_rideable",
                                   lat=None, lng=None,
                                   vehicle_identifier="aaaa000000000000", report_id=99)
    assert result is None
    assert cur.executed == []


# ---------- credit_qr_scan_points -----------------------------------------------

def test_credit_qr_scan_points_awards_on_first_scan():
    cur = _FakeCursor([None, (1, _NOW)])  # not-yet-scanned check, then credit_points
    result = credit_qr_scan_points(cur, account_id=1, vehicle_identifier="aaaa000000000000",
                                    lat=39.74, lng=-104.99)
    assert result["action"] == "qr_scan"
    lock_calls = [s for s, _ in cur.executed if "pg_advisory_xact_lock" in s]
    assert len(lock_calls) == 1


def test_credit_qr_scan_points_no_op_on_repeat_scan():
    cur = _FakeCursor([(1,)])  # already scanned
    result = credit_qr_scan_points(cur, account_id=1, vehicle_identifier="aaaa000000000000",
                                    lat=39.74, lng=-104.99)
    assert result is None


# ---------- credit_waypoint_points -----------------------------------------------

def test_credit_waypoint_points_scales_with_count():
    cur = _FakeCursor([(0,), (1, _NOW)])
    result = credit_waypoint_points(cur, account_id=1, vehicle_identifier="aaaa000000000000",
                                     waypoint_count=5, end_lat=39.74, end_lng=-104.99, ride_id="uuid-1")
    assert result["points"] == 10  # 2 * 5


def test_credit_waypoint_points_zero_count_is_a_noop():
    cur = _FakeCursor([])
    result = credit_waypoint_points(cur, account_id=1, vehicle_identifier=None,
                                     waypoint_count=0, end_lat=39.74, end_lng=-104.99, ride_id="uuid-1")
    assert result is None
    assert cur.executed == []


# ---------- credit_gbfs_validation_points -----------------------------------------

def test_credit_gbfs_validation_points_within_threshold():
    cur = _FakeCursor([(0,), (1, _NOW)])
    result = credit_gbfs_validation_points(
        cur, account_id=1, vehicle_identifier="aaaa000000000000",
        end_lat=39.74, end_lng=-104.99, reappear_lat=39.740001, reappear_lng=-104.99,
        ride_id="uuid-1",
    )
    assert result["points"] == 20


def test_credit_gbfs_validation_points_beyond_threshold_is_a_noop():
    cur = _FakeCursor([])
    # ~0.01 degrees latitude is over 1km away — well past 20m.
    result = credit_gbfs_validation_points(
        cur, account_id=1, vehicle_identifier="aaaa000000000000",
        end_lat=39.74, end_lng=-104.99, reappear_lat=39.75, reappear_lng=-104.99,
        ride_id="uuid-1",
    )
    assert result is None
    assert cur.executed == []


def test_credit_gbfs_validation_points_no_reappearance_is_a_noop():
    cur = _FakeCursor([])
    result = credit_gbfs_validation_points(
        cur, account_id=1, vehicle_identifier="aaaa000000000000",
        end_lat=39.74, end_lng=-104.99, reappear_lat=None, reappear_lng=None,
        ride_id="uuid-1",
    )
    assert result is None


# ---------- maybe_credit_profile_completion ---------------------------------------

_COMPLETE_ROW = ("rider@example.com", "resident", "+13035551234", 39.74, -104.99, None, None)
_INCOMPLETE_ROW = ("rider@example.com", "visitor", None, None, None, None, None)


def test_profile_completion_awards_once():
    cur = _FakeCursor([None, _COMPLETE_ROW, (1, _NOW)])
    result = maybe_credit_profile_completion(cur, account_id=1)
    assert result["action"] == "profile_completion"


def test_profile_completion_already_awarded_is_a_noop():
    cur = _FakeCursor([(1,)])  # already has a profile_completion row
    result = maybe_credit_profile_completion(cur, account_id=1)
    assert result is None


def test_profile_completion_incomplete_profile_is_a_noop():
    cur = _FakeCursor([None, _INCOMPLETE_ROW])
    result = maybe_credit_profile_completion(cur, account_id=1)
    assert result is None


def test_profile_completion_uses_work_location_when_home_absent():
    row = ("rider@example.com", "resident", "+13035551234", None, None, 39.75, -105.0)
    cur = _FakeCursor([None, row, (1, _NOW)])
    maybe_credit_profile_completion(cur, account_id=1)
    _, params = cur.executed[-1]
    assert params[3] == 39.75 and params[4] == -105.0
