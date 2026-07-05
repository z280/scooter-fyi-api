"""Daily trip/popularity rollup — window math."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from src.daily_trips import day_bounds_for_date

DENVER = ZoneInfo("America/Denver")


def test_day_bounds_span_exactly_24h_denver_local():
    start, end = day_bounds_for_date(date(2026, 6, 15))
    assert (end - start).total_seconds() == 24 * 3600
    assert start.astimezone(DENVER).time().hour == 0
    assert start.astimezone(DENVER).date() == date(2026, 6, 15)
    assert end.astimezone(DENVER).date() == date(2026, 6, 16)


def test_day_bounds_are_utc():
    start, end = day_bounds_for_date(date(2026, 6, 15))
    assert start.tzinfo == timezone.utc
    assert end.tzinfo == timezone.utc


def test_day_bounds_across_spring_forward_dst():
    """2026-03-08 is Denver's spring-forward day. Wall-clock midnight to
    midnight is genuinely only 23 hours of real elapsed time that day (one
    hour vanishes at 2am) — using wall-clock semantics (midnight_local,
    midnight_local + 1 day) rather than a hardcoded 24h window is exactly
    what makes this correct, mirroring daily_sla.window_for_date's same
    DST-aware convention."""
    start, end = day_bounds_for_date(date(2026, 3, 8))
    assert (end - start).total_seconds() == 23 * 3600
    assert start.astimezone(DENVER).date() == date(2026, 3, 8)
    assert end.astimezone(DENVER).date() == date(2026, 3, 9)


def test_day_bounds_across_fall_back_dst():
    """2026-11-01 is Denver's fall-back day — 25 real hours."""
    start, end = day_bounds_for_date(date(2026, 11, 1))
    assert (end - start).total_seconds() == 25 * 3600
