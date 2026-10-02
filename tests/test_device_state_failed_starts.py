"""sql/087: failed starts that end a rental, jitter, decay — fake cursor.

Same fake as tests/test_device_state_rentals.py (it records SQL and
parameters and answers update_for_cycle's reads). The Postgres version of
the same rules, end to end over several cycles, is
tests/test_device_state_failed_starts_pg.py.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from src import device_state
from tests.test_device_state_rentals import (  # noqa: F401  (cycle is a fixture)
    _ORIGIN, _T0, _VID, _device, _known, cycle,
)

_M_PER_DEG_LAT = 111_320.0


def _north(meters: float, of=_ORIGIN) -> tuple[float, float]:
    return (of[0] + meters / _M_PER_DEG_LAT, of[1])


def _in_rental(*, max_m: float | None = 3.0, origin_device_id="bike-1", fs=0) -> dict:
    """Known state of a vehicle mid-rental, origin at _ORIGIN, since _T0-4min."""
    s = _known(rental_started_at=_T0 - timedelta(minutes=4))
    s["rental_max_distance_m"] = max_m
    s["rental_origin_device_id"] = origin_device_id
    s["number_failed_starts"] = fs
    return s


def _update(cur, needle: str) -> tuple:
    rows = cur.rows_for(needle)
    assert len(rows) == 1, (needle, rows)
    return rows[0]


# ---------------------------------------------------------------------------
# Rule 1: in-place release = failed start
# ---------------------------------------------------------------------------

def test_in_place_release_with_rotation_is_a_failed_start_that_keeps_dwell(cycle):
    stats, cur = cycle([_device(_north(4), device_id="bike-2")], state=_in_rental())
    assert (stats.rentals_ended, stats.failed_starts, stats.rentals_failed_start) == (1, 1, 1)
    assert stats.moved == 0
    # No trip, no new stop, the closed one is reopened.
    assert not cur.ran("INSERT INTO trip_events")
    assert not cur.ran("INSERT INTO device_history")
    assert cur.ran("departed_at = NULL")
    # Position and dwell clock untouched; counter +1; mask pushes a 1.
    assert not cur.ran("current_lat = %s")
    assert not cur.ran("first_observed_at_location = %s")
    row = _update(cur, "number_failed_starts = number_failed_starts + %s")
    assert row[0] == "bike-2"            # bike_id picked up
    assert row[6] == 1                   # failed-start increment
    assert row[7] == row[8] == 1         # mask push (IS NULL test, value)
    assert cur.ran("rental_max_distance_m = NULL")
    assert cur.ran("rental_started_at = NULL")
    # sql/072 counters keep counting exactly as before.
    assert stats.rentals_no_go == 1
    assert cur.rows_for("rentals_no_go = rentals_no_go + %s") == [(1, _VID)]


def test_in_place_failure_ending_beyond_the_old_16m_still_counts(cycle):
    """Ended 40 m from the unlock point, never farther than 45 m: a failed
    start now; pre-087 it was a 40 m 'trip' that reset the counter."""
    stats, cur = cycle([_device(_north(40), device_id="bike-2")],
                       state=_in_rental(max_m=45.0))
    assert (stats.failed_starts, stats.moved) == (1, 0)
    assert not cur.ran("INSERT INTO trip_events")
    # Not a no-go in sql/072's 16 m sense, and that counter is unchanged.
    assert stats.rentals_no_go == 0


def test_reservation_blip_without_rotation_counts_for_nothing(cycle):
    stats, cur = cycle([_device(_north(2), device_id="bike-1")], state=_in_rental())
    assert (stats.failed_starts, stats.rentals_blip, stats.moved) == (0, 1, 0)
    assert not cur.ran("INSERT INTO trip_events")
    assert cur.ran("departed_at = NULL")          # stop reopened all the same
    row = _update(cur, "number_failed_starts = number_failed_starts + %s")
    assert row[6] == 0
    assert row[7] is None and row[8] is None      # nothing pushed to the mask


def test_rotation_at_rental_start_still_reads_as_rotated_at_release(cycle):
    """Veo sometimes rotates at the start instead of the release; the
    comparison is against the bike_id from before the rental."""
    state = _in_rental(origin_device_id="bike-0")
    state["device_id"] = "bike-1"                 # what rental start stored
    stats, _ = cycle([_device(_north(3), device_id="bike-1")], state=state)
    assert stats.failed_starts == 1


def test_round_trip_that_left_the_circle_is_not_a_failed_start(cycle):
    """Went 600 m away and came back: a ride. Old round-trip behaviour —
    new stop, dwell reset, no trip row — and a success in the mask."""
    stats, cur = cycle([_device(_north(5), device_id="bike-2")],
                       state=_in_rental(max_m=600.0, fs=1))
    assert (stats.failed_starts, stats.moved) == (0, 0)
    assert not cur.ran("INSERT INTO trip_events")
    assert len(cur.rows_for("INSERT INTO device_history")) == 1
    row = _update(cur, "first_observed_at_location = %s")
    assert row[6] == _T0                  # dwell reset
    assert row[7] is False                # 5 m end: does not clear the count
    assert row[8] == row[9] == 0          # mask push: went somewhere


def test_unknown_origin_takes_the_pre_087_path(cycle):
    """rental_max_distance_m NULL: first seen mid-rental, or already in a
    rental when sql/087 was applied. Never judged in place."""
    stats, cur = cycle([_device(_north(3), device_id="bike-2")],
                       state=_in_rental(max_m=None))
    assert stats.failed_starts == 0
    assert len(cur.rows_for("INSERT INTO device_history")) == 1
    assert not cur.ran("departed_at = NULL")


def test_vanished_mid_rental_gets_a_new_stop_not_a_reopened_one(cycle):
    state = _in_rental()
    state["last_observed_at"] = _T0 - 3 * device_state.ABSENT_STOP_AFTER
    stats, cur = cycle([_device(_north(3), device_id="bike-2")], state=state)
    assert stats.failed_starts == 1
    assert not cur.ran("departed_at = NULL")
    inserted = cur.rows_for("INSERT INTO device_history")
    assert len(inserted) == 1 and inserted[0][9] == 1   # dwell_failed_starts


# ---------------------------------------------------------------------------
# Rental bookkeeping that rule 1 needs
# ---------------------------------------------------------------------------

def test_rental_start_records_origin_bike_id_and_distance(cycle):
    _, cur = cycle([_device(_north(10), is_reserved=True, device_id="bike-9")],
                   state=_known(device_id="bike-1"))
    row = _update(cur, "rental_origin_device_id = %s")
    assert row[1] == "bike-9"                         # current_device_id
    assert row[5] == pytest.approx(10, abs=0.5)       # rental_max_distance_m
    assert row[6] == "bike-1"                         # rental_origin_device_id


def test_rental_hold_keeps_the_running_maximum(cycle):
    _, cur = cycle([_device(_north(30), is_reserved=True)], state=_in_rental(max_m=80.0))
    assert _update(cur, "rental_max_distance_m = %s WHERE")[3] == 80.0
    _, cur = cycle([_device(_north(120), is_reserved=True)], state=_in_rental(max_m=80.0))
    assert _update(cur, "rental_max_distance_m = %s WHERE")[3] == pytest.approx(120, abs=0.5)
    _, cur = cycle([_device(_north(120), is_reserved=True)], state=_in_rental(max_m=None))
    assert _update(cur, "rental_max_distance_m = %s WHERE")[3] is None


# ---------------------------------------------------------------------------
# Rules 2 and 3: jitter is not a move; a rotation inside it is a failed start
# ---------------------------------------------------------------------------

def test_jitter_under_50m_does_not_move_reset_or_write_a_trip(cycle):
    state = _known()
    state["number_failed_starts"] = 1
    stats, cur = cycle([_device(_north(30))], state=state)
    assert (stats.moved, stats.stationary, stats.jitter_held) == (0, 1, 1)
    assert not cur.ran("INSERT INTO trip_events")
    assert not cur.ran("INSERT INTO device_history")
    assert not cur.ran("first_observed_at_location = %s")
    assert not cur.ran("SET departed_at")


def test_unreserved_rotation_at_16_to_50m_is_a_failed_start(cycle):
    stats, cur = cycle([_device(_north(35), device_id="bike-2")], state=_known())
    assert (stats.failed_starts, stats.moved) == (1, 0)
    assert not cur.ran("INSERT INTO trip_events")
    assert cur.ran("number_failed_starts = number_failed_starts + 1")


def test_non_rental_move_beyond_50m_with_rotation_is_moved_with_a_trip(cycle):
    stats, cur = cycle([_device(_north(80), device_id="bike-2")], state=_known())
    assert stats.moved == 1
    assert len(cur.rows_for("INSERT INTO trip_events")) == 1
    row = _update(cur, "first_observed_at_location = %s")
    assert row[8] is None and row[9] is None      # no rental: mask untouched


def test_unrotated_drift_of_50_to_100m_is_still_not_a_move(cycle):
    """Half of the unrotated 50-75 m 'moves' were back where they started
    within two hours: drift. Without a rotation it takes UNROTATED_MOVE_M."""
    stats, cur = cycle([_device(_north(80))], state=_known())
    assert (stats.moved, stats.stationary, stats.jitter_held) == (0, 1, 1)
    assert not cur.ran("INSERT INTO trip_events")
    stats, cur = cycle([_device(_north(150))], state=_known())
    assert stats.moved == 1
    assert len(cur.rows_for("INSERT INTO trip_events")) == 1


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------

def test_ride_beyond_decay_distance_clears_the_count(cycle):
    stats, cur = cycle([_device(_north(1600), device_id="bike-2")],
                       state=_in_rental(max_m=1700.0, fs=2))
    assert stats.moved == 1
    row = _update(cur, "first_observed_at_location = %s")
    assert row[7] is True                          # number_failed_starts := 0
    assert cur.rows_for("INSERT INTO device_history")[0][9] == 0


def test_short_ride_carries_the_count_to_the_new_stop(cycle):
    stats, cur = cycle([_device(_north(200), device_id="bike-2")],
                       state=_in_rental(max_m=220.0, fs=2))
    assert stats.moved == 1
    assert len(cur.rows_for("INSERT INTO trip_events")) == 1
    row = _update(cur, "first_observed_at_location = %s")
    assert row[7] is False                         # count kept
    assert cur.rows_for("INSERT INTO device_history")[0][9] == 2


def test_decay_distance_is_between_the_owner_knee_and_a_km():
    assert 300 <= device_state.FAILED_START_DECAY_M <= 1000
    assert device_state.IN_PLACE_RADIUS_M == device_state.JITTER_RADIUS_M == 50.0
    assert device_state.UNROTATED_MOVE_M == 100.0


# ---------------------------------------------------------------------------
# Rental geometry is measured from the unlock fix, not the stop position
# ---------------------------------------------------------------------------

def test_failure_at_a_drifted_vehicle_is_measured_from_where_it_was_unlocked(cycle):
    """The stop position does not follow drift, so it can be 70 m from the
    vehicle. A failed rental there must not read as a 70 m ride."""
    state = _in_rental(max_m=4.0)
    state["last_fix_lat"], state["last_fix_lon"] = _north(70)
    stats, cur = cycle([_device(_north(73), device_id="bike-2")], state=state)
    assert (stats.failed_starts, stats.moved) == (1, 0)
    assert not cur.ran("INSERT INTO trip_events")
    assert stats.rentals_no_go == 1            # 3 m from the unlock fix


def test_rental_distances_run_from_the_last_fix(cycle):
    state = _known()
    state["last_fix_lat"], state["last_fix_lon"] = _north(70)
    _, cur = cycle([_device(_north(75), is_reserved=True)], state=state)
    assert _update(cur, "rental_origin_device_id = %s")[5] == pytest.approx(5, abs=0.5)


def test_every_unreserved_branch_records_the_fix(cycle):
    """STATIONARY (held drift included), FAILED_START and MOVED all write
    last_fix_*; IN_RENTAL does not, which is what freezes it."""
    for pos, dev in ((_north(30), "bike-1"), (_north(20), "bike-2"), (_north(900), "bike-1")):
        _, cur = cycle([_device(pos, device_id=dev)], state=_known())
        assert cur.ran("last_fix_lat = %s"), (pos, dev)
    _, cur = cycle([_device(_north(30), is_reserved=True)], state=_known())
    assert not cur.ran("last_fix_lat = %s")
