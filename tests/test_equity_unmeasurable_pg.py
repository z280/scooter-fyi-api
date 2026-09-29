"""Postgres-backed: the `unmeasurable` verdict end to end (sql/084).

tests/test_equity_backfill.py and tests/test_compliance_calendar.py cover
the decision logic with faked I/O. This covers what only real SQL can:

  * the migration's column + CHECK, and that re-running it is a no-op;
  * `equity_backfill.reprocess_date` storing the verdict on a day whose
    every snapshot fails the fidelity gate — and REFUSING to store it on a
    day that already holds a live figure;
  * `daily_sla.compute_for_date` keeping the verdict while the average is
    still NULL and clearing it the moment it produces a figure; and
  * `/api/v1/compliance/calendar` and `/api/v1/compliance/daily` reading it
    back as `unmeasurable` / `equity_unmeasurable_reason`.

Every reconstruction here is built to FAIL the gate (20 open stops against a
recorded fleet of 1): leftover open stops from other tests in a shared
database can only push it further out, never back inside ±10%.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database:

    VEO_TEST_PG_DSN='postgresql://postgres@127.0.0.1:5560/veo_test' pytest \
        tests/test_equity_unmeasurable_pg.py
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from src import api_public, daily_sla, equity_backfill as eb  # noqa: E402
from src.equity_backfill import Stop  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
MIGRATION = SQL_DIR / "084_equity_unmeasurable.sql"

# Far from any real data (and from tests/test_equity_backfill_pg.py's 2031-01).
DAY_UNMEASURED = date(2031, 3, 10)   # no live equity figure; every snapshot fails
DAY_LIVE = date(2031, 3, 11)         # live equity figure; every snapshot fails too
DAYS = (DAY_UNMEASURED, DAY_LIVE)
LIVE_PCT = 12.34
IN_EQUITY = (39.785137, -104.826320)
OUT_OF_EQUITY = (39.700000, -104.970000)
VID_PREFIX = "pgtest-unm-"


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


def _cleanup(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM device_history WHERE vehicle_identifier LIKE %s",
                    (VID_PREFIX + "%",))
        stale: list = []
        for d in DAYS:
            cur.execute(
                "DELETE FROM snapshot_metadata_core"
                " WHERE snapshot_time >= %s AND snapshot_time < %s RETURNING cycle_id",
                daily_sla.window_for_date(d),
            )
            stale += [r[0] for r in cur.fetchall()]
        cur.execute("DELETE FROM observation_cycles WHERE cycle_id = ANY(%s)", (stale,))
        cur.execute("DELETE FROM daily_sla_compliance WHERE sla_date = ANY(%s)", (list(DAYS),))
    conn.commit()


@pytest.fixture()
def pg(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — Postgres unmeasurable test skipped")
    if not _reachable(dsn):
        pytest.skip("VEO_TEST_PG_DSN unreachable")

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()
    _cleanup(conn)

    @contextmanager
    def _fake_connection():
        yield conn

    for mod in (eb, daily_sla, api_public):
        monkeypatch.setattr(mod, "connection", _fake_connection)
    # The spatial predicate is covered against the real map elsewhere; tag by
    # coordinate so this needs no DuckDB spatial extension.
    monkeypatch.setattr(
        eb, "tag_equity_membership",
        lambda stops: [
            Stop(**{**s.__dict__, "in_equity": (s.lat, s.lon) == IN_EQUITY}) for s in stops
        ],
    )

    first_cycle = None
    with conn.cursor() as cur:
        for d in DAYS:
            start, _ = daily_sla.window_for_date(d)
            for minutes in (10, 12):
                cid = uuid.uuid4()
                first_cycle = first_cycle or cid
                cur.execute("INSERT INTO observation_cycles (cycle_id) VALUES (%s)", (cid,))
                cur.execute(
                    """
                    INSERT INTO snapshot_metadata_core
                        (cycle_id, snapshot_time, total_devices_denver,
                         percent_all_devices_equity)
                    VALUES (%s, %s, 1, %s)
                    """,
                    (cid, start + timedelta(minutes=minutes),
                     LIVE_PCT if d == DAY_LIVE else None),
                )
        # 20 vehicles parked since before either window, against a recorded
        # fleet of 1: fidelity 20, far outside any gate.
        start, _ = daily_sla.window_for_date(DAY_UNMEASURED)
        for i in range(20):
            lat, lon = IN_EQUITY if i < 5 else OUT_OF_EQUITY
            cur.execute(
                """
                INSERT INTO device_history
                    (vehicle_identifier, cycle_id, snapshot_time, lat, lon,
                     spatial_status, form_factor, device_id_observed)
                VALUES (%s, %s, %s, %s, %s, 'denver_core', 'scooter', %s)
                """,
                (f"{VID_PREFIX}{i}", first_cycle, start - timedelta(hours=1),
                 lat, lon, f"dev{i}"),
            )
    conn.commit()
    # The daily rows, written by the real job.
    for d in DAYS:
        daily_sla.compute_for_date(d)

    try:
        yield conn
    finally:
        conn.rollback()
        _cleanup(conn)
        conn.close()


def _sla(conn, d):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT avg_percent_all_devices_equity, compliance_equity_pass,"
            " equity_unmeasurable_reason FROM daily_sla_compliance WHERE sla_date = %s",
            (d,),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def _calendar(group="equity"):
    from fastapi import Response

    out = api_public.compliance_calendar(Response(), month="2031-03", count=1, group=group)
    return {x["date"]: x for x in out["months"][0]["days"]}


def test_the_migration_is_replayable_and_its_check_holds(pg):
    with pg.cursor() as cur:
        cur.execute(MIGRATION.read_text())   # second application: a no-op
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "UPDATE daily_sla_compliance SET equity_unmeasurable_reason = 'ghosts'"
                " WHERE sla_date = %s",
                (DAY_UNMEASURED,),
            )
    pg.rollback()


def test_a_day_whose_every_snapshot_fails_is_stored_and_served_as_unmeasurable(pg):
    assert _sla(pg, DAY_UNMEASURED) == (None, None, None)
    assert _calendar()[DAY_UNMEASURED.isoformat()]["status"] == "pending"

    r = eb.reprocess_date(DAY_UNMEASURED)
    assert r.snapshots_considered == 2
    assert r.snapshots_skipped_low_fidelity == 2
    assert r.snapshots_written == 0
    assert r.unmeasurable_reason == "low_fidelity"
    assert r.unmeasurable_recorded is True

    # Stored — and still no figure: unmeasured is not failed.
    assert _sla(pg, DAY_UNMEASURED) == (None, None, "low_fidelity")

    cal = _calendar()
    day = cal[DAY_UNMEASURED.isoformat()]
    assert day["status"] == "unmeasurable"
    assert day["percent"] is None
    assert day["snapshot_count"] == 2
    assert cal["2031-03-01"]["status"] == "no_data"

    # The live-recorded map never borrows the verdict.
    assert _calendar("v1")[DAY_UNMEASURED.isoformat()]["status"] == "pending"

    row = api_public.daily_compliance_one(date=DAY_UNMEASURED.isoformat())
    assert row["equity_unmeasurable_reason"] == "low_fidelity"
    assert row["avg_percent_all_devices_equity"] is None
    assert row["compliance_equity_pass"] is None

    # Idempotent: a second run converges on the same row.
    eb.reprocess_date(DAY_UNMEASURED)
    assert _sla(pg, DAY_UNMEASURED) == (None, None, "low_fidelity")


def test_a_live_figure_is_never_stamped_unmeasurable(pg):
    """Re-running the backfill over a live-measured day whose snapshots fail
    today's gate (every September day from 09-12 does) must leave the live
    figure — and its verdict column — alone."""
    before = _sla(pg, DAY_LIVE)
    assert float(before[0]) == LIVE_PCT and before[2] is None

    r = eb.reprocess_date(DAY_LIVE)
    assert r.unmeasurable_reason == "low_fidelity"   # what the run concluded…
    assert r.unmeasurable_recorded is False           # …and that it was refused

    after = _sla(pg, DAY_LIVE)
    assert float(after[0]) == LIVE_PCT
    assert after[1] is False
    assert after[2] is None
    assert _calendar()[DAY_LIVE.isoformat()]["status"] == "fail"


def test_daily_sla_keeps_the_verdict_until_it_has_a_figure_then_clears_it(pg):
    eb.reprocess_date(DAY_UNMEASURED)
    assert _sla(pg, DAY_UNMEASURED)[2] == "low_fidelity"

    # Re-averaging a day that is still unmeasurable must not demote it.
    daily_sla.compute_for_date(DAY_UNMEASURED)
    assert _sla(pg, DAY_UNMEASURED) == (None, None, "low_fidelity")

    # A later run that CAN measure the day supersedes the verdict.
    start, end = daily_sla.window_for_date(DAY_UNMEASURED)
    with pg.cursor() as cur:
        cur.execute(
            "UPDATE snapshot_metadata_core SET percent_all_devices_equity = 31.5"
            " WHERE snapshot_time >= %s AND snapshot_time < %s",
            (start, end),
        )
    pg.commit()
    daily_sla.compute_for_date(DAY_UNMEASURED)
    pct, passed, reason = _sla(pg, DAY_UNMEASURED)
    assert float(pct) == 31.5 and passed is True and reason is None
    assert _calendar()[DAY_UNMEASURED.isoformat()]["status"] == "pass"


def test_no_verdict_without_a_daily_row(pg):
    """A day the daily job never computed is that job's gap: the backfill
    does not invent a row to hang a verdict on."""
    with pg.cursor() as cur:
        cur.execute("DELETE FROM daily_sla_compliance WHERE sla_date = %s", (DAY_UNMEASURED,))
    pg.commit()
    r = eb.reprocess_date(DAY_UNMEASURED)
    assert r.unmeasurable_reason == "low_fidelity"
    assert r.unmeasurable_recorded is False
    assert _sla(pg, DAY_UNMEASURED) is None
    assert _calendar()[DAY_UNMEASURED.isoformat()]["status"] == "no_data"
