"""Postgres-backed coverage for sql/048_h3_r8_area_leaders.sql +
src/area_leaders.py:recompute() — the parts a fake cursor cannot exercise:

  * the universe union actually matching real device_history /
    device_state / user_points contents (not just three canned lists),
  * full-replace idempotence against a real transaction (re-running with
    unchanged underlying data produces byte-identical h3_r8_area_leaders
    rows, not an accumulation),
  * the account -> h3_r8_area_leaders ON DELETE CASCADE actually firing.

tests/test_area_leaders_logic.py covers the tie-break / confirmed-only /
union-dedup LOGIC with a fake cursor; this file trusts that logic and
instead exercises the real SQL and real constraints around it.

SKIPS unless a reachable, migratable test database is provided via
VEO_TEST_PG_DSN (same contract as tests/test_daily_trips_rollup_pg.py /
tests/test_ride_usuals_pg.py). NEVER point that at production: the fixture
executes every migration and wipes device_history/device_state/user_points/
the area-leaders tables to get a clean slate for each test.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from src import area_leaders  # noqa: E402
from src.accounts import upsert_account  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_TEST_EMAIL_LIKE = "pgtest-arealeaders-%@example.com"

# Well inside the default 28-day window and far from its boundary, so two
# recompute() calls a few seconds apart (idempotence test) never disagree
# about which rows fall inside it.
_NOW = datetime.now(timezone.utc)
_RECENT = _NOW - timedelta(days=1)


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


def _apply_all(conn) -> None:
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()


@pytest.fixture()
def pg_conn(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — area leaders Postgres integration test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)

    # Defensive: wipe user_points BEFORE replaying migrations, not after —
    # same ordering rule tests/test_track_donation_pg.py documents. A row
    # left behind by another _pg file with an action value only a LATER
    # migration's guarded widening admits would otherwise make an earlier
    # migration's own guarded rewrite (sql/037) choke on it mid-replay.
    # Guarded against UndefinedTable for a truly fresh cluster.
    with conn.cursor() as cur:
        try:
            cur.execute("DELETE FROM user_points")
        except psycopg.errors.UndefinedTable:
            conn.rollback()
    conn.commit()

    _apply_all(conn)

    # Clean slate for the tables this file seeds directly (leave the rest
    # of the schema alone) — same idiom as
    # tests/test_daily_trips_rollup_pg.py's fixture.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM h3_r8_area_leader_runs")
        cur.execute("DELETE FROM h3_r8_area_report")  # cascades h3_r8_area_leaders
        cur.execute("DELETE FROM user_points")
        cur.execute("DELETE FROM device_history")
        cur.execute("DELETE FROM device_state")
        cur.execute("DELETE FROM accounts WHERE email LIKE %s", (_TEST_EMAIL_LIKE,))
    conn.commit()

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(area_leaders, "connection", _fake_connection)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------
def _account(conn) -> int:
    with conn.cursor() as cur:
        account_id = upsert_account(cur, f"pgtest-arealeaders-{uuid.uuid4()}@example.com")
    conn.commit()
    return account_id


def _insert_device_history(conn, h3_8_index: int | None, vehicle_identifier: str | None = None) -> None:
    vid = vehicle_identifier or f"dh{uuid.uuid4().hex[:14]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO device_history
                (vehicle_identifier, snapshot_time, lat, lon, spatial_status,
                 device_id_observed, h3_8_index)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (vid, _NOW, 39.74, -104.98, "denver_core", "bike-1", h3_8_index),
        )
    conn.commit()


def _insert_device_state(conn, h3_8_index: int | None, vehicle_identifier: str | None = None) -> None:
    vid = vehicle_identifier or f"ds{uuid.uuid4().hex[:14]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO device_state
                (vehicle_identifier, current_lat, current_lon, current_spatial_status,
                 first_observed_at_location, first_ever_observed_at, last_observed_at,
                 current_h3_8_index)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (vid, 39.74, -104.98, "denver_core", _NOW, _NOW, _NOW, h3_8_index),
        )
    conn.commit()


def _insert_point(
    conn, account_id: int, h3_8_index: int, points: int, created_at: datetime,
    status: str = "confirmed",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_points
                (account_id, created_at, action, points, lat, lng, h3_8_index, status)
            VALUES (%s, %s, 'profile_completion', %s, %s, %s, %s, %s)
            """,
            (account_id, created_at, points, 39.74, -104.98, h3_8_index, status),
        )
    conn.commit()


def _report_rows(conn) -> dict[int, tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT h3_8_index, has_devices, has_points, total_points, distinct_earners "
            "FROM h3_r8_area_report ORDER BY h3_8_index"
        )
        return {r[0]: r[1:] for r in cur.fetchall()}


def _leader_rows(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT h3_8_index, rank, account_id, points, first_point_at "
            "FROM h3_r8_area_leaders ORDER BY h3_8_index, rank"
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Universe union matches real table contents
# ---------------------------------------------------------------------------
def test_universe_union_matches_real_table_contents(pg_conn):
    account_id = _account(pg_conn)

    # device_history only: 1001. Also a NULL-h3 row that must be excluded.
    _insert_device_history(pg_conn, 1001)
    _insert_device_history(pg_conn, None)

    # device_state overlaps device_history's cell (1001) and adds a new one (1002).
    _insert_device_state(pg_conn, 1001)
    _insert_device_state(pg_conn, 1002)

    # user_points (confirmed) overlaps device_state's 1002 and adds a new one (1003).
    _insert_point(pg_conn, account_id, 1002, 10, _RECENT)
    _insert_point(pg_conn, account_id, 1003, 10, _RECENT)
    # pending_review, in a cell of its own — must NOT enter the universe at all.
    _insert_point(pg_conn, account_id, 9999, 10, _RECENT, status="pending_review")

    area_leaders.recompute(window_days=28)

    report = _report_rows(pg_conn)
    assert set(report) == {1001, 1002, 1003}, "NULL h3 excluded; pending_review cell excluded"

    has_devices_1001, has_points_1001, _, _ = report[1001]
    assert (has_devices_1001, has_points_1001) == (True, False)

    has_devices_1002, has_points_1002, _, _ = report[1002]
    assert (has_devices_1002, has_points_1002) == (True, True), "seen by both device_state and user_points"

    has_devices_1003, has_points_1003, _, _ = report[1003]
    assert (has_devices_1003, has_points_1003) == (False, True)


# ---------------------------------------------------------------------------
# Full-replace idempotence
# ---------------------------------------------------------------------------
def test_full_replace_idempotence(pg_conn):
    a1 = _account(pg_conn)
    a2 = _account(pg_conn)
    a3 = _account(pg_conn)
    a4 = _account(pg_conn)

    # Cell 2001 gets 4 earners (only top 3 stored as leaders); cell 2002 gets 1.
    _insert_point(pg_conn, a1, 2001, 40, _RECENT)
    _insert_point(pg_conn, a2, 2001, 30, _RECENT)
    _insert_point(pg_conn, a3, 2001, 20, _RECENT)
    _insert_point(pg_conn, a4, 2001, 10, _RECENT)
    _insert_point(pg_conn, a1, 2002, 6, _RECENT)

    area_leaders.recompute(window_days=28)
    first_report = _report_rows(pg_conn)
    first_leaders = _leader_rows(pg_conn)

    assert set(first_report) == {2001, 2002}
    assert len(first_leaders) == 3 + 1, "top 3 of 4 earners in 2001, plus the lone earner in 2002"

    area_leaders.recompute(window_days=28)
    second_report = _report_rows(pg_conn)
    second_leaders = _leader_rows(pg_conn)

    assert second_report == first_report
    assert second_leaders == first_leaders, "re-running with unchanged data must not accumulate/reorder rows"

    # The run log is the one APPEND-ONLY table — two calls, two rows.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT cell_count), COUNT(DISTINCT led_cells) "
                    "FROM h3_r8_area_leader_runs")
        run_count, distinct_cell_counts, distinct_led_cells = cur.fetchone()
    assert run_count == 2
    assert distinct_cell_counts == 1, "same underlying data -> same cell_count both runs"
    assert distinct_led_cells == 1, "same underlying data -> same led_cells both runs"


# ---------------------------------------------------------------------------
# Account-delete CASCADE
# ---------------------------------------------------------------------------
def test_account_delete_cascade_removes_leader_rows(pg_conn):
    account_id = _account(pg_conn)
    _insert_point(pg_conn, account_id, 3001, 50, _RECENT)

    area_leaders.recompute(window_days=28)
    leaders_before = _leader_rows(pg_conn)
    assert any(row[2] == account_id for row in leaders_before)

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
    pg_conn.commit()

    leaders_after = _leader_rows(pg_conn)
    assert all(row[2] != account_id for row in leaders_after), \
        "ON DELETE CASCADE on h3_r8_area_leaders.account_id must remove the deleted account's rows"

    # The cell's report row is untouched by the cascade — it is a separate
    # FK (h3_8_index -> h3_r8_area_report), not account-keyed, and is only
    # ever refreshed by the next recompute(), not reactively by a delete.
    report = _report_rows(pg_conn)
    assert 3001 in report
