"""Tests for src/points.py's ledger primitives against a fake cursor with
a scripted fetchone() queue."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.points import (
    settle_referrals_for_account,
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


# ---------- referrals & stand-downs (sql/076-078) -----------------------------

class _RefCursor(_FakeCursor):
    """Adds `fetchall`, which the referral sweep needs and the base fake
    (built for single-row credit_points) does not have."""

    def __init__(self, fetches, rows):
        super().__init__(fetches)
        self._rows = list(rows)

    def fetchall(self):
        return self._rows.pop(0) if self._rows else []


NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def _sql_for(cur, needle):
    return [s for s, _ in cur.executed if needle in s]


def test_a_completed_ride_pays_the_referrer():
    """Activation is a COMPLETED RIDE, not a signup — a lead who never rides
    has not been referred to anything (sql/076)."""
    cur = _RefCursor(
        fetches=[
            ("new@example.com", "+13035550142"),   # the rider's contacts
            (1, NOW),                              # credit_points -> referrer
        ],
        rows=[[(7, "Resourceful 🌈", 100, "referral", 0, None, 39.74, -104.99)]],
    )
    cur.fetchone = _seq(cur, [
        ("new@example.com", "+13035550142"),
        (42,),          # _account_id_for_username -> referrer's id
        (1, NOW),       # credit_points
    ])
    out = settle_referrals_for_account(cur, account_id=9, lat=39.7, lng=-104.9)
    assert [r["action"] for r in out] == ["referral"]
    assert out[0]["points"] == 100
    # Marked paid, so a second ride cannot pay it again.
    assert _sql_for(cur, "SET awarded_at = NOW()")


def test_a_stand_down_pays_BOTH_people():
    """Two debts, two payees, one row (sql/077): the holder's 100 for the
    introduction and the newcomer's 300 for walking away."""
    cur = _RefCursor(fetches=[], rows=[
        [(8, "Resourceful 🌈", 100, "stand_down", 300, LATER, 39.74, -104.99)],
    ])
    cur.fetchone = _seq(cur, [
        ("new@example.com", "+13035550142"),
        (42,),          # referrer id
        (1, NOW),       # referrer credit
        (2, NOW),       # newcomer credit
    ])
    out = settle_referrals_for_account(cur, account_id=9, lat=39.7, lng=-104.9)
    assert sorted(r["action"] for r in out) == ["referral", "stand_down"]
    assert {r["action"]: r["points"] for r in out} == {"referral": 100, "stand_down": 300}


def test_an_expired_stand_down_pays_the_referrer_but_NOT_the_newcomer():
    """The page promises the newcomer's points "when you start a ride on
    scooter.fyi today". Printing an expiry the payout ignores would be worse
    than not printing one — but the introduction still happened, so the
    referrer is still owed."""
    past = datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)
    cur = _RefCursor(fetches=[], rows=[
        [(9, "Resourceful 🌈", 100, "stand_down", 300, past, 39.74, -104.99)],
    ])
    cur.fetchone = _seq(cur, [
        ("new@example.com", "+13035550142"),
        (42,),
        (1, NOW),
    ])
    out = settle_referrals_for_account(cur, account_id=9, lat=39.7, lng=-104.9)
    assert [r["action"] for r in out] == ["referral"]


def test_a_missing_referrer_does_not_stop_the_newcomer_being_paid():
    """The referrer is stored as a public username so a referral survives a
    handle change (sql/076), which means resolving it back can legitimately
    MISS. That must not cost the newcomer their points."""
    cur = _RefCursor(fetches=[], rows=[
        [(10, "Gone 👻", 100, "stand_down", 300, LATER, 39.74, -104.99)],
    ])
    cur.fetchone = _seq(cur, [
        ("new@example.com", "+13035550142"),
        None,           # username no longer resolves
        (2, NOW),       # newcomer still credited
    ])
    out = settle_referrals_for_account(cur, account_id=9, lat=39.7, lng=-104.9)
    assert [r["action"] for r in out] == ["stand_down"]


def test_an_account_with_no_contacts_settles_nothing():
    cur = _RefCursor(fetches=[], rows=[])
    cur.fetchone = _seq(cur, [(None, None)])
    assert settle_referrals_for_account(cur, account_id=9, lat=39.7, lng=-104.9) == []


def test_points_land_where_the_referral_was_MADE():
    """"You get 100 points at the geographic spot where you referred them" —
    the claim's position, not the ride's end."""
    cur = _RefCursor(fetches=[], rows=[
        [(11, "Resourceful 🌈", 100, "referral", 0, None, 39.111, -104.222)],
    ])
    cur.fetchone = _seq(cur, [
        ("new@example.com", None),
        (42,),
        (1, NOW),
    ])
    settle_referrals_for_account(cur, account_id=9, lat=39.9, lng=-104.9)
    insert = [p for s, p in cur.executed if "INSERT INTO user_points" in s][0]
    assert insert[3] == 39.111 and insert[4] == -104.222


def _seq(cur, values):
    """fetchone() returning a scripted sequence."""
    it = iter(values)

    def _next():
        return next(it)

    return _next
