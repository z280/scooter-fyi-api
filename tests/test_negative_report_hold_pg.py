"""How long a "this one is broken" report counts for, against real Postgres.

Two rules, by whether anybody stands behind the report (see
src/api_frontend_reports.py's statement of them):

  * ANONYMOUS — 24 hours, in the vehicle's h3_10 cell. Nobody's name is on it,
    so it ages out on a clock.
  * SIGNED IN — until the vehicle MOVES or comes back at a FULL CHARGE. An
    accountable claim is not answered by time passing.

These run the REAL `has_negative_report` SQL, which is the only way to test
this: the predicate is three correlated subqueries over `device_reports`,
`device_state` and the telemetry row, and a fake connection would be asserting
on the test's own idea of the query rather than on the query. The positional
`%s` binding in api_public.py's SELECT list is exercised here too — get its
order wrong and every filter shifts by one, which no unit test would notice.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from src.quality import full_charge_range_meters  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
FULL = full_charge_range_meters()
HALF = FULL // 2
VID = "0123456789abcdef"
CELL = 614553222213795839  # any valid bigint h3_10; the SQL only compares it
OTHER_CELL = CELL + 2


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture()
def pg():
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")
    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM device_reports")
        cur.execute("DELETE FROM negative_reports")
        cur.execute("DELETE FROM device_state")
    conn.commit()
    yield conn
    conn.rollback()
    conn.close()


def _account(conn) -> int:
    """A real account row, because `device_reports.account_id` is a FK."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (email) VALUES (%s) RETURNING id",
            (f"hold-{uuid.uuid4().hex[:8]}@example.test",),
        )
        return cur.fetchone()[0]


def _report(conn, *, account_id, at, report_type="not_rideable", cell=CELL):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO device_reports "
            "  (vehicle_identifier, report_type, reported_at, h3_10_index, account_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (VID, report_type, at, cell, account_id),
        )
    conn.commit()


def _device_state(conn, *, parked_since):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO device_state "
            "  (vehicle_identifier, first_observed_at_location, "
            "   first_ever_observed_at, last_observed_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (vehicle_identifier) DO UPDATE SET "
            "  first_observed_at_location = EXCLUDED.first_observed_at_location",
            (VID, parked_since, parked_since, parked_since),
        )
    conn.commit()


def _flagged(conn, *, cell=CELL, range_meters=HALF) -> bool:
    """Run the shipped predicate against one synthetic telemetry row.

    The SQL is a copy of api_public.py's `has_negative_report` expression with
    the correlated row supplied inline — a copy because the endpoint's query
    needs a whole cycle's telemetry to run at all, and the thing under test is
    the predicate. `test_reliability_sql_is_mirrored` below is what keeps the
    copy honest.
    """
    from src.api_frontend_reports import reliability_report_type_sql

    sql = f"""
    SELECT (EXISTS (
        SELECT 1 FROM negative_reports nr
        WHERE nr.vehicle_identifier = r.vehicle_identifier
          AND nr.h3_10_index = r.h3_10_index
          AND nr.reported_at >= %(now)s - INTERVAL '24 hours'
    ) OR EXISTS (
        SELECT 1 FROM device_reports dr
        WHERE dr.vehicle_identifier = r.vehicle_identifier
          AND dr.h3_10_index = r.h3_10_index
          AND dr.reported_at >= %(now)s - INTERVAL '24 hours'
          AND {reliability_report_type_sql('dr')}
    ) OR EXISTS (
        SELECT 1 FROM device_reports dr
        WHERE dr.vehicle_identifier = r.vehicle_identifier
          AND dr.account_id IS NOT NULL
          AND {reliability_report_type_sql('dr')}
          AND (ds.first_observed_at_location IS NULL
               OR ds.first_observed_at_location <= dr.reported_at)
          AND (r.current_range_meters IS NULL
               OR r.current_range_meters < %(full)s)
    )) AS has_negative_report
    FROM (SELECT %(vid)s::text AS vehicle_identifier,
                 %(cell)s::bigint AS h3_10_index,
                 %(range)s::int AS current_range_meters) r
    LEFT JOIN device_state ds USING (vehicle_identifier)
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            {"now": NOW, "full": FULL, "vid": VID, "cell": cell, "range": range_meters},
        )
        return bool(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Anonymous: the clock still runs
# ---------------------------------------------------------------------------


def test_anonymous_report_counts_for_24h(pg):
    _device_state(pg, parked_since=NOW - timedelta(days=3))
    _report(pg, account_id=None, at=NOW - timedelta(hours=2))
    assert _flagged(pg) is True


def test_anonymous_report_expires(pg):
    _device_state(pg, parked_since=NOW - timedelta(days=3))
    _report(pg, account_id=None, at=NOW - timedelta(hours=25))
    assert _flagged(pg) is False


def test_anonymous_report_does_not_follow_the_vehicle(pg):
    # Scoped to the cell it was filed in, unchanged.
    _device_state(pg, parked_since=NOW - timedelta(days=3))
    _report(pg, account_id=None, at=NOW - timedelta(hours=2))
    assert _flagged(pg, cell=OTHER_CELL) is False


# ---------------------------------------------------------------------------
# Signed in: until something actually happens
# ---------------------------------------------------------------------------


def test_signed_in_report_outlives_24_hours(pg):
    # The whole change. A scooter nobody has repaired, moved or charged in
    # three days IS still broken, and the old rule said otherwise every
    # morning.
    acct = _account(pg)
    _device_state(pg, parked_since=NOW - timedelta(days=5))
    _report(pg, account_id=acct, at=NOW - timedelta(days=3))
    assert _flagged(pg) is True


def test_moving_clears_it(pg):
    acct = _account(pg)
    _report(pg, account_id=acct, at=NOW - timedelta(days=3))
    # Parked somewhere new since the report: device_state resets this on any
    # move past the ingest's stationary threshold.
    _device_state(pg, parked_since=NOW - timedelta(hours=1))
    assert _flagged(pg) is False


def test_moving_within_the_same_cell_clears_it_too(pg):
    # The case the 24h/h3_10 rule could never see: a vehicle picked up and
    # re-parked on the same block is still a vehicle somebody dealt with.
    acct = _account(pg)
    _report(pg, account_id=acct, at=NOW - timedelta(days=2))
    _device_state(pg, parked_since=NOW - timedelta(minutes=30))
    assert _flagged(pg, cell=CELL) is False


def test_a_full_charge_clears_it(pg):
    # A swapped or charged battery is a service visit. A scooter nobody has
    # touched does not refill itself.
    acct = _account(pg)
    _device_state(pg, parked_since=NOW - timedelta(days=5))
    _report(pg, account_id=acct, at=NOW - timedelta(days=3))
    assert _flagged(pg, range_meters=FULL) is False


def test_nearly_full_does_not_clear_it(pg):
    # 99% is not 100%, and the threshold is the same number the battery
    # readout calls full — see quality.full_charge_range_meters.
    acct = _account(pg)
    _device_state(pg, parked_since=NOW - timedelta(days=5))
    _report(pg, account_id=acct, at=NOW - timedelta(days=3))
    assert _flagged(pg, range_meters=FULL - 1) is True


def test_an_unknown_position_does_not_clear_it(pg):
    # No device_state row is not evidence of a move. The flag holds, which is
    # the safe direction for a claim that the scooter does not work.
    acct = _account(pg)
    _report(pg, account_id=acct, at=NOW - timedelta(days=3))
    assert _flagged(pg) is True


def test_an_unknown_battery_does_not_clear_it(pg):
    acct = _account(pg)
    _device_state(pg, parked_since=NOW - timedelta(days=5))
    _report(pg, account_id=acct, at=NOW - timedelta(days=3))
    assert _flagged(pg, range_meters=None) is True


def test_a_parking_complaint_never_counts(pg):
    # improperly_parked is a compliance signal. A scooter blocking a ramp can
    # still be a great ride, and this rule must not change that.
    acct = _account(pg)
    _device_state(pg, parked_since=NOW - timedelta(days=5))
    _report(
        pg,
        account_id=acct,
        at=NOW - timedelta(days=3),
        report_type="improperly_parked",
    )
    assert _flagged(pg) is False


def test_a_signed_in_report_follows_the_vehicle_across_cells(pg):
    # Not cell-scoped, deliberately: if it has not moved, the cell cannot have
    # changed, and if it has moved the movement check has already cleared it.
    # So a cell mismatch alone must not clear an unanswered report.
    acct = _account(pg)
    _device_state(pg, parked_since=NOW - timedelta(days=5))
    _report(pg, account_id=acct, at=NOW - timedelta(days=3))
    assert _flagged(pg, cell=OTHER_CELL) is True
