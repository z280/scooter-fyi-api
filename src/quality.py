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

BASELINE (from battery percent — recovered SoC, see BATTERY PERCENT)
--------------------------------------------------------------------

    great       : battery ≥ 75%
    good        : battery ≥ 53%   (≈ old 24,140 m / 15 mi cutoff)
    acceptable  : battery ≥ 28%   (≈ old 12,875 m / 8 mi cutoff)
    poor        : battery <  28%

Thresholds were re-expressed from meters to percent when battery percent
switched to exact SoC recovery (API_REQUIREMENTS.md §7.1): the old
"great" rule (≥ 75% of the rated per-type max) was unreachable for
bicycles, whose rated max (67,000 m) exceeds the highest value the feed
ever emits (45,293 m).

DEMERITS (apply in order; each knocks the tier DOWN one step, never
below "poor"):

    failed_starts == 1          : -1 tier
    failed_starts > 1           : straight to "poor"   (override)
    has_negative_report (live)  : straight to "poor"   (override)
    dwell ≥ 24h                 : -2 tiers
    elif dwell ≥ 12h            : -1 tier
    elif daylight-hours ≥ 6     : -1 tier
    dwell-outlier vs peers      : -1 tier   (stacks with the dwell/daylight
                                  demerits above; see DWELL OUTLIERS below)

Daylight hours are counted as the elapsed time since
first_observed_at_location that falls within 8 AM – 8 PM Denver
local. DST-aware via zoneinfo.

N/A OVERRIDES (any of these forces N/A and skips all other rules):

    is_disabled is True
    is_reserved is True
    current_range_meters is None

RELIABILITY TIER (API_REQUIREMENTS.md §1.2, recalibrated 2026-07)
-----------------------------------------------------------------
`compute_reliability_tier` collapses the failure signals into a single
public field answering "will this scooter actually unlock?" — distinct
from quality_designation, which is dominated by battery range. Rules,
evaluated in order (first match wins):

    high_risk : has_negative_report
              | number_failed_starts ≥ 2
              | number_failed_starts == 1 AND dwell ≥ 24h
              | dwell ≥ 72h                       (ghost-scooter idle)
              | dwell-outlier vs peers AND dwell ≥ 48h
    unknown   : device never state-tracked (no plate → both inputs None)
              | quality_designation == "N/A"     (disabled/reserved/rangeless)
              | number_failed_starts == 1         (uncorroborated by dwell)
              | dwell ≥ 2 × peer-median dwell     (softer, earlier-warning
                                                    version of the dwell-
                                                    outlier rule above — just
                                                    the ratio, no percentile
                                                    or absolute-hour floor)
    ok        : everything else

A single failed start no longer stays "ok" — it now reads "unknown" rather
than a clean bill of health, since one bike_id rotation could still be a
rebalancing scan rather than confirmed evidence of a rider failure. Two or
more, or one plus a day of dwell, remain outright high_risk. Dwell counters
reset when the scooter moves (see src/device_state.py), so both inputs are
naturally scoped to the current location — that's the "recent window".

The clean-dwell ghost threshold was recalibrated from 96h to 72h against
the 2026-07-06 production snapshot (8,449 devices): citywide dwell
percentiles were p50=7.2h / p90=48h / p95=76h, so 96h (~p97) was leaving
hundreds of top-decile idlers marked "ok".

DWELL OUTLIERS (peer-relative, API_REQUIREMENTS.md workstream 2026-07)
----------------------------------------------------------------------
Absolute dwell thresholds can't tell "31h idle on a block that turns
over every 6h" (damning) from "31h idle where everything sits a day"
(normal). `compute_dwell_peer_stats` judges each state-tracked device
against its local peers:

    peer set  : all state-tracked devices in gridDisk(h3_r9_cell, 1) —
                the device's res-9 cell plus its 6 neighbors, ~0.74 km²
                centered on the device (same area as an r8 cell without
                the fixed-grid boundary lottery). The device itself is
                a member of its own peer set. If that yields < 5 peers,
                expand to gridDisk(r9, 2); if still < 5, fall back to
                the citywide distribution. Fewer than 5 peers citywide
                → no stats (percentile is None, never an outlier).
    outlier   : dwell percentile ≥ 0.90 among ≥ 5 peers
                AND dwell ≥ 3 × peer-median dwell
                AND dwell ≥ 24h  (absolute floor: a high-turnover block
                                  can't flag an objectively fresh scooter)

The evidence is exposed publicly as `dwell_percentile_hood` (0-100) and
`dwell_peer_median_hours` so the frontend can explain verdicts
("idle 31h — 5× its block's typical 6h") instead of asserting them.
Percentile is the ≤-fraction: share of peers (self included) whose dwell
is ≤ this device's dwell.

BATTERY PERCENT
---------------
`compute_battery_percent` recovers the exact 0-100 integer SoC behind
current_range_meters: the feed emits one fleet-wide 100-value lookup
table for every vehicle type (verified stable across a 37-day archive by
scripts/analyze_range_signal.py), so percent = the value's rank in
data/range_soc_lut.json. Values outside the table (vendor drift) fall
back to linear scaling against the observed 45,293 m full-charge cap and
log a warning. None when range is missing (e.g. pedal-only bikes have no
battery). The per-type rated max is NOT used — it's fiction (a full
bicycle would read 68% against it).
"""

from __future__ import annotations

import json
import logging
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Hashable, Iterable
from zoneinfo import ZoneInfo

import h3

DENVER_TZ = ZoneInfo("America/Denver")

# Tier ladder, worst → best (poor is index 0; "N/A" is sentinel).
_TIERS = ("poor", "acceptable", "good", "great")

# Baseline thresholds (battery percent — true SoC via the lookup table).
# Percent equivalents of the previous meter cutoffs against the observed
# 45,293 m full-charge cap (the rated per-type max they used to be scaled
# by is fiction — see API_REQUIREMENTS.md §7.1).
_GREAT_MIN_PCT = 75
_GOOD_MIN_PCT = 53        # ≈ old 24,140 m (≈ 15 mi nominal)
_ACCEPTABLE_MIN_PCT = 28  # ≈ old 12,875 m (≈ 8 mi nominal)

# Dwell thresholds (hours).
_DWELL_HARD_HOURS = 24.0
_DWELL_SOFT_HOURS = 12.0
_DAYLIGHT_SOFT_HOURS = 6.0

# Daylight window in Denver local time.
_DAY_START_HOUR = 8
_DAY_END_HOUR = 20

# Reliability-tier thresholds (see module docstring).
_RELIABILITY_FS_HARD = 2           # failed starts that alone mean high_risk
_RELIABILITY_FS_DWELL_HOURS = 24.0 # 1 failed start + this much dwell
_RELIABILITY_IDLE_HOURS = 72.0     # dwell alone (ghost scooter; was 96h pre-recalibration)
_RELIABILITY_OUTLIER_DWELL_HOURS = 48.0  # peer-relative dwell outlier + this much dwell
_RELIABILITY_UNKNOWN_DWELL_MULT = 2.0    # dwell ≥ this × peer median alone → unknown

# Dwell-outlier thresholds (see module docstring).
_DWELL_OUTLIER_MIN_PEERS = 5       # below this, widen the ring / fall back
_DWELL_OUTLIER_PERCENTILE = 0.90   # ≤-fraction within the peer set
_DWELL_OUTLIER_MEDIAN_MULT = 3.0   # dwell must be ≥ this × peer median
_DWELL_OUTLIER_FLOOR_HOURS = 24.0  # absolute floor; high-turnover blocks can't flag fresh scooters


def _baseline_tier(battery_percent: int) -> str:
    """Tier from battery percent alone, no demerits."""
    if battery_percent >= _GREAT_MIN_PCT:
        return "great"
    if battery_percent >= _GOOD_MIN_PCT:
        return "good"
    if battery_percent >= _ACCEPTABLE_MIN_PCT:
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


@dataclass(frozen=True)
class DwellPeerStats:
    """Peer-relative dwell evidence for one state-tracked device.

    `percentile` / `peer_median_hours` are None when even the citywide
    fallback has fewer than _DWELL_OUTLIER_MIN_PEERS members — the device
    then can't be an outlier either.
    """

    dwell_hours: float
    percentile: float | None       # ≤-fraction in [0, 1] within the peer set
    peer_median_hours: float | None
    peer_count: int
    is_outlier: bool


def compute_dwell_peer_stats(
    entries: Iterable[tuple[Hashable, int | None, float | None]],
) -> dict[Hashable, DwellPeerStats]:
    """Peer-relative dwell stats for a whole fleet snapshot.

    ``entries`` is (key, h3_9_index, dwell_hours) per device; devices with
    dwell_hours None (not state-tracked) are excluded from peer sets and
    get no stats. h3_9_index is the raw 64-bit integer as stored; devices
    without one only ever compare citywide.

    Peer-set fallback per device: gridDisk(r9 cell, 1) → gridDisk(r9, 2)
    → citywide, stopping at the first ring with ≥ _DWELL_OUTLIER_MIN_PEERS
    members (the device itself included).
    """
    tracked: list[tuple[Hashable, str | None, float]] = []
    cell_dwells: dict[str, list[float]] = {}
    for key, h3_9, dwell in entries:
        if dwell is None:
            continue
        cell = h3.int_to_str(h3_9) if h3_9 is not None else None
        tracked.append((key, cell, dwell))
        if cell is not None:
            cell_dwells.setdefault(cell, []).append(dwell)

    citywide = sorted(d for _, _, d in tracked)
    citywide_median = median(citywide) if citywide else None

    def _gather(cells: Iterable[str]) -> list[float]:
        out: list[float] = []
        for c in cells:
            out.extend(cell_dwells.get(c, ()))
        return out

    stats: dict[Hashable, DwellPeerStats] = {}
    for key, cell, dwell in tracked:
        peers: list[float] | None = None
        peer_median: float | None = None
        if cell is not None:
            for k in (1, 2):
                ring = _gather(h3.grid_disk(cell, k))
                if len(ring) >= _DWELL_OUTLIER_MIN_PEERS:
                    peers = sorted(ring)
                    peer_median = median(peers)
                    break
        if peers is None:
            peers = citywide
            peer_median = citywide_median

        n = len(peers)
        if n < _DWELL_OUTLIER_MIN_PEERS:
            stats[key] = DwellPeerStats(
                dwell_hours=dwell, percentile=None, peer_median_hours=None,
                peer_count=n, is_outlier=False,
            )
            continue

        pctl = bisect_right(peers, dwell) / n
        assert peer_median is not None  # n ≥ min peers ⇒ median exists
        stats[key] = DwellPeerStats(
            dwell_hours=dwell,
            percentile=pctl,
            peer_median_hours=peer_median,
            peer_count=n,
            is_outlier=(
                pctl >= _DWELL_OUTLIER_PERCENTILE
                and dwell >= _DWELL_OUTLIER_MEDIAN_MULT * peer_median
                and dwell >= _DWELL_OUTLIER_FLOOR_HOURS
            ),
        )
    return stats


@lru_cache(maxsize=1)
def _soc_lut() -> tuple[int, ...]:
    path = Path(__file__).resolve().parent.parent / "data" / "range_soc_lut.json"
    return tuple(json.loads(path.read_text())["values"])


# Values seen outside the LUT (vendor table drift); warn once per value.
_unknown_range_values: set[int] = set()


def compute_battery_percent(current_range_meters: int | None) -> int | None:
    """0-100 integer battery estimate, None when it can't be derived.

    Exact SoC recovery: the feed's range value is an integer percent
    mapped through a fleet-wide 100-value lookup table (see
    data/range_soc_lut.json), so percent = rank in that table. A value
    outside the table means the vendor table drifted — fall back to
    linear scaling against the observed full-charge cap and warn so the
    LUT gets re-derived (scripts/analyze_range_signal.py section 2).
    """
    if current_range_meters is None:
        return None
    lut = _soc_lut()
    r = int(current_range_meters)
    i = bisect_left(lut, r)
    if i < len(lut) and lut[i] == r:
        return round(100 * i / (len(lut) - 1))
    if r not in _unknown_range_values:
        _unknown_range_values.add(r)
        logging.getLogger(__name__).warning(
            "current_range_meters=%d not in the SoC lookup table — vendor "
            "table drift? Re-derive data/range_soc_lut.json", r,
        )
    pct = round(100 * r / lut[-1])
    return max(0, min(100, pct))


def compute_quality_designation(
    *,
    current_range_meters: int | None,
    is_disabled: bool | None,
    is_reserved: bool | None,
    number_failed_starts: int | None,
    first_observed_at_location: datetime | None,
    has_negative_report: bool,
    is_dwell_outlier: bool = False,
    now: datetime | None = None,
) -> str:
    """Return one of "N/A", "poor", "acceptable", "good", "great"."""
    # N/A overrides
    if is_disabled or is_reserved or current_range_meters is None:
        return "N/A"

    tier = _baseline_tier(compute_battery_percent(current_range_meters))

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

    if is_dwell_outlier:
        tier = _knock_down(tier, 1)

    return tier


def compute_reliability_tier(
    *,
    number_failed_starts: int | None,
    first_observed_at_location: datetime | None,
    quality_designation: str,
    has_negative_report: bool,
    is_dwell_outlier: bool = False,
    peer_median_dwell_hours: float | None = None,
    now: datetime | None = None,
) -> str:
    """Return "ok", "unknown", or "high_risk". Rules in module docstring."""
    fs = number_failed_starts or 0
    if first_observed_at_location is not None:
        now = now or datetime.now(timezone.utc)
        dwell_hours = (now - first_observed_at_location).total_seconds() / 3600.0
    else:
        dwell_hours = 0.0

    if has_negative_report or fs >= _RELIABILITY_FS_HARD:
        return "high_risk"
    if fs == 1 and dwell_hours >= _RELIABILITY_FS_DWELL_HOURS:
        return "high_risk"
    if dwell_hours >= _RELIABILITY_IDLE_HOURS:
        return "high_risk"
    if is_dwell_outlier and dwell_hours >= _RELIABILITY_OUTLIER_DWELL_HOURS:
        return "high_risk"

    if number_failed_starts is None and first_observed_at_location is None:
        return "unknown"  # never state-tracked (upstream payload had no plate)
    if quality_designation == "N/A":
        return "unknown"
    if fs == 1:
        return "unknown"  # uncorroborated by dwell, but no longer a clean "ok"
    if (
        peer_median_dwell_hours is not None
        and peer_median_dwell_hours > 0
        and dwell_hours >= _RELIABILITY_UNKNOWN_DWELL_MULT * peer_median_dwell_hours
    ):
        return "unknown"

    return "ok"
