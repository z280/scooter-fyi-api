"""Composite scooter quality designation.

Computed at QUERY time (in /api/v1/devices/current and the private
mirror) rather than stored, so we can iterate the rule set without
re-ingesting. Each call does a handful of arithmetic ops — negligible
at our fleet size.

DESIGNATIONS
------------
Ordered from worst to best:

    "N/A"        — vehicle is disabled, reserved, or has no range data.
                   Quality is undefined; do not show in best-of lists.
    "poor"
    "acceptable"
    "good"
    "great"

BASELINE (from current_range_meters alone)
------------------------------------------

    great       : range ≥ 75% of max_range_meters_for_type
    good        : range ≥ 24,140 m (≈ 15 miles)
    acceptable  : range ≥ 12,875 m (≈ 8 miles)
    poor        : range  <  12,875 m

If `max_range_meters_for_type` is unknown, the "great" tier is
unreachable from baseline (treated as `good` if the absolute threshold
is met).

DEMERITS (apply in order; each knocks the tier DOWN one step, never
below "poor"):

    failed_starts == 1          : -1 tier
    failed_starts > 1           : straight to "poor"   (override)
    has_negative_report (live)  : straight to "poor"   (override)
    dwell ≥ 24h                 : -2 tiers
    elif dwell ≥ 12h            : -1 tier
    elif daylight-hours ≥ 6     : -1 tier

Daylight hours are counted as the elapsed time since
first_observed_at_location that falls within 8 AM – 8 PM Denver
local. DST-aware via zoneinfo.

N/A OVERRIDES (any of these forces N/A and skips all other rules):

    is_disabled is True
    is_reserved is True
    current_range_meters is None
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DENVER_TZ = ZoneInfo("America/Denver")

# Tier ladder, worst → best (poor is index 0; "N/A" is sentinel).
_TIERS = ("poor", "acceptable", "good", "great")

# Baseline thresholds (meters).
_GOOD_MIN_METERS = 24_140        # ≈ 15 mi
_ACCEPTABLE_MIN_METERS = 12_875  # ≈ 8 mi
_GREAT_FRAC_OF_MAX = 0.75

# Dwell thresholds (hours).
_DWELL_HARD_HOURS = 24.0
_DWELL_SOFT_HOURS = 12.0
_DAYLIGHT_SOFT_HOURS = 6.0

# Daylight window in Denver local time.
_DAY_START_HOUR = 8
_DAY_END_HOUR = 20


def _baseline_tier(range_m: int, max_range_m: int | None) -> str:
    """Tier from range alone, no demerits."""
    if max_range_m and max_range_m > 0 and range_m >= max_range_m * _GREAT_FRAC_OF_MAX:
        return "great"
    if range_m >= _GOOD_MIN_METERS:
        return "good"
    if range_m >= _ACCEPTABLE_MIN_METERS:
        return "acceptable"
    return "poor"


def _knock_down(tier: str, steps: int) -> str:
    """Move `steps` rungs down the ladder; floor at 'poor'."""
    idx = _TIERS.index(tier)
    return _TIERS[max(idx - steps, 0)]


def daylight_hours_between(start: datetime, end: datetime) -> float:
    """Hours within Denver 8 AM – 8 PM local that fall in [start, end].

    DST-aware: a "day" in Denver-local terms is a wall-clock day, which
    has 23 / 24 / 25 hours of real time depending on the DST transition.
    The 8a-8p window always covers 12 hours of wall-clock time, but the
    UTC equivalent shifts by 1 hour across DST.
    """
    if end <= start:
        return 0.0
    s = start.astimezone(DENVER_TZ)
    e = end.astimezone(DENVER_TZ)

    total = 0.0
    # Start the cursor at 8 AM on the same Denver-local day as `s`, or the
    # previous day if `s` happens to be before that day's 8 AM.
    cur_day = s.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur_day < e:
        win_start = cur_day.replace(hour=_DAY_START_HOUR)
        win_end = cur_day.replace(hour=_DAY_END_HOUR)
        overlap_start = max(s, win_start)
        overlap_end = min(e, win_end)
        if overlap_end > overlap_start:
            total += (overlap_end - overlap_start).total_seconds() / 3600.0
        cur_day += timedelta(days=1)
    return total


def compute_quality_designation(
    *,
    current_range_meters: int | None,
    max_range_meters_for_type: int | None,
    is_disabled: bool | None,
    is_reserved: bool | None,
    number_failed_starts: int | None,
    first_observed_at_location: datetime | None,
    has_negative_report: bool,
    now: datetime | None = None,
) -> str:
    """Return one of "N/A", "poor", "acceptable", "good", "great"."""
    # N/A overrides
    if is_disabled or is_reserved or current_range_meters is None:
        return "N/A"

    tier = _baseline_tier(current_range_meters, max_range_meters_for_type)

    # Hard overrides (any of these forces "poor")
    if has_negative_report:
        return "poor"
    if (number_failed_starts or 0) > 1:
        return "poor"

    # Demerits
    if (number_failed_starts or 0) == 1:
        tier = _knock_down(tier, 1)

    if first_observed_at_location is not None:
        now = now or datetime.now(timezone.utc)
        elapsed_hours = (now - first_observed_at_location).total_seconds() / 3600.0
        if elapsed_hours >= _DWELL_HARD_HOURS:
            tier = _knock_down(tier, 2)
        elif elapsed_hours >= _DWELL_SOFT_HOURS:
            tier = _knock_down(tier, 1)
        else:
            daylight = daylight_hours_between(first_observed_at_location, now)
            if daylight >= _DAYLIGHT_SOFT_HOURS:
                tier = _knock_down(tier, 1)

    return tier
