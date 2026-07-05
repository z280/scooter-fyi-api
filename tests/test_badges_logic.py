"""Pure badge helpers (the SQL-facing paths are exercised in deployment)."""

from __future__ import annotations

from datetime import date

from src.badges import _streak_earned_at


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
