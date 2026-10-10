"""sql/104 against real Postgres: ingest stamps device_state.last_serviced_at
when a vehicle's charge rises by at least fleet_reports.charge_rise_meters()
between two readings (a swap or a charge), including across an absence, and
keeps max_observed_range_* behaving as before.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
Shares tests/test_ghost_stops_pg.py's fixture.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

pytest.importorskip("psycopg")

from src.fleet_reports import charge_rise_meters  # noqa: E402
from tests.test_ghost_stops_pg import (  # noqa: E402,F401  (pg is a fixture)
    _T0, _dev, _observe, _vid, pg,
)

RISE = charge_rise_meters()


@pytest.fixture(autouse=True)
def _leave_no_future_cycles(pg):
    """The shared fixture wipes at SETUP only; these cycles are dated 2031,
    and a later file that reads "the newest complete cycle" (favourites)
    would pick them up. Wipe them on the way out too."""
    yield
    pg.rollback()
    with pg.cursor() as cur:
        for t in ("trip_events", "device_history", "device_state",
                  "snapshot_metadata_core", "device_state_processed_cycles"):
            cur.execute(f"DELETE FROM {t}")
    pg.commit()


def _at_range(rng):
    return dataclasses.replace(_dev(1), current_range_meters=rng)


def _row(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_range_meters, last_serviced_at, max_observed_range_meters, "
            "max_observed_range_at FROM device_state WHERE vehicle_identifier = %s", (_vid(1),))
        return cur.fetchone()


def test_a_rise_of_the_threshold_is_servicing_and_a_smaller_one_is_not(pg):
    t = _T0
    _observe(pg, t, [_at_range(20000)])
    assert _row(pg)[:2] == (20000, None)                   # first reading seeds, no service
    t += timedelta(minutes=2)
    _observe(pg, t, [_at_range(20000 + RISE - 1)])
    assert _row(pg)[:2] == (20000 + RISE - 1, None)        # under the threshold
    t += timedelta(minutes=2)
    _observe(pg, t, [_at_range(20000 + RISE - 1 + RISE)])
    assert _row(pg)[1] == t                                # a swap
    serviced = t
    t += timedelta(minutes=2)
    _observe(pg, t, [_at_range(9000)])                     # ridden down
    last_range, last_serviced, max_rng, _max_at = _row(pg)
    assert (last_range, last_serviced) == (9000, serviced)  # servicing is kept
    assert max_rng == 20000 + 2 * RISE - 1


def test_coming_back_on_the_map_with_more_charge_counts(pg):
    t = _T0
    _observe(pg, t, [_at_range(5000)])
    t += timedelta(hours=30)                               # gone a day, back charged
    _observe(pg, t, [_at_range(40000)])
    assert _row(pg)[1] == t


def test_max_observed_range_still_records_only_a_new_peak(pg):
    t0 = _T0
    _observe(pg, t0, [_at_range(30000)])
    _observe(pg, t0 + timedelta(minutes=2), [_at_range(10000)])
    _observe(pg, t0 + timedelta(minutes=4), [_at_range(25000)])
    _, _, max_rng, max_at = _row(pg)
    assert (max_rng, max_at) == (30000, t0)
    _observe(pg, t0 + timedelta(minutes=6), [_at_range(31000)])
    assert _row(pg)[2:] == (31000, t0 + timedelta(minutes=6))
