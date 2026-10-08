"""Fleet analytics rollups (sql/094) end to end on real Postgres: source rows
in, refresh(), endpoints out. SKIPS unless VEO_TEST_PG_DSN is set."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("psycopg")

from src import analytics_rollups as ar, api_analytics  # noqa: E402
from tests.test_ghost_stops_pg import pg  # noqa: E402,F401  (fixture)

NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
T = NOW - timedelta(hours=10)   # past the 6 h departure lag


@pytest.fixture
def db(pg, monkeypatch):
    @contextmanager
    def _conn():
        yield pg

    for mod in (ar, api_analytics):
        monkeypatch.setattr(mod, "connection", _conn)
    monkeypatch.setattr(ar, "regions_for",
                        lambda lat, lon: (ar.CITY, ("neighborhood", "NB_Test")) if lat else (ar.CITY,))
    monkeypatch.setattr(api_analytics.geo, "region_names", lambda layer: ["NB_Test"])
    api_analytics._cache.clear()
    with pg.cursor() as cur:
        for t in ("analytics_rides_hourly", "analytics_failed_starts_hourly", "analytics_dwell_daily",
                  "analytics_region_devices_hourly", "analytics_rollup_state", "analytics_stop_closes",
                  "analytics_open_at_cutover",
                  "regional_metrics_narrow", "device_status_snapshots"):
            cur.execute(f"DELETE FROM {t}")
        # Start the watermarks just before the fixture's rows, as a backfill
        # from the real epoch would reach them.
        # Legacy sweep already done (cutover in the past, swept up to it);
        # region snapshots start just before the fixture's rows.
        cut = T - timedelta(days=3)
        cur.execute("SELECT COALESCE(MAX(id), 0) FROM device_history")
        cut_id = cur.fetchone()[0]
        cur.execute("INSERT INTO analytics_rollup_state (name, watermark_id, watermark_time) VALUES "
                    "('stops_cutover', %s, %s), ('stops', NULL, %s), ('region_devices', NULL, %s)",
                    (cut_id, cut, cut, T - timedelta(hours=1)))
    pg.commit()
    return pg


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(api_analytics.router)
    return TestClient(app)


def test_rides_stops_and_region_devices_flow_into_the_endpoints(db):
    cid = uuid.uuid4()
    with db.cursor() as cur:
        cur.execute("INSERT INTO observation_cycles (cycle_id, job_status) VALUES (%s, 'complete')", (str(cid),))
        for i, model in enumerate(["Cosmo", "Cosmo", "Astro"]):
            cur.execute(
                "INSERT INTO trip_events (vehicle_identifier, vehicle_plate, cycle_id, detected_at, "
                "form_factor, vehicle_use_type, vehicle_model_name, from_lat, from_lon, to_lat, to_lon, "
                "distance_meters) VALUES (%s, %s, NULL, %s, 'scooter', 'standing', %s, 39.74, -104.98, "
                "39.75, -104.99, 500)", (f"v{i}", f"10{i}0000", T + timedelta(minutes=5 * i), model))
        for i, (n_failed, minutes) in enumerate([(2, 30), (0, 90)]):
            cur.execute(
                "INSERT INTO device_history (vehicle_identifier, snapshot_time, departed_at, lat, lon, "
                "spatial_status, form_factor, device_id_observed, dwell_failed_starts, vehicle_model_name) "
                "VALUES (%s, %s, %s, 39.74, -104.98, 'denver_core', 'scooter', %s, %s, 'Cosmo')",
                (f"s{i}", T - timedelta(minutes=minutes), T, f"bike-{i}", n_failed))
        cur.execute("INSERT INTO snapshot_metadata_core (cycle_id, snapshot_time, percent_all_devices_equity) "
                    "VALUES (%s, %s, 31.5)", (str(cid), T + timedelta(minutes=2)))
        for name, n in (("NB_Test", 40), ("NB_Other", 10)):
            cur.execute("INSERT INTO regional_metrics_narrow (cycle_id, snapshot_time, region_category, "
                        "region_type, region_name, count_total, count_bikes, count_scooters) "
                        "VALUES (%s, %s, 'admin', 'neighborhood', %s, %s, 0, %s)",
                        (str(cid), T + timedelta(minutes=2), name, n, n))
        cur.execute("INSERT INTO device_status_snapshots (cycle_id, snapshot_time, total, available, "
                    "reserved, out_of_service, models) VALUES (%s, %s, 100, 80, 15, 5, %s)",
                    (str(cid), T + timedelta(minutes=2),
                     '{"Cosmo": {"available": 50, "reserved": 10, "out_of_service": 3}}'))
        # The closes the trigger queued have settled.
        cur.execute("UPDATE analytics_stop_closes SET closed_at = %s", (T,))
    db.commit()

    got = ar.backfill(max_passes=20)
    assert got["rides"] == 3

    c = _client()
    rides = c.get("/api/v1/analytics/rides?days=2&granularity=hour").json()
    assert rides["rides"] == 3 and rides["models"] == ["Astro", "Cosmo"]
    assert rides["series"][0]["by_model"] == {"Astro": 1, "Cosmo": 2}
    assert rides["series"][0]["bucket"].endswith(("-06:00", "-07:00"))   # Denver local
    assert rides["data_through"] is not None
    by_region = c.get("/api/v1/analytics/rides?days=2&region_type=neighborhood&region_name=NB_Test").json()
    assert by_region["rides"] == 3

    fs = c.get("/api/v1/analytics/failed-starts?days=2").json()
    assert fs["failed_starts"] == 2 and fs["stops_with_failures"] == 1
    assert "under-reported" in fs["caveat"]
    assert fs["comparable_since"] == "2026-10-06T01:36:00+00:00"
    assert rides["counting_changes"][0]["commit"] == "8a51d4d"

    dwell = c.get("/api/v1/analytics/dwell?region_type=city&days=2").json()
    cosmo = dwell["regions"][0]["by_model"]["Cosmo"]
    assert cosmo["dwells"] == 2 and cosmo["average_minutes"] is None   # under the 30-stop floor

    dev = c.get("/api/v1/analytics/devices-by-region?region_type=neighborhood&days=2").json()
    assert [r["region"] for r in dev["regions"]] == ["NB_Test", "NB_Other"]
    assert dev["regions"][0]["average"] == 40.0 and dev["regions"][0]["now"] == 40

    eq = c.get("/api/v1/analytics/equity-compliance?days=2").json()
    assert eq["series"][0]["percent"] == 31.5 and eq["buckets_meeting_threshold"] == 1

    st = c.get("/api/v1/analytics/fleet-status?days=2").json()
    assert st["series"][0]["available"] == 80.0 and st["series"][0]["in_use"] == 15.0
    st_m = c.get("/api/v1/analytics/fleet-status?days=2&model=Cosmo").json()
    assert st_m["series"][0]["available"] == 50.0

    counts = c.get("/api/v1/analytics/fleet-counts").json()
    assert counts["visible_now"] == 100


def test_refresh_is_incremental_never_double_counting(db):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO trip_events (vehicle_identifier, vehicle_plate, cycle_id, detected_at, form_factor, "
            "vehicle_use_type, vehicle_model_name, from_lat, from_lon, to_lat, to_lon, distance_meters) "
            "VALUES ('v1', '1010000', NULL, %s, 'scooter', 'standing', 'Cosmo', 39.74, -104.98, 39.75, "
            "-104.99, 500)", (T,))
    db.commit()
    ar.refresh()
    ar.refresh()
    with db.cursor() as cur:
        cur.execute("SELECT SUM(rides) FROM analytics_rides_hourly WHERE region_type = 'city'")
        assert cur.fetchone()[0] == 1


def test_off_map_counts_recent_vehicles_missing_from_this_cycle(db):
    cid, old = uuid.uuid4(), uuid.uuid4()
    with db.cursor() as cur:
        for c in (cid, old):
            cur.execute("INSERT INTO observation_cycles (cycle_id, job_status) VALUES (%s, 'complete')", (str(c),))
        cur.execute("INSERT INTO device_state_processed_cycles (cycle_id, snapshot_time, eligible_count, "
                    "counts_as_observation) VALUES (%s, %s, 1, true)", (str(cid), NOW))
        cur.execute("INSERT INTO device_status_snapshots (cycle_id, snapshot_time, total, available, "
                    "reserved, out_of_service, models) VALUES (%s, %s, 1, 1, 0, 0, '{}')", (str(cid), NOW))
        for vid, cycle, seen in (("here", cid, NOW), ("gone", old, NOW - timedelta(days=2)),
                                 ("ancient", old, NOW - timedelta(days=30))):
            cur.execute("INSERT INTO device_state (vehicle_identifier, current_device_id, current_lat, "
                        "current_lon, current_spatial_status, first_observed_at_location, "
                        "first_ever_observed_at, last_observed_at, last_cycle_id) "
                        "VALUES (%s, %s, 39.74, -104.98, 'denver_core', %s, %s, %s, %s)",
                        (vid, vid, seen, seen, seen, str(cycle)))
    db.commit()
    ar.refresh(cid, NOW)
    with db.cursor() as cur:
        cur.execute("SELECT off_map FROM device_status_snapshots WHERE cycle_id = %s", (str(cid),))
        assert cur.fetchone()[0] == 1   # "gone" only


def test_a_cycle_skips_the_rollups_while_the_backfill_holds_the_lock(db):
    import os
    import psycopg
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO trip_events (vehicle_identifier, vehicle_plate, cycle_id, detected_at, form_factor, "
            "vehicle_use_type, vehicle_model_name, from_lat, from_lon, to_lat, to_lon, distance_meters) "
            "VALUES ('v9', '1090000', NULL, %s, 'scooter', 'standing', 'Cosmo', 39.74, -104.98, 39.75, "
            "-104.99, 500)", (T,))
    db.commit()
    holder = psycopg.connect(os.environ["VEO_TEST_PG_DSN"])
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (ar._LOCK_KEY,))
        assert ar.refresh() == {"rides": 0, "stops": 0, "region_devices": 0}, "a cycle must not wait"
    finally:
        holder.close()
    assert ar.refresh()["rides"] == 1



def _stop(cur, vid, arrived, departed, failed=0):
    cur.execute(
        "INSERT INTO device_history (vehicle_identifier, snapshot_time, departed_at, lat, lon, "
        "spatial_status, form_factor, device_id_observed, dwell_failed_starts, vehicle_model_name) "
        "VALUES (%s, %s, %s, 39.74, -104.98, 'denver_core', 'scooter', %s, %s, 'Cosmo') RETURNING id",
        (vid, arrived, departed, vid, failed))
    return cur.fetchone()[0]


def _city_failed(db):
    with db.cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(failed_starts), 0) FROM analytics_failed_starts_hourly "
                    "WHERE region_type = 'city'")
        return cur.fetchone()[0]


def test_a_close_backdated_by_weeks_is_counted_once(db):
    """close_ghost_stops / a device_state outage stamp departed_at far in the
    past. A departed_at watermark skipped those forever; the queue does not."""
    with db.cursor() as cur:
        sid = _stop(cur, "ghost", T - timedelta(days=2, hours=1), None)
        cur.execute("UPDATE device_history SET departed_at = %s, dwell_failed_starts = 3 WHERE id = %s",
                    (T - timedelta(days=2), sid))
        cur.execute("UPDATE analytics_stop_closes SET closed_at = %s", (T,))
    db.commit()
    ar.refresh()
    ar.refresh()
    assert _city_failed(db) == 3


def test_a_reopened_stop_is_counted_once_when_it_finally_closes(db):
    with db.cursor() as cur:
        sid = _stop(cur, "inplace", T - timedelta(hours=3), T - timedelta(hours=2), failed=1)
        # In-place release: the close is undone before it settles...
        cur.execute("UPDATE device_history SET departed_at = NULL WHERE id = %s", (sid,))
        # ...and the stop really closes later.
        cur.execute("UPDATE device_history SET departed_at = %s WHERE id = %s", (T, sid))
        cur.execute("SELECT COUNT(*) FROM analytics_stop_closes WHERE stop_id = %s", (sid,))
        assert cur.fetchone()[0] == 1
        cur.execute("UPDATE analytics_stop_closes SET closed_at = %s", (T,))
    db.commit()
    ar.refresh()
    assert _city_failed(db) == 1


def _pre_cutover(db, cur, sid):
    """Make stop `sid` look like it closed before the migration: inside the
    cutover id, and never queued (the trigger did not exist yet)."""
    cur.execute("UPDATE analytics_rollup_state SET watermark_id = %s WHERE name = 'stops_cutover'", (sid,))
    cur.execute("DELETE FROM analytics_stop_closes WHERE stop_id = %s", (sid,))


def test_legacy_stops_are_swept_once_and_never_by_the_queue(db):
    with db.cursor() as cur:
        sid = _stop(cur, "old", T - timedelta(days=4, hours=1), T - timedelta(days=4), failed=5)
        _pre_cutover(db, cur, sid)
        cur.execute("UPDATE analytics_rollup_state SET watermark_time = %s WHERE name = 'stops'",
                    (T - timedelta(days=5),))
    db.commit()
    for _ in range(10):
        ar.refresh(backfill=True)
    assert _city_failed(db) == 5


def test_a_stop_open_at_cutover_closed_later_with_an_old_departure_counts_once(db):
    """zneill-agent's P1: open at the cutover, then closed AFTER the legacy
    sweep finished with departed_at backdated before the cutover (ghost-stop
    cleanup after an outage). It must be counted exactly once, by the queue."""
    with db.cursor() as cur:
        sid = _stop(cur, "open-at-cut", T - timedelta(days=6), None)
        _pre_cutover(db, cur, sid)
        cur.execute("INSERT INTO analytics_open_at_cutover (stop_id) VALUES (%s)", (sid,))
    db.commit()
    for _ in range(10):                       # legacy sweep reaches the cutover
        ar.refresh(backfill=True)
    assert _city_failed(db) == 0
    with db.cursor() as cur:                  # the backdated close, after the sweep
        cur.execute("UPDATE device_history SET departed_at = %s, dwell_failed_starts = 4 WHERE id = %s",
                    (T - timedelta(days=5), sid))
        cur.execute("UPDATE analytics_stop_closes SET closed_at = %s", (T,))
    db.commit()
    for _ in range(3):
        ar.refresh(backfill=True)
    assert _city_failed(db) == 4


def test_a_stop_created_after_cutover_is_the_queues_alone(db):
    with db.cursor() as cur:
        _stop(cur, "born-closed", T - timedelta(days=4, hours=1), T - timedelta(days=4), failed=2)
        cur.execute("UPDATE analytics_rollup_state SET watermark_time = %s WHERE name = 'stops'",
                    (T - timedelta(days=5),))
        cur.execute("UPDATE analytics_stop_closes SET closed_at = %s", (T,))
    db.commit()
    for _ in range(10):
        ar.refresh(backfill=True)
    assert _city_failed(db) == 2


def test_a_locked_off_map_row_cannot_hold_the_cycle(db, monkeypatch):
    """zneill-agent's P2: the statement timeout covers off_map too."""
    import os
    import time
    import psycopg
    cid = uuid.uuid4()
    with db.cursor() as cur:
        cur.execute("INSERT INTO observation_cycles (cycle_id, job_status) VALUES (%s, 'complete')", (str(cid),))
        cur.execute("INSERT INTO device_status_snapshots (cycle_id, snapshot_time, total, available, "
                    "reserved, out_of_service, models) VALUES (%s, %s, 1, 1, 0, 0, '{}')", (str(cid), NOW))
        cur.execute("INSERT INTO device_state_processed_cycles (cycle_id, snapshot_time, eligible_count, "
                    "counts_as_observation) VALUES (%s, %s, 1, true)", (str(cid), NOW))
    db.commit()
    monkeypatch.setattr(ar, "CYCLE_STATEMENT_TIMEOUT", "300ms")
    holder = psycopg.connect(os.environ["VEO_TEST_PG_DSN"])
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT 1 FROM device_status_snapshots WHERE cycle_id = %s FOR UPDATE", (str(cid),))
        t = time.monotonic()
        ar.refresh(cid, NOW)                  # must give up, not wait for the lock
        assert time.monotonic() - t < 3
    finally:
        holder.rollback()
        holder.close()
    db.rollback()


def test_off_map_is_not_recorded_when_device_state_missed_the_cycle(db):
    cid = uuid.uuid4()
    with db.cursor() as cur:
        cur.execute("INSERT INTO observation_cycles (cycle_id, job_status) VALUES (%s, 'complete')", (str(cid),))
        cur.execute("INSERT INTO device_status_snapshots (cycle_id, snapshot_time, total, available, "
                    "reserved, out_of_service, models) VALUES (%s, %s, 1, 1, 0, 0, '{}')", (str(cid), NOW))
    db.commit()
    ar.refresh(cid, NOW)
    with db.cursor() as cur:
        cur.execute("SELECT off_map FROM device_status_snapshots WHERE cycle_id = %s", (str(cid),))
        assert cur.fetchone()[0] is None


def test_the_backfill_raises_instead_of_reporting_caught_up(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("statement timeout")
    monkeypatch.setattr(ar, "refresh_rides", boom)
    with pytest.raises(RuntimeError):
        ar.backfill(max_passes=2)
