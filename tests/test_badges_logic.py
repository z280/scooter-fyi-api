"""Pure badge helpers, plus the tracked_rides-backed mileage/streak badges
(fake cursor — no DB needed)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from src.badges import _MILES_10_M, _ride_badges, _streak_earned_at


def test_streak_found_on_seventh_consecutive_day():
    days = [date(2026, 6, d) for d in range(1, 10)]  # 9 consecutive days
    assert _streak_earned_at(days, 7) == date(2026, 6, 7)


def test_streak_broken_by_gap():
    days = [date(2026, 6, d) for d in (1, 2, 3, 5, 6, 7, 8, 9, 10)]  # gap at 4
    # run restarts at the 5th → completes on the 11th... which isn't present
    # (5,6,7,8,9,10 = only 6 days) → no streak
    assert _streak_earned_at(days, 7) is None


def test_streak_after_gap_can_still_complete():
    days = [date(2026, 6, d) for d in (1, 3, 4, 5, 6, 7, 8, 9)]
    assert _streak_earned_at(days, 7) == date(2026, 6, 9)


def test_no_streak_with_sparse_days():
    days = [date(2026, 6, d) for d in (1, 5, 9, 13)]
    assert _streak_earned_at(days, 7) is None


# ---------- _ride_badges reads tracked_rides ---------------------------------

class _FakeCursor:
    """Records the SQL it was handed and replays canned rows."""

    def __init__(self, rows):
        self._rows = rows
        self.sql = ""

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self._rows


def _ts(day: int) -> datetime:
    return datetime(2026, 6, day, 12, 0, tzinfo=timezone.utc)


def test_ride_badges_span_both_ride_mechanisms():
    """Mileage is the union of tracked (Veo/GBFS) and off-feed rides — a
    rider shouldn't need twice the distance because half their riding was
    on a personal scooter."""
    cur = _FakeCursor([])
    _ride_badges(cur, 1)
    assert "FROM tracked_rides" in cur.sql
    assert "FROM rides" in cur.sql
    assert "UNION ALL" in cur.sql
    # Unfinished rides on either side have no distance and no end date —
    # they must not reach the mileage sum or the streak set.
    assert "user_reported_ended_at IS NOT NULL" in cur.sql
    assert "status = 'completed'" in cur.sql
    # Both halves are scoped to the caller.
    assert cur.params == (1, 1)


def test_no_ride_badges_without_rides():
    assert _ride_badges(_FakeCursor([]), 1) == []


def test_miles_10_earned_on_the_ride_that_crosses_the_threshold():
    half = _MILES_10_M / 2 + 1
    rows = [(_ts(1), half), (_ts(2), half), (_ts(3), half)]
    badges = {b["id"]: b for b in _ride_badges(_FakeCursor(rows), 1)}
    assert "miles_10" in badges
    assert badges["miles_10"]["earned_at"] == _ts(2).isoformat()
    assert "miles_100" not in badges


def test_null_distance_rides_count_toward_streak_but_not_mileage():
    """A ride that ended with no computed distance (distance_meters NULL)
    still happened — it belongs in the streak. It just adds no miles."""
    rows = [(_ts(d), None) for d in range(1, 8)]  # 7 consecutive days
    badges = {b["id"] for b in _ride_badges(_FakeCursor(rows), 1)}
    assert badges == {"streak_7"}


def test_straight_line_and_waypoint_distances_both_count():
    """_ride_badges does not filter on distance_source — see its docstring
    for why undercounting beats excluding."""
    rows = [(_ts(1), _MILES_10_M - 1), (_ts(2), 2.0)]
    badges = {b["id"] for b in _ride_badges(_FakeCursor(rows), 1)}
    assert "miles_10" in badges
