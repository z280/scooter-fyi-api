"""sql/104 + sql/105 against real Postgres: ingest stamps
device_state.last_serviced_at when a vehicle reads FULL (95%) while parked
after reading <= 50% parked since its last full reading — a swap or a charge
— and NOT on a ride's sag-and-rebound, which is what sql/104's "any 5% rise"
rule fired on (46 vehicles in its first live cycle, one a real swap).

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
Shares tests/test_ghost_stops_pg.py's fixture.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

pytest.importorskip("psycopg")

from src.fleet_reports import full_battery_meters, service_from_meters  # noqa: E402
from src.quality import full_charge_range_meters  # noqa: E402
from tests.test_ghost_stops_pg import (  # noqa: E402,F401  (pg is a fixture)
    _T0, _dev, _observe, _vid, pg,
)

FULL = full_charge_range_meters()
FULL95 = full_battery_meters()
LOW = service_from_meters()


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


class _Feed:
    def __init__(self, conn):
        self.conn, self.t = conn, _T0

    def __call__(self, rng, *, reserved=None, gap=timedelta(minutes=2)):
        self.t += gap
        _observe(self.conn, self.t, [dataclasses.replace(
            _dev(1), current_range_meters=rng, is_reserved=reserved)])
        return self.t


def _serviced(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT last_serviced_at FROM device_state WHERE vehicle_identifier = %s",
                    (_vid(1),))
        return cur.fetchone()[0]


def test_a_swap_from_low_to_full_is_servicing(pg):
    feed = _Feed(pg)
    feed(8753)
    assert _serviced(pg) is None
    t = feed(FULL)                                        # 8.7 km -> 45.3 km, parked
    assert _serviced(pg) == t
    feed(9000, reserved=True)
    feed(12000)                                           # ridden down afterwards
    assert _serviced(pg) == t                             # the stamp is kept


def test_a_rides_sag_and_rebound_is_not_servicing(pg):
    """Real feed shape (2026-10-10): 21947 parked, sag to 7514 under load,
    then 11231 -> 17876 parked as it recovers."""
    feed = _Feed(pg)
    feed(21947)
    for r in (10153, 8479, 11231, 7514):
        feed(r, reserved=True)
    feed(11231)
    feed(17876)
    assert _serviced(pg) is None


def test_a_full_vehicle_ridden_and_rebounding_to_full_is_not_servicing(pg):
    feed = _Feed(pg)
    feed(FULL)
    for r in (43105, 41287, 38000):
        feed(r, reserved=True)
    feed(39000)                                           # parked sag, above 50%
    feed(FULL)
    assert _serviced(pg) is None


def test_a_charge_that_stops_short_of_full_is_not_servicing(pg):
    feed = _Feed(pg)
    feed(LOW - 1000)
    feed(FULL95 - 1)
    assert _serviced(pg) is None
    t = feed(FULL95)
    assert _serviced(pg) == t


def test_a_low_reading_only_while_reserved_does_not_count(pg):
    feed = _Feed(pg)
    feed(FULL - 3000)
    feed(5000, reserved=True)                             # sag under load
    feed(FULL)
    assert _serviced(pg) is None


def test_coming_back_on_the_map_full_counts(pg):
    feed = _Feed(pg)
    feed(5000)
    t = feed(FULL, gap=timedelta(hours=30))               # gone a day, back full
    assert _serviced(pg) == t


def test_max_observed_range_still_records_only_a_new_peak(pg):
    feed = _Feed(pg)
    t0 = feed(30000)
    feed(10000)
    feed(25000)
    with pg.cursor() as cur:
        cur.execute("SELECT max_observed_range_meters, max_observed_range_at FROM device_state "
                    "WHERE vehicle_identifier = %s", (_vid(1),))
        assert cur.fetchone() == (30000, t0)


def test_after_the_migration_a_mid_ride_low_cannot_seed_servicing(pg):
    """zneill-agent (#151): a vehicle whose last reading before sql/105 was a
    ride's sag (in rental, 5 km) must not have that low carried over — a
    later parked 80% -> full is not servicing."""
    from pathlib import Path

    with pg.cursor() as cur:
        cur.execute(
            "INSERT INTO device_state (vehicle_identifier, current_device_id, current_lat, "
            "current_lon, first_observed_at_location, first_ever_observed_at, last_observed_at, "
            "rental_started_at, last_range_meters) VALUES (%s, 'bike-1', %s, %s, %s, %s, %s, %s, 5000)",
            (_vid(1), _dev(1).lat, _dev(1).lon, _T0, _T0, _T0, _T0))
        cur.execute((Path(__file__).resolve().parents[1]
                     / "sql" / "105_servicing_needs_full_from_low.sql").read_text())
        cur.execute("SELECT range_low_since_full FROM device_state WHERE vehicle_identifier = %s",
                    (_vid(1),))
        assert cur.fetchone()[0] is None
    pg.commit()
    feed = _Feed(pg)
    feed(int(FULL * 0.8))                                 # released, parked at 80%
    feed(FULL)
    assert _serviced(pg) is None
