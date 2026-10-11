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

    @contextmanager
    def _conn():
        yield pg

    monkeypatch.setattr(servicing, "connection", _conn)
    _wipe(pg)                      # the shared fixture does not know these tables
    yield
    pg.rollback()
    _wipe(pg)


def _wipe(conn):
    with conn.cursor() as cur:
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
