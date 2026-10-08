"""Plate lookups (src/api_vehicle_plates.py) against real Postgres.

What only a real database can show:

  * the SQL runs against the real schema (raw_telemetry_points.vehicle_plate,
    the cycle join shared with the devices payload);
  * CURRENT SNAPSHOT ONLY — a device_id or plate seen only in an older cycle
    is not answered, because bike_id rotates and an old id can now belong to
    another vehicle;
  * the SQL-side plate normalisation (`[[:space:]-]+` under Postgres's regex
    dialect) agrees with the Python/frontend one;
  * the real rate limiter, with real commit semantics: each request gets its
    own connection that commits on success and rolls back on an exception,
    like the pool. That is what proves a 404 MISS still consumes quota — the
    property that makes the per-IP cap an enumeration guard at all.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
NEVER point that at production: the fixture executes every migration.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

psycopg = pytest.importorskip("psycopg")

from src import api_vehicle_plates  # noqa: E402
from src.accounts import SessionUser, require_session  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

# Far enough ahead that these cycles are "the latest complete" for the test's
# duration whatever else is in the database; deleted again on teardown so
# they never shadow another suite's cycle.
_BASE = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)
_BUCKETS = ("vehicle_plates_account", "vehicle_plates_ip", "vehicle_resolve_ip")


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture()
def pg(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — vehicle plates Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
        cur.execute("DELETE FROM rate_limit_events WHERE bucket = ANY(%s)", (list(_BUCKETS),))
    conn.commit()

    @contextmanager
    def _per_request_connection():
        # psycopg's Connection context manager: COMMIT on a clean exit,
        # ROLLBACK when the block raises — the pool's semantics.
        with psycopg.connect(dsn) as c:
            yield c

    monkeypatch.setattr(api_vehicle_plates, "connection", _per_request_connection)
    cycles: list[uuid.UUID] = []
    try:
        yield conn, cycles
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM raw_telemetry_points WHERE cycle_id = ANY(%s)", (cycles,))
            cur.execute("DELETE FROM snapshot_metadata_core WHERE cycle_id = ANY(%s)", (cycles,))
            cur.execute("DELETE FROM observation_cycles WHERE cycle_id = ANY(%s)", (cycles,))
            cur.execute("DELETE FROM rate_limit_events WHERE bucket = ANY(%s)", (list(_BUCKETS),))
        conn.commit()
        conn.close()


def _cycle(pg, minutes: int, rows: list[tuple[str, str | None, str]],
           status: str = "complete") -> datetime:
    """One cycle at _BASE + minutes with rows (device_id, plate, vid)."""
    conn, cycles = pg
    cid = uuid.uuid4()
    cycles.append(cid)
    snap = _BASE + timedelta(minutes=minutes)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO observation_cycles (cycle_id, job_status) VALUES (%s, %s)",
            (cid, status),
        )
        cur.execute(
            "INSERT INTO snapshot_metadata_core (cycle_id, snapshot_time) VALUES (%s, %s)",
            (cid, snap),
        )
        for device_id, plate, vid in rows:
            cur.execute(
                """
                INSERT INTO raw_telemetry_points (
                    cycle_id, snapshot_time, device_id, form_factor,
                    latitude, longitude, spatial_status,
                    vehicle_identifier, vehicle_plate
                ) VALUES (%s, %s, %s, 'scooter', 39.7392, -104.9903,
                          'denver_core', %s, %s)
                """,
                (cid, snap, device_id, vid, plate),
            )
    conn.commit()
    return snap


def _client(signed_in: bool = True, account_id: int = 4242) -> TestClient:
    app = FastAPI()
    app.include_router(api_vehicle_plates.router)
    if signed_in:
        user = SessionUser(
            account_id=account_id, email="pgtest-plates@example.com", scopes=("rider",),
            expires_at=None, sliding=True, method="google", token_sha256="x",
        )
        app.dependency_overrides[require_session] = lambda: user
    return TestClient(app)


def _fleet(pg) -> datetime:
    # Older cycle: bike-1 carried a DIFFERENT plate (bike_id rotated onto
    # another vehicle since), and bike-old existed only then.
    _cycle(pg, 0, [
        ("bike-1", "5550001", "aaaaaaaaaaaaaaa1"),
        ("bike-old", "5550002", "aaaaaaaaaaaaaaa2"),
    ])
    current = _cycle(pg, 10, [
        ("bike-1", "1025543", "8c4a1f0d2e9b7a35"),
        ("bike-2", "ab-12 3", "0123456789abcdef"),
        ("bike-3", None, "fedcba9876543210"),
    ])
    # A newer cycle still running is NOT the snapshot the map serves.
    _cycle(pg, 20, [("bike-1", "7770001", "bbbbbbbbbbbbbbb1")], status="in_progress")
    return current


def test_forward_reads_only_the_current_complete_snapshot(pg):
    snap = _fleet(pg)
    r = _client().get(
        "/api/v1/vehicles/plates",
        params={"device_ids": "bike-1,bike-2,bike-3,bike-old,nope"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "plates": {"bike-1": "1025543", "bike-2": "ab-12 3"},
        "as_of": snap.isoformat(),
    }
    assert r.headers["cache-control"] == "private, no-store"


def test_reverse_resolves_current_vehicle_with_sql_side_normalisation(pg):
    _fleet(pg)
    c = _client(signed_in=False)

    r = c.get("/api/v1/vehicles/resolve", params={"plate": "10-25 543"})
    assert r.status_code == 200, r.text
    assert r.json() == {"device_id": "bike-1", "vehicle_identifier": "8c4a1f0d2e9b7a35"}
    assert "1025543" not in r.text

    # Stored "ab-12 3" — separators and case stripped on the SQL side too.
    r = c.get("/api/v1/vehicles/resolve", params={"plate": "AB123"})
    assert r.status_code == 200, r.text
    assert r.json()["device_id"] == "bike-2"
    assert "AB" not in r.text and "ab-12" not in r.text


def test_reverse_plate_from_an_older_cycle_is_404(pg):
    _fleet(pg)
    c = _client(signed_in=False)
    assert c.get("/api/v1/vehicles/resolve", params={"plate": "5550001"}).status_code == 404
    assert c.get("/api/v1/vehicles/resolve", params={"plate": "5550002"}).status_code == 404
    # Nor the in-progress newer cycle's plate.
    assert c.get("/api/v1/vehicles/resolve", params={"plate": "7770001"}).status_code == 404


def test_reverse_rate_limit_is_real_and_misses_are_charged(pg):
    _fleet(pg)
    c = _client(signed_in=False)
    hdr = {"CF-Connecting-IP": "192.0.2.77"}
    for i in range(api_vehicle_plates.LIMIT_RESOLVE_PER_IP[0]):
        r = c.get("/api/v1/vehicles/resolve", params={"plate": f"{8000000 + i}"},
                  headers=hdr)
        assert r.status_code == 404
    r = c.get("/api/v1/vehicles/resolve", params={"plate": "1025543"}, headers=hdr)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1

    conn, _ = pg
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM rate_limit_events WHERE bucket = 'vehicle_resolve_ip' AND key = %s",
            ("192.0.2.77",),
        )
        assert cur.fetchone()[0] == api_vehicle_plates.LIMIT_RESOLVE_PER_IP[0]
    conn.commit()


def test_forward_rate_limit_per_account_is_real(pg):
    _fleet(pg)
    c = _client(account_id=777001)
    for _ in range(api_vehicle_plates.LIMIT_PLATES_PER_ACCOUNT[0]):
        assert c.get("/api/v1/vehicles/plates",
                     params={"device_ids": "bike-1"}).status_code == 200
    r = c.get("/api/v1/vehicles/plates", params={"device_ids": "bike-1"})
    assert r.status_code == 429
