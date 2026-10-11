"""docs/SERVICING_PLAN.md Phase 1 against real Postgres: the servicing log
(service_events), the settled reading, depot visits (ingest and backfill),
effective fleet, the census absence kinds.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
Shares tests/test_ghost_stops_pg.py's fixture.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

pytest.importorskip("psycopg")

from src import servicing  # noqa: E402
from src.quality import full_charge_range_meters  # noqa: E402
from tests.test_ghost_stops_pg import (  # noqa: E402,F401  (pg is a fixture)
    _SPOT, _T0, _dev, _observe, _vid, pg,
)

FULL = full_charge_range_meters()
DEPOT = (39.8135, -105.0185)
DEPOT_JITTER = (39.8137, -105.0183)      # ~30 m, still inside
STREET_B = (39.7400, -104.9900)


@pytest.fixture(autouse=True)
def _wired(pg, monkeypatch):
    from contextlib import contextmanager

    import os

    import psycopg

    @contextmanager
    def _conn():
        # A real connection per call, as the pool gives in production: the
        # depot backfill reads on one and commits on another.
        with psycopg.connect(os.environ["VEO_TEST_PG_DSN"]) as c:
            yield c

    monkeypatch.setattr(servicing, "connection", _conn)
    _wipe(pg)                      # the shared fixture does not know these tables
    yield
    pg.rollback()
    _wipe(pg)


def _wipe(conn):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM raw_telemetry_points WHERE vehicle_identifier LIKE '9e0%%'")
        for t in ("trip_events", "device_history", "device_state", "snapshot_metadata_core",
                  "device_state_processed_cycles", "service_events", "depot_visits"):
            cur.execute(f"DELETE FROM {t}")
    conn.commit()


class _Feed:
    def __init__(self, conn):
        self.conn, self.t = conn, _T0

    def __call__(self, rng=20000, *, at=_SPOT, reserved=None, gap=timedelta(minutes=2)):
        self.t += gap
        devs = [dataclasses.replace(_dev(1, at), current_range_meters=rng, is_reserved=reserved)]
        _observe(self.conn, self.t, devs)
        servicing.update_depot_visits(None, self.t, devs)
        return self.t


def _one(conn, sql, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()


def test_a_swap_is_logged_with_where_it_waited(pg):
    feed = _Feed(pg)
    feed(9000)
    low_at = feed(4000)
    full_at = feed(FULL)
    row = _one(pg, "SELECT observed_at, low_range_meters, low_at, full_range_meters, in_place, "
                   "after_absence, depot_id, equity_area, source FROM service_events "
                   "WHERE vehicle_identifier = %s", _vid(1))
    assert row[:7] == (full_at, 4000, low_at, FULL, True, False, None)
    assert row[7] is not None and row[8] == "ingest"
    assert _one(pg, "SELECT last_serviced_at FROM device_state WHERE vehicle_identifier = %s",
                _vid(1))[0] == full_at


def test_the_settled_reading_follows_a_ride(pg):
    feed = _Feed(pg)
    feed(30000)
    feed(20000, reserved=True)
    release = feed(12000, at=STREET_B)
    feed(18000, at=STREET_B)
    settled, until = _one(pg, "SELECT settled_range_meters, settling_until FROM device_state "
                              "WHERE vehicle_identifier = %s", _vid(1))
    assert settled == 18000
    assert until == release + timedelta(minutes=servicing.SETTLE_MINUTES)


def test_a_depot_stay_with_indoor_jitter_is_one_visit(pg):
    feed = _Feed(pg)
    feed(5000)
    feed(5000)
    entered = feed(6000, at=DEPOT, gap=timedelta(hours=1))
    feed(30000, at=DEPOT_JITTER)
    feed(FULL, at=DEPOT)
    exited = feed(FULL, at=STREET_B, gap=timedelta(hours=10))
    with pg.cursor() as cur:
        cur.execute("SELECT depot_id, entered_at, exited_at, pickup_lat, deploy_lat, "
                    "went_dark_first FROM depot_visits WHERE vehicle_identifier = %s", (_vid(1),))
        rows = cur.fetchall()
    assert len(rows) == 1
    dep, ent, ext, plat, dlat, dark = rows[0]
    assert (dep, ent, ext) == ("veo-denver-federal-72nd", entered, exited)
    assert plat == pytest.approx(_SPOT[0]) and dlat == pytest.approx(STREET_B[0])
    assert dark is False
    # the swap at the depot is logged as such
    assert _one(pg, "SELECT depot_id FROM service_events WHERE vehicle_identifier = %s",
                _vid(1))[0] == "veo-denver-federal-72nd"


def test_backfill_rebuilds_the_same_visit_and_is_idempotent(pg):
    feed = _Feed(pg)
    feed(5000)
    entered = feed(6000, at=DEPOT, gap=timedelta(hours=1))
    feed(FULL, at=DEPOT_JITTER)
    exited = feed(FULL, at=STREET_B, gap=timedelta(hours=10))
    assert servicing.backfill_depot_visits()["visits"] == 0      # ingest already wrote it
    with pg.cursor() as cur:
        cur.execute("DELETE FROM depot_visits")
    pg.commit()
    assert servicing.backfill_depot_visits()["visits"] == 1
    assert servicing.backfill_depot_visits()["visits"] == 0
    row = _one(pg, "SELECT entered_at, exited_at, source FROM depot_visits")
    assert row == (entered, exited, "backfill")


def test_effective_fleet_counts_vehicles_inside(pg):
    feed = _Feed(pg)
    feed(5000)
    feed(5000, at=DEPOT, gap=timedelta(hours=1))
    with pg.cursor() as cur:
        out = servicing.effective_fleet(cur, feed.t)
    assert out["inside_depot"] == 1 and out["inside_depot_under_7d"] == 1
    assert out["on_street_24h"] == 0


@pytest.mark.parametrize("pos, rng, kind", [
    (DEPOT, 20000, "at_depot"),
    (_SPOT, 2000, "hidden_low_battery"),
    (_SPOT, 20000, "missing"),
])
def test_census_absence_kind(pg, pos, rng, kind):
    feed = _Feed(pg)
    feed(rng, at=pos)
    got = _one(pg, f"SELECT {servicing.absence_kind_sql()} FROM device_state ds "
                   "WHERE ds.vehicle_identifier = %s", _vid(1))[0]
    assert got == kind


# ---------------------------------------------------------------------------
# Phase 2: the admin watch's servicing events, the /fleet/service summary
# ---------------------------------------------------------------------------

def test_admin_watch_names_this_cycles_servicing(pg):
    from src import admin_watch

    feed = _Feed(pg)
    feed(4000)
    t = feed(FULL)
    from src.quality import compute_battery_percent

    with pg.cursor() as cur:
        assert admin_watch.servicing_changes(cur, [_vid(1)], t) == {
            _vid(1): [f"got a fresh battery in place ({compute_battery_percent(4000)}% to 100%)"]}
    entered = feed(FULL, at=DEPOT, gap=timedelta(hours=1))
    with pg.cursor() as cur:
        assert admin_watch.servicing_changes(cur, [_vid(1)], entered) == {
            _vid(1): ["was taken into the depot"]}
    out = feed(FULL, at=STREET_B, gap=timedelta(hours=30))
    with pg.cursor() as cur:
        assert admin_watch.servicing_changes(cur, [_vid(1)], out) == {
            _vid(1): ["is back from the depot after 30 h"]}


def test_fleet_service_summary(pg, monkeypatch):
    from src import fleet_service

    monkeypatch.setattr(fleet_service, "connection", servicing.connection)
    feed = _Feed(pg)
    feed(2000)                                    # waits empty in Denver
    feed(2000, gap=timedelta(hours=3))
    feed(FULL)
    feed(FULL, at=DEPOT, gap=timedelta(hours=1))
    feed(FULL, at=STREET_B, gap=timedelta(hours=80))
    out = fleet_service.summarize("28d", feed.t)
    assert out["swaps"]["count"] == 1 and out["swaps"]["share_in_place"] == 1.0
    wait = out["swap_wait"]
    groups = [g for g in ("equity_areas", "rest_of_denver") if wait[g]]
    assert len(groups) == 1 and wait[groups[0]]["median_hours"] == pytest.approx(3.0, abs=0.1)
    assert out["depot"]["visits"] == 1 and out["depot"]["stay"]["3_to_7d"] == 1
    assert out["fleet"]["inside_depot"] == 0
    assert out["caveats"]


def test_backfill_replaces_a_visit_ingest_opened_late(pg):
    """A vehicle already inside the depot when sql/107 deployed: ingest opens a
    visit at the first cycle it sees it, the backfill knows when it really
    arrived and replaces it, and ingest then closes the backfilled one."""
    feed = _Feed(pg)
    feed(5000)
    arrived = feed(6000, at=DEPOT, gap=timedelta(hours=1))
    with pg.cursor() as cur:                     # pretend ingest saw it only later
        cur.execute("DELETE FROM depot_visits")
    pg.commit()
    late = feed(FULL, at=DEPOT_JITTER, gap=timedelta(hours=5))
    assert _one(pg, "SELECT entered_at, source FROM depot_visits") == (late, "ingest")
    with pg.cursor() as cur:                     # the "already inside" visit was not opened
        cur.execute("DELETE FROM depot_visits")
        cur.execute("INSERT INTO depot_visits (vehicle_identifier, depot_id, entered_at) "
                    "VALUES (%s, 'veo-denver-federal-72nd', %s)", (_vid(1), late))
    pg.commit()
    stats = servicing.backfill_depot_visits()
    assert stats["replaced"] == 1 and stats["visits"] == 1
    assert _one(pg, "SELECT entered_at, exited_at, source FROM depot_visits") == (arrived, None, "backfill")
    out = feed(FULL, at=STREET_B, gap=timedelta(hours=2))
    assert _one(pg, "SELECT entered_at, exited_at FROM depot_visits") == (arrived, out)


def test_the_raw_buffer_replay_stops_where_ingest_started(pg):
    """The tail of backfill_service_events: replay raw_telemetry_points after
    the archive, up to the first ingest-written event, without duplicating it."""
    feed = _Feed(pg)
    feed(3000)
    t_full = feed(FULL)
    # raw_telemetry_points is not written by _observe; seed the two readings
    from src.servicing import _replay_raw_buffer
    with pg.cursor() as cur:
        cur.execute("SELECT cycle_id, snapshot_time FROM snapshot_metadata_core ORDER BY snapshot_time")
        cycles = cur.fetchall()
        for (cid, t), rng in zip(cycles, (3000, FULL)):
            cur.execute("INSERT INTO raw_telemetry_points (cycle_id, snapshot_time, device_id, "
                        "form_factor, spatial_status, vehicle_identifier, latitude, longitude, "
                        "current_range_meters, is_reserved) "
                        "VALUES (%s, %s, 'bike-1', 'scooter', 'denver_core', %s, %s, %s, %s, FALSE)",
                        (cid, t, _vid(1), _SPOT[0], _SPOT[1], rng))
    pg.commit()
    assert _one(pg, "SELECT COUNT(*) FROM service_events")[0] == 1          # ingest's
    out = _replay_raw_buffer({}, {}, {}, None, timedelta(hours=1))
    assert out["rows"] == 1 and out["inserted"] == 0                       # stops before ingest's event
    with pg.cursor() as cur:
        cur.execute("DELETE FROM service_events")
    pg.commit()
    out = _replay_raw_buffer({}, {}, {}, None, timedelta(hours=1),
                             until=t_full + timedelta(hours=1))   # the fixture lives in 2031
    assert out["rows"] == 2 and out["inserted"] == 1
    assert _one(pg, "SELECT observed_at, source FROM service_events") == (t_full, "backfill")


def test_backfill_keeps_an_ingest_visit_that_began_a_cycle_earlier(pg):
    """Ingest saw the vehicle inside one cycle before the history's stop
    opened there: the ingest visit stands, no duplicate is added."""
    feed = _Feed(pg)
    feed(5000)
    feed(6000, at=DEPOT, gap=timedelta(hours=1))
    with pg.cursor() as cur:
        cur.execute("UPDATE depot_visits SET entered_at = entered_at - INTERVAL '2 minutes'")
    pg.commit()
    before = _one(pg, "SELECT entered_at FROM depot_visits")[0]
    assert servicing.backfill_depot_visits()["visits"] == 0
    assert _one(pg, "SELECT COUNT(*), MIN(entered_at), MIN(source) FROM depot_visits") == (1, before, "ingest")
