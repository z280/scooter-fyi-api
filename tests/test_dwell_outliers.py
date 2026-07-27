"""Peer-relative dwell outliers + the recalibrated reliability rules.

Covers the five cases the frontend asked to see locked before it drops
its interim client-side 48-96h caution band:

  * sparse-cell fallback (k1 → k2 → citywide → no-stats)
  * the 24h absolute floor
  * a busy-cell outlier (31h where the block median is ~6h)
  * a uniformly-slow cell that must NOT flag
  * the 72h clean-dwell ghost boundary (and the outlier ∧ 48h rule)

Peer sets are built from real h3 cells so gridDisk geometry is exercised
for real — the tests derive neighbor/second-ring cells from the library
rather than hardcoding indexes.
"""

from __future__ import annotations

from datetime import timedelta

import h3

from src.quality import (
    compute_dwell_peer_stats,
    compute_quality_designation,
    compute_reliability_tier,
)
from tests.test_quality import _REL_BASE, _denver

# A res-9 cell in central Denver + useful relatives.
_CELL = h3.latlng_to_cell(39.7392, -104.9876, 9)
_CELL_INT = h3.str_to_int(_CELL)

# A cell in the SECOND ring around _CELL (in gridDisk(_CELL, 2) but not
# gridDisk(_CELL, 1)) — peers there are only reachable via the k=2 fallback.
_RING2_CELL = sorted(set(h3.grid_disk(_CELL, 2)) - set(h3.grid_disk(_CELL, 1)))[0]
_RING2_INT = h3.str_to_int(_RING2_CELL)

# A cell far across town (Montbello) — never inside gridDisk(_CELL, 2).
_FAR_CELL = h3.latlng_to_cell(39.7850, -104.8720, 9)
_FAR_INT = h3.str_to_int(_FAR_CELL)
assert _FAR_CELL not in h3.grid_disk(_CELL, 2)


def _entries(*groups: tuple[int | None, list[float]]) -> list[tuple[str, int | None, float | None]]:
    """Flatten (cell_int, [dwells...]) groups into keyed entries d0, d1, …"""
    out = []
    i = 0
    for cell_int, dwells in groups:
        for d in dwells:
            out.append((f"d{i}", cell_int, d))
            i += 1
    return out


# ---------- peer-set construction & fallbacks --------------------------------
def test_busy_cell_outlier_is_flagged():
    """31h idle where the block's peers sit ~4-8h → outlier."""
    stats = compute_dwell_peer_stats(
        _entries((_CELL_INT, [4.0, 5.0, 6.0, 7.0, 8.0, 31.0]))
    )
    st = stats["d5"]
    assert st.peer_count == 6
    assert st.percentile == 1.0
    assert st.peer_median_hours == 6.5
    assert st.is_outlier  # 31 ≥ 3×6.5, pctl ≥ .9, ≥ 24h floor
    # A mid-pack peer in the same cell is not an outlier.
    assert not stats["d2"].is_outlier


def test_uniformly_slow_cell_does_not_flag():
    """31h idle where EVERYTHING sits ~a day → high percentile but not 3× median."""
    stats = compute_dwell_peer_stats(
        _entries((_CELL_INT, [28.0, 29.0, 30.0, 30.0, 31.0, 31.5]))
    )
    st = stats["d5"]
    assert st.percentile >= 0.9
    assert not st.is_outlier  # 31.5 < 3 × 30


def test_24h_absolute_floor():
    """A high-turnover block can't flag a scooter that's objectively fresh."""
    stats = compute_dwell_peer_stats(
        _entries((_CELL_INT, [1.0, 1.5, 2.0, 2.0, 3.0, 20.0]))
    )
    st = stats["d5"]
    assert st.percentile == 1.0
    assert st.dwell_hours >= 3 * st.peer_median_hours
    assert not st.is_outlier  # 20h < 24h floor


def test_sparse_cell_falls_back_to_second_ring():
    """<5 peers in gridDisk(k=1) → widen to k=2 before going citywide."""
    stats = compute_dwell_peer_stats(
        _entries(
            (_CELL_INT, [40.0]),                       # the device, alone in its cell
            (_RING2_INT, [5.0, 6.0, 6.0, 7.0, 8.0]),   # peers only reachable at k=2
            (_FAR_INT, [1.0] * 20),                    # noise the k-ring must NOT see
        )
    )
    st = stats["d0"]
    assert st.peer_count == 6  # self + the 5 ring-2 peers; far cluster excluded
    assert st.peer_median_hours == 6.5  # median of [5, 6, 6, 7, 8, 40]
    assert st.is_outlier  # 40 ≥ 3×6.5, top percentile, ≥ 24h


def test_isolated_cell_falls_back_to_citywide():
    """<5 peers even at k=2 → judged against the citywide distribution."""
    stats = compute_dwell_peer_stats(
        _entries(
            (_CELL_INT, [40.0, 40.0]),   # isolated pair
            (_FAR_INT, [1.0] * 20),      # the rest of the fleet, far away
        )
    )
    st = stats["d0"]
    assert st.peer_count == 22  # citywide
    assert st.peer_median_hours == 1.0
    assert st.is_outlier


def test_no_h3_cell_uses_citywide():
    stats = compute_dwell_peer_stats(
        _entries((None, [40.0]), (_FAR_INT, [1.0] * 20))
    )
    st = stats["d0"]
    assert st.peer_count == 21
    assert st.is_outlier


def test_under_5_devices_citywide_yields_no_stats():
    """percentile/median null, never an outlier — matches the API's null contract."""
    stats = compute_dwell_peer_stats(_entries((_CELL_INT, [40.0, 1.0, 2.0])))
    st = stats["d0"]
    assert st.percentile is None
    assert st.peer_median_hours is None
    assert not st.is_outlier


def test_untracked_devices_get_no_stats_and_pollute_nothing():
    entries = _entries((_CELL_INT, [4.0, 5.0, 6.0, 7.0, 31.0]))
    entries.append(("untracked", _CELL_INT, None))
    stats = compute_dwell_peer_stats(entries)
    assert "untracked" not in stats
    assert stats["d4"].peer_count == 5  # the None entry didn't join the peer set


# ---------- reliability wiring ------------------------------------------------
def test_ghost_rule_72h_boundary():
    just_under = compute_reliability_tier(**{
        **_REL_BASE,
        "now": _denver("2026-06-04", 9, 59),  # 71h59m
    })
    at_boundary = compute_reliability_tier(**{
        **_REL_BASE,
        "now": _denver("2026-06-04", 10),     # 72h
    })
    assert just_under == "ok"
    assert at_boundary == "high_risk"


def test_dwell_outlier_at_48h_is_high_risk():
    out = compute_reliability_tier(**{
        **_REL_BASE,
        "is_dwell_outlier": True,
        "now": _denver("2026-06-03", 10),  # 48h
    })
    assert out == "high_risk"


def test_dwell_outlier_under_48h_is_unknown():
    """Below the high_risk floor (48h), but the ratio rule still catches it
    as unknown — the same evidence just isn't damning enough yet."""
    out = compute_reliability_tier(**{
        **_REL_BASE,
        "is_dwell_outlier": True,
        "peer_median_dwell_hours": 10.0,
        "now": _denver("2026-06-03", 9),  # 47h dwell, 4.7x the 10h peer median
    })
    assert out == "unknown"


def test_high_risk_outlier_beats_unknown_dwell_ratio():
    """When both the strict (3x/p90/48h) and the loose (2x-median) rules
    would fire, the more severe high_risk verdict wins — first-match-wins."""
    out = compute_reliability_tier(**{
        **_REL_BASE,
        "is_dwell_outlier": True,
        "peer_median_dwell_hours": 10.0,
        "now": _denver("2026-06-03", 10),  # 48h dwell, 4.8x the 10h peer median
    })
    assert out == "high_risk"


def test_non_outlier_48h_dwell_stays_ok():
    """48h alone (no outlier flag, no peer median, no failed starts) is
    under the 72h ghost rule."""
    out = compute_reliability_tier(**{
        **_REL_BASE,
        "now": _denver("2026-06-03", 10),  # 48h
    })
    assert out == "ok"


def test_one_fresh_failed_start_is_now_unknown_not_ok():
    """A single failed start used to be pure leniency ("ok"); it now demotes
    to unknown instead — still short of high_risk without dwell to back it."""
    out = compute_reliability_tier(**{
        **_REL_BASE,
        "number_failed_starts": 1,
        "is_dwell_outlier": False,
    })
    assert out == "unknown"


# ---------- quality-designation wiring ----------------------------------------
def test_outlier_demerit_stacks_with_dwell_demerit():
    """great baseline − 2 (dwell ≥ 24h) − 1 (outlier) → poor."""
    base = dict(
        current_range_meters=50_000,
        is_disabled=False,
        is_reserved=False,
        number_failed_starts=0,
        has_negative_report=False,
        first_observed_at_location=_denver("2026-06-01", 10),
        now=_denver("2026-06-02", 17),  # 31h dwell
    )
    without_flag = compute_quality_designation(**base)
    with_flag = compute_quality_designation(**base, is_dwell_outlier=True)
    assert without_flag == "acceptable"
    assert with_flag == "poor"
