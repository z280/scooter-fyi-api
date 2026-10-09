"""sql/090 end to end against real Postgres: a rental that goes nowhere, a
round trip and a real ride, through update_for_cycle, read back out of
rental_outcomes_hourly and checked against device_state's own counters.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from src.config import load  # noqa: E402
from tests.test_device_state_failed_starts_pg import (  # noqa: E402
    _Clock, _ds, _north, _park, _rent,
)
from tests.test_ghost_stops_pg import pg  # noqa: E402,F401  (fixture)

R = float(load().device_tracking.stationary_threshold_meters)


def _rollup(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sum(rentals), sum(no_gos), sum(no_gos_max), sum(max_known), "
            "array_agg(DISTINCT radius_m::float8), array_agg(DISTINCT h3_9) "
            "FROM rental_outcomes_hourly")
        return cur.fetchone()


def test_three_rentals_land_in_the_rollup_and_agree_with_the_counters(pg):
    with pg.cursor() as cur:
        cur.execute("DELETE FROM rental_outcomes_hourly")
    pg.commit()
    clock = _Clock()
    _park(pg, clock)
    # 1. Went nowhere: never past 8 m, ends 5 m out.
    _rent(pg, clock, [_north(3), _north(8)], release_at=_north(5), release_id="bike-2")
    _park(pg, clock, device_id="bike-2")
    # 2. Round trip: out 400 m, back to within 6 m of the unlock point.
    _rent(pg, clock, [_north(150), _north(400), _north(120)], release_at=_north(6),
          release_id="bike-3", device_id="bike-2")
    _park(pg, clock, pos=_north(6), device_id="bike-3")
    # 3. A real ride.
    _rent(pg, clock, [_north(300), _north(900)], release_at=_north(1600),
          release_id="bike-4", device_id="bike-3")

    rentals, no_gos, no_gos_max, max_known, radii, cells = _rollup(pg)
    # Every rental was unlocked at the parking spot or right beside it (within
    # 6 m): all three are attributed to that spot's r9 cell, not to where the
    # real ride ended 1.6 km away.
    from src.ingest import _h3_cells
    from tests.test_ghost_stops_pg import _SPOT
    assert cells == [_h3_cells(*_SPOT)[1]]
    assert (rentals, no_gos, max_known) == (3, 2, 3)
    assert no_gos_max == 1, "the round trip left the kerb: not a maximum no-go"
    assert radii == [R]
    s = _ds(pg)
    # Same transaction, same definition: the rollup and the per-vehicle
    # counters can never disagree.
    assert (s["rentals_observed"], s["rentals_no_go"]) == (rentals, no_gos)


def test_stayed_lands_in_both_counters_and_obeys_the_checks(pg):
    """sql/099 end to end: in place, round trip, 25-50 m in place, real ride."""
    import psycopg

    with pg.cursor() as cur:
        cur.execute("DELETE FROM rental_outcomes_hourly")
    pg.commit()
    clock = _Clock()
    _park(pg, clock)
    # 1. In place (failed start): no-go AND stayed.
    _rent(pg, clock, [_north(3), _north(8)], release_at=_north(5), release_id="bike-2")
    _park(pg, clock, device_id="bike-2")
    # 2. Round trip out 400 m, back to 6 m: no-go, NOT stayed.
    _rent(pg, clock, [_north(150), _north(400)], release_at=_north(6),
          release_id="bike-3", device_id="bike-2")
    _park(pg, clock, pos=_north(6), device_id="bike-3")
    # 3. Never past 45 m, dropped 40 m from the unlock point: stayed, NOT a no-go.
    _rent(pg, clock, [_north(30), _north(45)], release_at=_north(46),
          release_id="bike-4", device_id="bike-3")
    _park(pg, clock, pos=_north(46), device_id="bike-4")
    # 4. A real ride: neither.
    _rent(pg, clock, [_north(300), _north(900)], release_at=_north(1600),
          release_id="bike-5", device_id="bike-4")

    with pg.cursor() as cur:
        cur.execute("SELECT sum(rentals), sum(no_gos), sum(stayed), sum(stayed_known) "
                    "FROM rental_outcomes_hourly")
        rentals, no_gos, stayed, stayed_known = cur.fetchone()
        cur.execute("SELECT rentals_observed, rentals_no_go, rentals_stayed, "
                    "rentals_observed_stayed_era FROM device_state")
        (observed, no_go, v_stayed, era), = cur.fetchall()
    assert (rentals, no_gos, stayed, stayed_known) == (4, 2, 2, 4)
    assert (observed, no_go, v_stayed, era) == (4, 2, 2, 4)

    # The CHECKs: stayed can never exceed what it is counted against.
    with pg.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("UPDATE device_state SET rentals_stayed = rentals_observed_stayed_era + 1")
    pg.rollback()
    with pg.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("UPDATE rental_outcomes_hourly SET stayed = rentals + 1, "
                        "stayed_known = rentals + 1")
    pg.rollback()
    with pg.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("UPDATE rental_outcomes_hourly SET stayed = stayed_known + 1")
    pg.rollback()


def test_rows_before_sql_099_default_to_not_recorded(pg):
    with pg.cursor() as cur:
        cur.execute("DELETE FROM rental_outcomes_hourly")
        cur.execute(
            "INSERT INTO rental_outcomes_hourly (hour, h3_9, model, radius_m, equity_area, "
            "rentals, no_gos) VALUES ('2026-10-08T10:00:00+00', 1, 'Cosmo', 25, 'outside', 9, 1) "
            "RETURNING stayed, stayed_known")
        assert cur.fetchone() == (0, 0)
    pg.rollback()
