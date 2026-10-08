"""addresses.nearest — the Denver address point that names a reverse-geocoded
pin (GET /api/v1/geocode/reverse). Real SQL, because the bbox prefilter and
the ORDER BY are the whole function.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from src import addresses  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
LAT, LON = 39.737900, -104.989900


@pytest.fixture()
def pg_conn(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set")
    try:
        conn = psycopg.connect(dsn, connect_timeout=3)
    except Exception:  # noqa: BLE001
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
        cur.execute("DELETE FROM address_points")
        cur.execute("DELETE FROM address_streets")
        cur.execute(
            "INSERT INTO address_streets (street_name, posttype, search_key, name_key, "
            "display_name) VALUES ('BANNOCK', 'ST', 'BANNOCK ST', 'BANNOCK', 'Bannock St') "
            "RETURNING id")
        (sid,) = cur.fetchone()
        # 1437 is ~11 m north of the pin, 1450 ~33 m, 1500 ~110 m.
        for number, dlat in (("1450", 0.0003), ("1437", 0.0001), ("1500", 0.001)):
            cur.execute(
                "INSERT INTO address_points (street_id, number, number_text, lat, lon) "
                "VALUES (%s, %s, %s, %s, %s)", (sid, int(number), number, LAT + dlat, LON))
    conn.commit()

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(addresses, "connection", _fake_connection)
    try:
        yield conn
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM address_points")
            cur.execute("DELETE FROM address_streets")
        conn.commit()
        conn.close()


def test_nearest_point_within_range(pg_conn):
    hit = addresses.nearest(LAT, LON)
    assert hit["housenumber"] == "1437" and hit["street"] == "Bannock St"
    assert 10 < hit["distance_m"] < 13


def test_nothing_within_range_is_none(pg_conn):
    assert addresses.nearest(LAT - 0.002, LON) is None        # ~220 m south
    assert addresses.nearest(LAT, LON, max_meters=5) is None


def test_database_failure_is_none_not_an_error(monkeypatch):
    @contextmanager
    def broken():
        raise RuntimeError("db down")
        yield  # pragma: no cover
    monkeypatch.setattr(addresses, "connection", broken)
    assert addresses.nearest(LAT, LON) is None
