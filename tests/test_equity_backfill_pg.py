"""Postgres-backed proof that `equity_backfill.reprocess_date(dry_run=True)`
performs zero writes (tests/test_equity_backfill.py covers the logic with
faked I/O; this covers the real SQL path).

Two independent checks:

  * the dry run succeeds on a session forced READ ONLY — any INSERT/UPDATE
    anywhere on the path (snapshot columns, daily_sla upsert, a job_runs
    row) would raise ReadOnlySqlTransaction. The write path is run on the
    same session to prove the guard is live, not vacuous; and
  * on an ordinary writable session, every table the job could touch is
    byte-identical before and after a dry run.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database:

    VEO_TEST_PG_DSN='postgresql://postgres@127.0.0.1:5560/veo_test' pytest \
        tests/test_equity_backfill_pg.py
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from src import daily_sla, equity_backfill as eb  # noqa: E402
from src.equity_backfill import Stop  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

# Far from any real data so a shared test DB cannot collide.
DAY = date(2031, 1, 15)
WIN_START, _ = daily_sla.window_for_date(DAY)
IN_EQUITY = (39.785137, -104.826320)
OUT_OF_EQUITY = (39.700000, -104.970000)
LIVE_PCT = 12.34  # the "live" figure already on the row; must survive


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture()
def seeded():
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — Postgres equity dry-run test skipped")
    if not _reachable(dsn):
        pytest.skip("VEO_TEST_PG_DSN unreachable")

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()

    with conn.cursor() as cur:  # leftovers from an aborted earlier run
        cur.execute("DELETE FROM device_history WHERE vehicle_identifier LIKE 'pgtest-eq-%%'")
        cur.execute(
            "DELETE FROM snapshot_metadata_core WHERE snapshot_time >= %s AND snapshot_time < %s"
            " RETURNING cycle_id",
            daily_sla.window_for_date(DAY),
        )
        stale = [r[0] for r in cur.fetchall()]
        cur.execute("DELETE FROM observation_cycles WHERE cycle_id = ANY(%s)", (stale,))
    conn.commit()

    cycles = [uuid.uuid4(), uuid.uuid4()]
    times = [WIN_START + timedelta(minutes=10), WIN_START + timedelta(minutes=12)]
    with conn.cursor() as cur:
        for cid, t in zip(cycles, times):
            cur.execute("INSERT INTO observation_cycles (cycle_id) VALUES (%s)", (cid,))
            cur.execute(
                """
                INSERT INTO snapshot_metadata_core
                    (cycle_id, snapshot_time, total_devices_denver,
                     total_devices_equity, percent_all_devices_equity)
                VALUES (%s, %s, 10, 1, %s)
                """,
                (cid, t, LIVE_PCT),
            )
        # 10 vehicles parked since before the window, 4 inside an equity area.
        for i in range(10):
            lat, lon = IN_EQUITY if i < 4 else OUT_OF_EQUITY
            cur.execute(
                """
                INSERT INTO device_history
                    (vehicle_identifier, cycle_id, snapshot_time, lat, lon,
                     spatial_status, form_factor, device_id_observed)
                VALUES (%s, %s, %s, %s, %s, 'denver_core', 'scooter', %s)
                """,
                (f"pgtest-eq-{i}", cycles[0], WIN_START - timedelta(hours=1),
                 lat, lon, f"dev{i}"),
            )
        cur.execute(
            """
            INSERT INTO daily_sla_compliance
                (sla_date, window_start_ts, window_end_ts, snapshot_count,
                 avg_percent_all_devices_equity)
            VALUES (%s, %s, %s, 2, %s)
            ON CONFLICT (sla_date) DO UPDATE
               SET avg_percent_all_devices_equity = EXCLUDED.avg_percent_all_devices_equity
            """,
            (DAY, *daily_sla.window_for_date(DAY), LIVE_PCT),
        )
    conn.commit()

    try:
        yield conn, cycles
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            # Takes effect from the NEXT transaction, hence the commit.
            cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE")
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM device_history WHERE vehicle_identifier LIKE 'pgtest-eq-%%'")
            cur.execute("DELETE FROM snapshot_metadata_core WHERE cycle_id = ANY(%s)", (cycles,))
            cur.execute("DELETE FROM observation_cycles WHERE cycle_id = ANY(%s)", (cycles,))
            cur.execute("DELETE FROM daily_sla_compliance WHERE sla_date = %s", (DAY,))
        conn.commit()
        conn.close()


def _route_through(monkeypatch, conn):
    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(eb, "connection", _fake_connection)
    monkeypatch.setattr(daily_sla, "connection", _fake_connection)
    # The spatial predicate is covered against the real map in
    # test_equity_backfill.py; here tag by coordinate so this test needs no
    # DuckDB spatial extension download.
    monkeypatch.setattr(
        eb, "tag_equity_membership",
        lambda stops: [
            Stop(**{**s.__dict__, "in_equity": (s.lat, s.lon) == IN_EQUITY}) for s in stops
        ],
    )


def _fingerprint(conn) -> dict:
    with conn.cursor() as cur:
        out = {}
        for table, where in (
            ("snapshot_metadata_core", "snapshot_time >= %(s)s AND snapshot_time < %(e)s"),
            ("daily_sla_compliance", "sla_date = %(d)s"),
            ("device_history", "vehicle_identifier LIKE 'pgtest-eq-%%'"),
        ):
            cur.execute(
                f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM {table} t WHERE {where}",
                {"s": WIN_START - timedelta(days=1), "e": WIN_START + timedelta(days=1), "d": DAY},
            )
            out[table] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*), MAX(id) FROM job_runs")
        out["job_runs"] = cur.fetchone()
    conn.commit()
    return out


def test_dry_run_succeeds_on_a_read_only_session_and_the_write_path_does_not(seeded, monkeypatch):
    conn, _ = seeded
    with conn.cursor() as cur:
        cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    conn.commit()
    _route_through(monkeypatch, conn)

    r = eb.reprocess_date(DAY, dry_run=True)
    conn.rollback()
    assert r.snapshots_considered == 2
    assert r.snapshots_passing_gate == 2
    assert r.snapshots_written == 0
    assert r.avg_percent_all_devices_equity == pytest.approx(40.0)
    assert all(s["fidelity"] == 1.0 for s in r.snapshots)

    # The guard is real: the write path on the same session is refused.
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        eb.reprocess_date(DAY, dry_run=False)
    conn.rollback()


def test_dry_run_leaves_every_touched_table_identical(seeded, monkeypatch):
    conn, _ = seeded
    _route_through(monkeypatch, conn)
    before = _fingerprint(conn)
    r = eb.reprocess_date(DAY, dry_run=True)
    conn.commit()  # would persist anything the dry run had written
    assert r.avg_percent_all_devices_equity == pytest.approx(40.0)
    assert _fingerprint(conn) == before
    with conn.cursor() as cur:
        cur.execute(
            "SELECT avg_percent_all_devices_equity FROM daily_sla_compliance WHERE sla_date = %s",
            (DAY,),
        )
        assert float(cur.fetchone()[0]) == LIVE_PCT
