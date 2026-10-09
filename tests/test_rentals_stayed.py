"""sql/098: "never left the spot" (50 m), beside the unchanged 25 m no-go.

A rental STAYED when the vehicle never got more than IN_PLACE_RADIUS_M from
its unlock point (final fix included) and was released inside it: every
in-place release, failed start and blip alike. rentals_no_go (end
displacement within the stationary threshold, round trips included) is not
redefined.

Same fake cursor as tests/test_device_state_rentals.py; the grade and the
devices payload are tested without a database.
"""

from __future__ import annotations

import pytest

from src import api_public, device_state
from src.config import load
from src.device_state import IN_PLACE_RADIUS_M
from src.quality import (
    GRADE_FLEET_STAYED_RATE, GRADE_MIN_RENTALS, GRADE_POINTS_PER_RATE, smart_ride_grade,
)
from tests.test_device_state_failed_starts import _in_rental, _north
from tests.test_device_state_rentals import _VID, _device, cycle  # noqa: F401  (fixture)
from tests.test_devices_route import client  # noqa: F401  (fixture)

R = float(load().device_tracking.stationary_threshold_meters)

# Row layout of the rollup upsert: (hour, h3_9, model, radius_m, equity_area,
# rentals, no_gos, no_gos_max, max_known, origin_unknown, stayed, stayed_known)
NO_GOS, STAYED, STAYED_KNOWN = 6, 10, 11


@pytest.fixture(autouse=True)
def _equity_layer(monkeypatch):
    monkeypatch.setattr(device_state, "_region_for_point", lambda *a: "EQ_007")


def _counters(cur) -> tuple:
    """(no_go, stayed, vid) from the per-vehicle counter UPDATE."""
    (row,) = cur.rows_for("rentals_stayed = rentals_stayed + %s")
    return row


def _rollup(cur) -> tuple:
    (row,) = cur.rows_for("INSERT INTO rental_outcomes_hourly")
    return row


def test_the_radii_are_the_ones_the_owner_decided():
    assert R == 25.0
    assert IN_PLACE_RADIUS_M == 50.0


# --- the per-vehicle counter --------------------------------------------------

def test_a_round_trip_beyond_50m_is_a_no_go_but_did_not_stay(cycle):
    """Out 400 m, back to 5 m from the rack: ended where it began (no-go),
    but it left the spot."""
    stats, cur = cycle([_device(_north(5))], state=_in_rental(max_m=400.0))
    assert _counters(cur) == (1, 0, _VID)
    assert (stats.rentals_no_go, stats.rentals_stayed) == (1, 0)
    row = _rollup(cur)
    assert (row[NO_GOS], row[STAYED], row[STAYED_KNOWN]) == (1, 0, 1)


def test_an_in_place_failed_start_is_both(cycle):
    stats, cur = cycle([_device(_north(4), device_id="bike-2")], state=_in_rental(max_m=6.0))
    assert _counters(cur) == (1, 1, _VID)
    assert (stats.rentals_no_go, stats.rentals_stayed, stats.rentals_failed_start) == (1, 1, 1)
    row = _rollup(cur)
    assert (row[NO_GOS], row[STAYED], row[STAYED_KNOWN]) == (1, 1, 1)


def test_an_in_place_blip_without_rotation_also_stayed(cycle):
    """The owner's question is "never left the spot", not "was it an
    attempt": a blip counts as stayed though it is no failed start."""
    stats, cur = cycle([_device(_north(2), device_id="bike-1")], state=_in_rental(max_m=3.0))
    assert (stats.rentals_blip, stats.failed_starts) == (1, 0)
    assert _counters(cur) == (1, 1, _VID)
    assert _rollup(cur)[STAYED] == 1


def test_a_25_to_50m_drop_that_never_left_50m_stayed_but_is_no_no_go(cycle):
    stats, cur = cycle([_device(_north(40), device_id="bike-2")], state=_in_rental(max_m=45.0))
    assert _counters(cur) == (0, 1, _VID)
    assert (stats.rentals_no_go, stats.rentals_stayed) == (0, 1)
    row = _rollup(cur)
    assert (row[NO_GOS], row[STAYED]) == (0, 1)


def test_the_final_fix_counts_toward_the_maximum(cycle):
    """Never past 30 m while reserved, released 60 m out: it left."""
    stats, cur = cycle([_device(_north(60), device_id="bike-2")], state=_in_rental(max_m=30.0))
    assert _counters(cur) == (0, 0, _VID)
    assert _rollup(cur)[STAYED] == 0


def test_a_real_ride_is_neither(cycle):
    stats, cur = cycle([_device(_north(1600))], state=_in_rental(max_m=1700.0))
    assert _counters(cur) == (0, 0, _VID)


def test_an_unknown_maximum_is_never_counted_as_stayed(cycle):
    """Origin max unknown (first seen mid-rental, or begun before sql/087):
    still a rental in both denominators, never claimed to have stayed."""
    stats, cur = cycle([_device(_north(4), device_id="bike-2")], state=_in_rental(max_m=None))
    assert _counters(cur) == (1, 0, _VID)
    row = _rollup(cur)
    assert (row[STAYED], row[STAYED_KNOWN]) == (0, 1)


def test_every_release_feeds_both_denominators_in_one_update(cycle):
    """stayed is a subset of rentals_observed: both denominators are bumped in
    the same statement as the numerators, for every release."""
    _, cur = cycle([_device(_north(1600))], state=_in_rental(max_m=1700.0))
    (_, sql, _) = next(c for c in cur.calls if "rentals_stayed = rentals_stayed + %s" in c[1])
    assert "rentals_observed = rentals_observed + 1" in sql
    assert "rentals_observed_stayed_era = rentals_observed_stayed_era + 1" in sql
    assert "rentals_no_go = rentals_no_go + %s" in sql


def test_the_rollup_upsert_adds_stayed(cycle):
    _, cur = cycle([_device(_north(4), device_id="bike-2")], state=_in_rental(max_m=6.0))
    sql = next(s for _, s, _ in cur.calls if "INSERT INTO rental_outcomes_hourly" in s)
    assert "stayed = rental_outcomes_hourly.stayed + EXCLUDED.stayed" in sql
    assert "stayed_known = rental_outcomes_hourly.stayed_known + EXCLUDED.stayed_known" in sql
    assert len(_rollup(cur)) == 12


# --- the grade ----------------------------------------------------------------

def test_the_fleet_prior_is_the_measured_stayed_rate():
    assert GRADE_FLEET_STAYED_RATE == 0.021
    # Old slope scaled by the ratio of the fleet rates, 0.085 -> 0.021.
    assert GRADE_POINTS_PER_RATE == pytest.approx(210.0 * 0.085 / 0.021)


def test_round_trips_no_longer_lower_the_grade(client, monkeypatch):
    """Two vehicles, 40 rentals each since sql/098, none stayed: one has 30
    round-trip no-gos, the other none. Same grade."""
    def grade(no_gos):
        monkeypatch.setattr(api_public, "_rental_outcomes",
                            lambda: {"8c4a1f0d2e9b7a35": (40, no_gos, 0, 0, 40)})
        (f,) = client.get("/api/v1/devices/current").json()["features"]
        return f["properties"]

    loops, clean = grade(30), grade(0)
    assert loops["smart_ride_grade"] == clean["smart_ride_grade"] is not None
    assert loops["rentals_no_go"] == 30          # still published, unchanged
    assert (loops["rentals_stayed"], loops["rentals_observed_stayed_era"]) == (0, 40)


def test_staying_put_still_lowers_it():
    grades = [smart_ride_grade(40, k) for k in (0, 1, 2, 4, 8)]
    assert grades == sorted(grades, reverse=True)
    assert grades[0] > grades[-1]


def test_the_fleet_rate_lands_near_the_old_median_grade():
    """A vehicle at the fleet stayed rate reads like the old median (~87 at
    the old ~6% no-go median, scaled): within the band, well above the floor."""
    assert 80 <= smart_ride_grade(1000, 21) <= 85
    assert smart_ride_grade(1000, 15) == 87          # ~1.5%, the scaled median
    assert smart_ride_grade(1000, 41) == 65          # ~4.1%, the scaled p90 floor


def test_cold_start_gates_on_rentals_since_the_counter_started(client, monkeypatch):
    """rentals_observed has counted since sql/089, rentals_stayed starts at 0:
    a vehicle with 300 observed rentals but only 3 since sql/098 has no grade
    yet, rather than a flattering one built on 0/300."""
    monkeypatch.setattr(api_public, "_rental_outcomes",
                        lambda: {"8c4a1f0d2e9b7a35": (300, 12, 0, 0, GRADE_MIN_RENTALS - 2)})
    (f,) = client.get("/api/v1/devices/current").json()["features"]
    assert f["properties"]["rentals_observed"] == 300
    assert f["properties"]["smart_ride_grade"] is None
    assert smart_ride_grade(GRADE_MIN_RENTALS, 0) is not None


def test_the_recent_rule_still_counts_failed_starts_only(cycle):
    """recent_no_go_mask is unchanged: a blip (stayed, no rotation) pushes
    nothing, a failed start pushes 1, a round trip pushes 0."""
    _, cur = cycle([_device(_north(2), device_id="bike-1")], state=_in_rental(max_m=3.0))
    (row,) = cur.rows_for("number_failed_starts = number_failed_starts + %s")
    assert row[7] is None                                      # blip: no push
    _, cur = cycle([_device(_north(4), device_id="bike-2")], state=_in_rental(max_m=6.0))
    (row,) = cur.rows_for("number_failed_starts = number_failed_starts + %s")
    assert row[7] == 1                                         # failed start
