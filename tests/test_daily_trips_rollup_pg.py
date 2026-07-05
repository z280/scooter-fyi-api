"""Postgres-backed integration test for the daily trip-rollup SQL.

`day_bounds_for_date` (pure window math) is covered by test_daily_trips.py.
This file covers the parts of `daily_trips.compute_for_date` that only a
real SQL engine exercises, and where a silent regression would miscount
popularity with no failing test:

    * RANK() OVER (ORDER BY trip_count DESC) — ties share a rank, and the
      next rank skips (RANK, not ROW_NUMBER or DENSE_RANK).
    * DISTINCT ON (vehicle_identifier) ... ORDER BY detected_at DESC —
      per-vehicle plate/model/etc. reflect the vehicle's MOST RECENT trip
      that day, not the earliest.
    * DELETE-then-INSERT idempotency — re-running for the same date
      replaces the per-vehicle rows rather than doubling them (the
      docstring promises this for backfills / late-arriving events).
    * The half-open [start, end) Denver-day window — a trip at exactly
      the next midnight belongs to the NEXT day, not this one.

The repo has no Postgres test fixtures, so this SKIPS unless a reachable,
migratable test database is provided via VEO_TEST_PG_DSN, e.g.:

    VEO_TEST_PG_DSN='postgresql://postgres@127.0.0.1:5560/veo_test' pytest \
        tests/test_daily_trips_rollup_pg.py

CI can wire this up with a Postgres service container + that env var; until
then it's a no-op in the normal suite (never red, never falsely green).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from src import daily_trips  # noqa: E402  (after importorskip by design)

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
TRIP_DAY = date(2026, 6, 15)
# 2026-06-15 midnight Denver == 06:00 UTC; next midnight == 2026-06-16 06:00 UTC.
DAY_START_UTC = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)
DAY_END_UTC = datetime(2026, 6, 16, 6, 0, tzinfo=timezone.utc)


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture()
def pg_conn(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — Postgres rollup integration test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    # Every migration is IF NOT EXISTS / ADD COLUMN IF NOT EXISTS, so applying
    # them directly (same as src.pg.run_migrations does per file) is safe to
    # repeat across runs. Ordered by filename == the runner's order.
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()

    # Clean slate for just the trip tables (leave the rest of the schema).
    with conn.cursor() as cur:
        cur.execute("DELETE FROM daily_vehicle_trip_counts")
        cur.execute("DELETE FROM daily_trip_summary")
        cur.execute("DELETE FROM trip_events")
    conn.commit()

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(daily_trips, "connection", _fake_connection)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _insert_trip(conn, vid, plate, detected_at, model=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trip_events (
                vehicle_identifier, vehicle_plate, cycle_id, detected_at,
                form_factor, vehicle_use_type, vehicle_model_name,
                from_lat, from_lon, to_lat, to_lon, distance_meters
            ) VALUES (%s, %s, NULL, %s, 'bicycle', 'sitting', %s,
                      39.74, -104.98, 39.75, -104.99, 500.0)
            """,
            (vid, plate, detected_at, model),
        )
    conn.commit()


def _seed(conn):
    """Trip counts for 2026-06-15 Denver:
        A: 2   B: 2   D: 2   -> three-way tie, RANK 1
        C: 1   E: 1         -> RANK 4 (three ranks consumed by the tie)
    D's plate changes mid-day (D_old @07:00 -> D_new @08:00): most-recent wins.
    E has one in-window trip (at exactly the day's start) plus one at exactly
    the next midnight (must be EXCLUDED by the half-open window).
    """
    inside = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    _insert_trip(conn, "idA", "A1", inside)
    _insert_trip(conn, "idA", "A1", inside)
    _insert_trip(conn, "idB", "B1", inside)
    _insert_trip(conn, "idB", "B1", inside)
    _insert_trip(conn, "idC", "C1", inside)
    # D: earlier then later, different plate — DISTINCT ON detected_at DESC
    # must surface the later "D_new".
    _insert_trip(conn, "idD", "D_old", datetime(2026, 6, 15, 13, 0, tzinfo=timezone.utc), model="Cosmo")
    _insert_trip(conn, "idD", "D_new", datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc), model="Apollo")
    # E: boundary — one at exactly DAY_START (counts), one at exactly DAY_END (excluded).
    _insert_trip(conn, "idE", "E1", DAY_START_UTC)
    _insert_trip(conn, "idE", "E1", DAY_END_UTC)


def _counts(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT vehicle_identifier, vehicle_plate, vehicle_model_name, "
            "trip_count, popularity_rank FROM daily_vehicle_trip_counts "
            "WHERE trip_date = %s ORDER BY vehicle_identifier",
            (TRIP_DAY,),
        )
        return {r[0]: {"plate": r[1], "model": r[2], "count": r[3], "rank": r[4]}
                for r in cur.fetchall()}


def _summary(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT total_trips, distinct_vehicles_tripped "
            "FROM daily_trip_summary WHERE trip_date = %s",
            (TRIP_DAY,),
        )
        return cur.fetchone()


def test_rollup_summary_and_boundary(pg_conn):
    _seed(pg_conn)
    daily_trips.compute_for_date(TRIP_DAY)

    total, distinct = _summary(pg_conn)
    # A2 + B2 + C1 + D2 + E1(start only) = 8 ; E's end-midnight trip excluded.
    assert total == 8
    assert distinct == 5

    counts = _counts(pg_conn)
    assert counts["idE"]["count"] == 1, "trip at exactly next-midnight must not count for this day"


def test_rollup_rank_ties_use_RANK_not_row_number(pg_conn):
    _seed(pg_conn)
    daily_trips.compute_for_date(TRIP_DAY)
    counts = _counts(pg_conn)

    # Three vehicles tied at 2 trips all share rank 1...
    assert counts["idA"]["rank"] == 1
    assert counts["idB"]["rank"] == 1
    assert counts["idD"]["rank"] == 1
    # ...and RANK() skips to 4 for the next group (not 2 for ROW_NUMBER, not
    # 2 for DENSE_RANK).
    assert counts["idC"]["rank"] == 4
    assert counts["idE"]["rank"] == 4


def test_rollup_picks_most_recent_plate_and_model(pg_conn):
    _seed(pg_conn)
    daily_trips.compute_for_date(TRIP_DAY)
    counts = _counts(pg_conn)
    assert counts["idD"]["count"] == 2
    assert counts["idD"]["plate"] == "D_new"
    assert counts["idD"]["model"] == "Apollo"


def test_rollup_is_idempotent_across_reruns(pg_conn):
    _seed(pg_conn)
    daily_trips.compute_for_date(TRIP_DAY)
    first = _counts(pg_conn)
    first_summary = _summary(pg_conn)

    # A backfill / late-event re-run must REPLACE, not accumulate.
    daily_trips.compute_for_date(TRIP_DAY)
    second = _counts(pg_conn)
    second_summary = _summary(pg_conn)

    assert len(second) == len(first) == 5
    assert second == first
    assert second_summary == first_summary
