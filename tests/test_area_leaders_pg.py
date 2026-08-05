"""Postgres-backed coverage for territory control against a real database —
src/area_leaders.py:refresh_universe() and, since sql/061 moved the
leaderboard to read time, the LIVE read path in src/api_leaderboard.py.

The read path is now the part that most needs a real Postgres. A fake
cursor can prove the handler groups and filters what it is handed; only a
real database can prove the SQL asks for the right rows in the right
order — the `GROUP BY (h3_8_index, account_id)`, the window boundary, and
the `points DESC, first_point_at ASC, account_id ASC` tie-break that used
to be a Python sort inside the nightly job.

So this file covers:

  * the universe union actually matching real device_history /
    device_state / user_points contents (not just three canned lists),
  * full-replace idempotence against a real transaction (re-running with
    unchanged data replaces rather than accumulates),
  * the live per-cell and regional reads over real ledger rows, including
    the tie-break and the window boundary, and
  * account deletion cascading through user_points so a deleted rider
    stops holding territory.

tests/test_area_leaders_logic.py covers the union-dedup and ranking LOGIC
with a fake cursor; this file trusts that logic and exercises the real SQL
and real constraints around it.

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

from src import api_leaderboard, area_leaders  # noqa: E402
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
        cur.execute("DELETE FROM h3_r8_area_report")
        cur.execute("DELETE FROM user_points")
        cur.execute("DELETE FROM device_history")
        cur.execute("DELETE FROM device_state")
        cur.execute("DELETE FROM accounts WHERE email LIKE %s", (_TEST_EMAIL_LIKE,))
    conn.commit()

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(area_leaders, "connection", _fake_connection)
    # The read path opens its own connection; point it at the same one so a
    # test can seed and then read inside one transaction.
    monkeypatch.setattr(api_leaderboard, "connection", _fake_connection)
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
            "SELECT h3_8_index, has_devices, has_points "
            "FROM h3_r8_area_report ORDER BY h3_8_index"
        )
        return {r[0]: r[1:] for r in cur.fetchall()}


def _map_cells(conn) -> dict[int, dict]:
    """Drive the real live handler and re-key its payload by integer cell id
    so tests can index it the same way they seed."""
    from fastapi import Response
    from starlette.requests import Request

    req = Request({"type": "http", "method": "GET", "path": "/api/v1/leaderboard/map",
                   "headers": [], "query_string": b""})
    out = api_leaderboard.leaderboard_map(req, Response())
    return {int(key, 16): cell for key, cell in out["cells"].items()}


def _regional(conn) -> list[dict]:
    from fastapi import Response
    from starlette.requests import Request

    req = Request({"type": "http", "method": "GET", "path": "/api/v1/leaderboard/regional",
                   "headers": [], "query_string": b""})
    return api_leaderboard.leaderboard_regional(req, Response())["leaders"]


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
    # pending_review, in a cell of its own — must NOT enter the universe.
    _insert_point(pg_conn, account_id, 9999, 10, _RECENT, status="pending_review")

    area_leaders.refresh_universe(window_days=28)

    report = _report_rows(pg_conn)
    assert set(report) == {1001, 1002, 1003}, "NULL h3 excluded; pending_review cell excluded"
    assert report[1001] == (True, False), "device sources only"
    assert report[1002] == (True, True), "device_state AND points"
    assert report[1003] == (False, True), "points only"


def test_refresh_is_a_full_replace_not_an_accumulation(pg_conn):
    _insert_device_history(pg_conn, 1001)
    area_leaders.refresh_universe(window_days=28)
    first = _report_rows(pg_conn)

    area_leaders.refresh_universe(window_days=28)
    second = _report_rows(pg_conn)
    assert first == second, "same underlying data -> byte-identical rows, not doubled"

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT cell_count) FROM h3_r8_area_leader_runs")
        run_count, distinct_cell_counts = cur.fetchone()
    assert run_count == 2, "the runs table is append-only — one row per call"
    assert distinct_cell_counts == 1, "same data -> same cell_count both runs"


def test_a_cell_that_drops_out_of_the_universe_is_removed(pg_conn):
    _insert_device_history(pg_conn, 1001)
    area_leaders.refresh_universe(window_days=28)
    assert set(_report_rows(pg_conn)) == {1001}

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM device_history")
    pg_conn.commit()
    area_leaders.refresh_universe(window_days=28)
    assert _report_rows(pg_conn) == {}, "full replace, so a vanished cell really vanishes"


# ---------------------------------------------------------------------------
# The live read path, against real ledger rows
# ---------------------------------------------------------------------------
def test_live_map_groups_by_cell_and_account_with_real_totals(pg_conn):
    a1, a2 = _account(pg_conn), _account(pg_conn)
    # a1 earns twice in cell 1001 — the GROUP BY must sum them into one entry.
    _insert_point(pg_conn, a1, 1001, 30, _RECENT)
    _insert_point(pg_conn, a1, 1001, 20, _RECENT)
    _insert_point(pg_conn, a2, 1001, 40, _RECENT)
    area_leaders.refresh_universe(window_days=28)

    cell = _map_cells(pg_conn)[1001]
    assert cell["leader"]["points"] == 50, "two rows for one rider sum to one entry"
    assert [r["points"] for r in cell["runners_up"]] == [40]
    assert cell["total_points"] == 90
    assert cell["distinct_earners"] == 2, "earners, not ledger rows"


def test_live_map_tie_break_prefers_whoever_got_there_first(pg_conn):
    earlier, later = _account(pg_conn), _account(pg_conn)
    # Equal points; `later` was created first, so account_id order would put
    # it on top if first_point_at were not the second key.
    _insert_point(pg_conn, later, 1001, 10, _NOW - timedelta(days=2))
    _insert_point(pg_conn, earlier, 1001, 10, _NOW - timedelta(days=5))
    area_leaders.refresh_universe(window_days=28)

    cell = _map_cells(pg_conn)[1001]
    assert cell["leader"]["points"] == 10
    with pg_conn.cursor() as cur:
        cur.execute("SELECT display_name FROM accounts WHERE id = %s", (earlier,))
        (name,) = cur.fetchone()
    assert cell["leader"]["display_name"] == name, \
        "whoever got there first holds the territory"


def test_live_map_excludes_points_older_than_the_window(pg_conn):
    account_id = _account(pg_conn)
    _insert_point(pg_conn, account_id, 1001, 10, _NOW - timedelta(days=29))
    _insert_device_history(pg_conn, 1001)
    area_leaders.refresh_universe(window_days=28)

    cell = _map_cells(pg_conn)[1001]
    assert cell["leader"] is None and cell["total_points"] == 0, \
        "the cell is still on the map (it has devices) but nobody holds it"


def test_live_map_excludes_pending_review_points(pg_conn):
    account_id = _account(pg_conn)
    _insert_point(pg_conn, account_id, 1001, 10, _RECENT, status="pending_review")
    _insert_device_history(pg_conn, 1001)
    area_leaders.refresh_universe(window_days=28)
    assert _map_cells(pg_conn)[1001]["leader"] is None


def test_live_map_shows_a_cell_the_universe_has_not_seen_yet(pg_conn):
    # The universe is refreshed weekly; a first point in a brand-new cell
    # must not wait for it.
    account_id = _account(pg_conn)
    area_leaders.refresh_universe(window_days=28)      # universe is empty
    _insert_point(pg_conn, account_id, 4242, 10, _RECENT)

    cell = _map_cells(pg_conn)[4242]
    assert cell["leader"]["points"] == 10


def test_live_regional_sums_one_rider_across_cells(pg_conn):
    a1, a2 = _account(pg_conn), _account(pg_conn)
    _insert_point(pg_conn, a1, 1001, 30, _RECENT)
    _insert_point(pg_conn, a1, 1002, 30, _RECENT)   # same rider, another cell
    _insert_point(pg_conn, a2, 1001, 50, _RECENT)

    leaders = _regional(pg_conn)
    assert [e["points"] for e in leaders] == [60, 50], \
        "the regional board collapses the cell dimension away"
    assert [e["rank"] for e in leaders] == [1, 2]


def test_deleting_an_account_releases_its_territory(pg_conn):
    holder, other = _account(pg_conn), _account(pg_conn)
    _insert_point(pg_conn, holder, 1001, 98, _RECENT)
    _insert_point(pg_conn, other, 1001, 10, _RECENT)
    area_leaders.refresh_universe(window_days=28)
    assert _map_cells(pg_conn)[1001]["leader"]["points"] == 98

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM accounts WHERE id = %s", (holder,))
    pg_conn.commit()

    cell = _map_cells(pg_conn)[1001]
    assert cell["leader"]["points"] == 10, (
        "user_points ON DELETE CASCADE removes the rows, and because the "
        "board is computed at read time the hexagon changes hands immediately"
    )
    assert cell["distinct_earners"] == 1
