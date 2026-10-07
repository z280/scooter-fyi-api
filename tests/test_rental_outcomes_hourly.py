"""sql/090: every completed rental lands in rental_outcomes_hourly, in the
same transaction as the per-vehicle counters, keyed by the hour it ended,
the h3_9 cell it was UNLOCKED in, the model and the radius it was counted at.

Same fake cursor as tests/test_device_state_rentals.py."""

from __future__ import annotations

from datetime import timedelta

from src import device_state
from src.config import load
from src.ingest import _h3_cells
from tests.test_device_state_failed_starts import _in_rental, _north
from tests.test_device_state_rentals import (  # noqa: F401  (cycle is a fixture)
    _ORIGIN, _T0, _device, _known, cycle,
)

R = float(load().device_tracking.stationary_threshold_meters)
ORIGIN_CELL = _h3_cells(*_ORIGIN)[1]
HOUR = _T0.replace(minute=0, second=0, microsecond=0)


def _rollup(cur) -> list[tuple]:
    return cur.rows_for("INSERT INTO rental_outcomes_hourly")


def test_a_rental_that_went_nowhere_is_a_no_go_on_both_definitions(cycle):
    stats, cur = cycle([_device(_north(4), device_id="bike-2")], state=_in_rental(max_m=6.0))
    (row,) = _rollup(cur)
    assert row == (HOUR, ORIGIN_CELL, "Unknown", R, 1, 1, 1, 1)


def test_a_round_trip_is_an_end_displacement_no_go_but_not_a_maximum_one(cycle):
    """Out 400 m and back to the rack: rentals_no_go counts it (end
    displacement), "never left the kerb" must not (it left)."""
    stats, cur = cycle([_device(_north(5))], state=_in_rental(max_m=400.0))
    (row,) = _rollup(cur)
    assert row[4:] == (1, 1, 0, 1)
    # The per-vehicle counter is unchanged in meaning: still a no-go.
    assert cur.rows_for("rentals_no_go = rentals_no_go + %s")[0][0] == 1


def test_a_real_trip_is_no_no_go(cycle):
    stats, cur = cycle([_device(_north(1600))], state=_in_rental(max_m=1700.0))
    (row,) = _rollup(cur)
    assert row[4:] == (1, 0, 0, 1)


def test_attributed_to_where_it_was_unlocked_not_where_it_ended(cycle):
    stats, cur = cycle([_device(_north(1600))], state=_in_rental(max_m=1700.0))
    (row,) = _rollup(cur)
    assert row[1] == ORIGIN_CELL
    assert row[1] != _h3_cells(*_north(1600))[1]


def test_an_unknown_maximum_is_not_guessed(cycle):
    """Origin max unknown (rental began before sql/087): counted as a rental,
    no maximum judgement either way."""
    stats, cur = cycle([_device(_north(4), device_id="bike-2")], state=_in_rental(max_m=None))
    (row,) = _rollup(cur)
    assert row[4:] == (1, 1, 0, 0)


def test_every_row_carries_the_radius_it_was_counted_at(cycle):
    stats, cur = cycle([_device(_north(1600))], state=_in_rental(max_m=1700.0))
    assert _rollup(cur)[0][3] == R


def test_no_rollup_without_a_completed_rental(cycle):
    # Parked and staying parked; mid-rental hold.
    _, cur = cycle([_device(_north(2))], state=_known())
    assert not cur.ran("INSERT INTO rental_outcomes_hourly")
    _, cur = cycle([_device(_north(300), is_reserved=True)], state=_in_rental(max_m=100.0))
    assert not cur.ran("INSERT INTO rental_outcomes_hourly")


def test_written_in_the_same_transaction_after_the_counters(cycle):
    _, cur = cycle([_device(_north(4), device_id="bike-2")], state=_in_rental(max_m=6.0))
    sqls = [sql for _, sql, _ in cur.calls]
    i_counter = next(i for i, s in enumerate(sqls) if "rentals_observed = rentals_observed + 1" in s)
    i_rollup = next(i for i, s in enumerate(sqls) if "INSERT INTO rental_outcomes_hourly" in s)
    assert i_rollup > i_counter
    assert "ON CONFLICT (hour, h3_9, model, radius_m) DO UPDATE" in sqls[i_rollup]
