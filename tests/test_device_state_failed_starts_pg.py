"""sql/087 against real Postgres: failed starts, jitter, decay, rule 4.

Drives src/device_state.update_for_cycle cycle by cycle, as
tests/test_ghost_stops_pg.py does, and checks what device_state,
device_history and trip_events end up holding:

  * a rental released where it was unlocked, with a rotated bike_id, is a
    failed start: the counter goes up, the dwell clock and the stop survive
    (the stop the rental start closed is reopened), no trip_events row;
  * a reservation blip with no rotation counts for nothing and also keeps
    the stop;
  * GPS jitter under 50 m never resets the counter or dwell, nor writes a trip;
  * a real ride writes one trip; one past FAILED_START_DECAY_M clears the
    counter, a shorter one carries it;
  * fail / long ride / fail reads high_risk through recent_no_go_mask.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
Shares tests/test_ghost_stops_pg.py's fixture, which wipes device_history /
device_state / trip_events / snapshot_metadata_core / the ledger at setup.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("psycopg")

from src import device_state  # noqa: E402
from src.quality import compute_reliability_tier, recent_rentals_no_go  # noqa: E402
from tests.test_ghost_stops_pg import (  # noqa: E402,F401  (pg is a fixture)
    _SPOT, _T0, _dev, _observe, _stops, _vid, pg,
)

_STEP = timedelta(minutes=2)
_M_PER_DEG_LAT = 111_320.0


def _north(meters: float, of=_SPOT) -> tuple[float, float]:
    return (of[0] + meters / _M_PER_DEG_LAT, of[1])


class _Clock:
    def __init__(self):
        self.t = _T0

    def __call__(self):
        self.t += _STEP
        return self.t


def _ds(conn, n=1) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT number_failed_starts, first_observed_at_location, current_lat, "
            "current_device_id, rental_started_at, rental_max_distance_m, "
            "rental_origin_device_id, recent_no_go_mask, rentals_observed, rentals_no_go "
            "FROM device_state WHERE vehicle_identifier = %s", (_vid(n),))
        r = cur.fetchone()
    keys = ("fs", "first_obs", "lat", "device_id", "rental_started_at", "rental_max",
            "origin_device_id", "mask", "rentals_observed", "rentals_no_go")
    return dict(zip(keys, r))


def _trips(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM trip_events")
        return cur.fetchone()[0]


def _rent(conn, clock, path, *, release_at, release_id, device_id="bike-1"):
    """Reserved samples along `path`, then released at `release_at`."""
    for pos in path:
        _observe(conn, clock(), [_dev(1, pos, is_reserved=True, device_id=device_id)])
    return _observe(conn, clock(), [_dev(1, release_at, device_id=release_id)])


def _park(conn, clock, pos=_SPOT, device_id="bike-1", cycles=2):
    for _ in range(cycles):
        _observe(conn, clock(), [_dev(1, pos, device_id=device_id)])


def test_in_place_failed_rental_counts_and_keeps_dwell_and_stop(pg):
    clock = _Clock()
    _park(pg, clock)
    arrived = _ds(pg)["first_obs"]
    stats = _rent(pg, clock, [_north(3), _north(8)], release_at=_north(5),
                  release_id="bike-2")
    assert (stats.failed_starts, stats.rentals_failed_start, stats.moved) == (1, 1, 0)

    s = _ds(pg)
    assert s["fs"] == 1
    assert s["first_obs"] == arrived                       # dwell clock kept
    assert float(s["lat"]) == pytest.approx(_SPOT[0])      # position kept
    assert s["device_id"] == "bike-2"
    assert (s["rental_started_at"], s["rental_max"], s["origin_device_id"]) == (None, None, None)
    assert s["mask"] == 0b1
    assert (s["rentals_observed"], s["rentals_no_go"]) == (1, 1)   # sql/072 unchanged
    assert _trips(pg) == 0

    stops = _stops(pg, 1)
    assert len(stops) == 1, stops                          # reopened, not a new one
    arrived_at, departed_at, reason, _lat, _lon, dwell_fs = stops[0]
    assert (departed_at, reason, dwell_fs) == (None, None, 1)

    # The next cycle compares against the new bike_id: no second count.
    _park(pg, clock, device_id="bike-2", cycles=1)
    assert _ds(pg)["fs"] == 1


def test_reservation_blip_without_rotation_counts_for_nothing(pg):
    clock = _Clock()
    _park(pg, clock)
    arrived = _ds(pg)["first_obs"]
    stats = _rent(pg, clock, [_north(2)], release_at=_north(1), release_id="bike-1")
    assert (stats.failed_starts, stats.rentals_blip) == (0, 1)
    s = _ds(pg)
    assert (s["fs"], s["mask"], s["first_obs"]) == (0, 0, arrived)
    assert len(_stops(pg, 1)) == 1 and _stops(pg, 1)[0][1] is None
    assert _trips(pg) == 0


def test_jitter_does_not_reset_counter_or_dwell_or_write_trips(pg):
    clock = _Clock()
    _park(pg, clock)
    _rent(pg, clock, [_north(4)], release_at=_north(4), release_id="bike-2")
    arrived = _ds(pg)["first_obs"]
    # A wandering fix: 20, 35, 45 m from the stored position, then back.
    for m in (20, 35, 45, 25, 2):
        _observe(pg, clock(), [_dev(1, _north(m), device_id="bike-2")])
    s = _ds(pg)
    assert (s["fs"], s["first_obs"]) == (1, arrived)
    assert _trips(pg) == 0
    assert len(_stops(pg, 1)) == 1


def test_failure_after_drift_is_still_a_failure_not_a_ride(pg):
    """Drift of 70 m without rotation is held (the stop stays put); a failed
    rental at the drifted fix is judged from that fix."""
    clock = _Clock()
    _park(pg, clock)
    arrived = _ds(pg)["first_obs"]
    for _ in range(3):
        _observe(pg, clock(), [_dev(1, _north(70))])
    assert _trips(pg) == 0 and _ds(pg)["first_obs"] == arrived
    stats = _rent(pg, clock, [_north(72)], release_at=_north(71), release_id="bike-2")
    assert (stats.failed_starts, stats.moved) == (1, 0)
    s = _ds(pg)
    assert (s["fs"], s["first_obs"], s["rentals_no_go"]) == (1, arrived, 1)
    assert _trips(pg) == 0
    assert len(_stops(pg, 1)) == 1


def test_unreserved_rotation_inside_50m_is_a_failed_start(pg):
    clock = _Clock()
    _park(pg, clock)
    stats = _observe(pg, clock(), [_dev(1, _north(30), device_id="bike-2")])
    assert (stats.failed_starts, stats.moved) == (1, 0)
    assert _ds(pg)["fs"] == 1 and _trips(pg) == 0
    assert _stops(pg, 1)[0][5] == 1                        # dwell_failed_starts


def test_real_ride_writes_one_trip_and_long_one_clears_the_count(pg):
    clock = _Clock()
    _park(pg, clock)
    _rent(pg, clock, [_north(4)], release_at=_north(4), release_id="bike-2")
    assert _ds(pg)["fs"] == 1
    # 200 m ride: a trip and a new stop, but the count is carried.
    _rent(pg, clock, [_north(100), _north(220)], release_at=_north(200),
          release_id="bike-3", device_id="bike-2")
    s = _ds(pg)
    assert (s["fs"], s["mask"]) == (1, 0b10)
    assert _trips(pg) == 1
    assert _stops(pg, 1)[-1][5] == 1                       # carried to the new stop
    # 2 km ride from there: clears it.
    _rent(pg, clock, [_north(1000, _north(200)), _north(2100, _north(200))],
          release_at=_north(2000, _north(200)), release_id="bike-4", device_id="bike-3")
    s = _ds(pg)
    assert (s["fs"], s["mask"]) == (0, 0b100)
    assert _trips(pg) == 2
    assert _stops(pg, 1)[-1][5] == 0


def test_fail_long_ride_fail_is_high_risk_through_the_mask(pg):
    clock = _Clock()
    _park(pg, clock)
    _rent(pg, clock, [_north(3)], release_at=_north(3), release_id="bike-2")
    far = _north(1500)
    _rent(pg, clock, [_north(700), _north(1500)], release_at=far,
          release_id="bike-3", device_id="bike-2")
    assert _ds(pg)["fs"] == 0                              # cleared by the ride
    _park(pg, clock, far, device_id="bike-3", cycles=1)
    _rent(pg, clock, [_north(5, far)], release_at=_north(2, far),
          release_id="bike-4", device_id="bike-3")
    s = _ds(pg)
    assert (s["fs"], s["mask"]) == (1, 0b101)
    recent = recent_rentals_no_go(s["mask"])
    assert recent == 2
    tier = compute_reliability_tier(
        number_failed_starts=s["fs"], first_observed_at_location=s["first_obs"],
        quality_designation="good", has_negative_report=False,
        now=clock.t, recent_rentals_no_go=recent)
    assert tier == "high_risk"
    # Without rule 4 the same vehicle would read merely "unknown".
    assert compute_reliability_tier(
        number_failed_starts=s["fs"], first_observed_at_location=s["first_obs"],
        quality_designation="good", has_negative_report=False, now=clock.t) == "unknown"


def test_round_trip_back_to_the_spot_is_a_ride_not_a_failure(pg):
    clock = _Clock()
    _park(pg, clock)
    arrived = _ds(pg)["first_obs"]
    stats = _rent(pg, clock, [_north(300), _north(600), _north(200)],
                  release_at=_north(3), release_id="bike-2")
    assert stats.failed_starts == 0
    s = _ds(pg)
    assert s["fs"] == 0 and s["mask"] == 0
    assert s["first_obs"] != arrived                       # it left: dwell restarts
    assert len(_stops(pg, 1)) == 2


def test_migration_087_replays_cleanly(pg):
    from tests.test_ghost_stops_pg import SQL_DIR
    sql = (SQL_DIR / "087_device_state_failed_start_rentals.sql").read_text()
    with pg.cursor() as cur:
        cur.execute(sql)
        cur.execute(sql)
    pg.commit()
    assert device_state.RECENT_RENTALS_KEPT == 3
