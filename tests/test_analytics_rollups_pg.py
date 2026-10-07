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
                  "analytics_region_devices_hourly", "analytics_rollup_state",
                  "regional_metrics_narrow", "device_status_snapshots"):
            cur.execute(f"DELETE FROM {t}")
        # Start the watermarks just before the fixture's rows, as a backfill
        # from the real epoch would reach them.
        cur.execute("INSERT INTO analytics_rollup_state (name, watermark_time) VALUES "
                    "('stops', %s), ('region_devices', %s)", (T - timedelta(hours=1), T - timedelta(hours=1)))
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
    db.commit()

    got = ar.backfill(max_passes=20)
    assert got["rides"] == 3

    c = _client()
    rides = c.get("/api/v1/analytics/rides?days=2&granularity=hour").json()
    assert rides["rides"] == 3 and rides["models"] == ["Astro", "Cosmo"]
    assert rides["series"][0]["by_model"] == {"Astro": 1, "Cosmo": 2}
    assert rides["series"][0]["bucket"].endswith(("-06:00", "-07:00"))   # Denver local
    by_region = c.get("/api/v1/analytics/rides?days=2&region_type=neighborhood&region_name=NB_Test").json()
    assert by_region["rides"] == 3

    fs = c.get("/api/v1/analytics/failed-starts?days=2").json()
    assert fs["failed_starts"] == 2 and fs["stops_with_failures"] == 1
    assert "under-reported" in fs["caveat"]

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
