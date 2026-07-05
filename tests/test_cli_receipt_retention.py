"""Calendar-month arithmetic for the receipt-retention cutoff.

18 months is not `18 * 30` days — that shortcut under-counts by up to
~9 days over the window, which would delete receipts before the
documented retention actually elapses.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.cli import _months_ago


def test_18_months_ago_crosses_year_boundary():
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    assert _months_ago(now, 18) == datetime(2025, 1, 4, 12, 0, tzinfo=timezone.utc)


def test_1_month_ago_same_day():
    now = datetime(2026, 3, 15, tzinfo=timezone.utc)
    assert _months_ago(now, 1) == datetime(2026, 2, 15, tzinfo=timezone.utc)


def test_clamps_to_shorter_target_month():
    """Aug 31 minus 18 months lands on Feb, which has no 31st."""
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    out = _months_ago(now, 18)
    assert out == datetime(2025, 2, 28, tzinfo=timezone.utc)


def test_is_stricter_than_naive_30_day_multiplication():
    """The bug this guards against: 18*30=540 days is ~9 days short of a
    true 18-calendar-month span, which would purge receipts too early."""
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    from datetime import timedelta
    naive_cutoff = now - timedelta(days=18 * 30)
    calendar_cutoff = _months_ago(now, 18)
    assert calendar_cutoff < naive_cutoff
