"""The sql/ set must be replayable — every file, in order, against a
database that already has data in it.

src/pg.py records applied filenames so production only ever runs each file
once, but that guarantee is exactly what hides a broken file: a migration
set that cannot be replayed cannot build a database from scratch, and the
_pg test fixtures in this suite DO execute every sql/*.sql on every run,
so a non-replayable file breaks them for everyone.

The concrete failure this file pins: sql/029 used to drop
device_reports_report_type_allowed unconditionally and re-add it with the
value list as it stood when it was written — which does not contain
'not_rideable'. Replaying it against a database holding a single
not_rideable report died with a CheckViolation, taking every other
Postgres test with it.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from src.accounts import upsert_account  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_VID = "cccc000000000000"


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
def pg_conn():
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — migration replay test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    _apply_all(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM device_reports WHERE vehicle_identifier = %s", (_VID,))
        cur.execute("DELETE FROM accounts WHERE email LIKE 'pgtest-replay%@example.com'")
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def test_replaying_every_migration_is_safe_with_a_not_rideable_row(pg_conn):
    """The exact production shape: a rider files a not_rideable report,
    then someone rebuilds/redeploys and the whole sql/ set runs again."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO device_reports (vehicle_identifier, report_type) "
            "VALUES (%s, 'not_rideable')",
            (_VID,),
        )
    pg_conn.commit()

    _apply_all(pg_conn)   # would raise CheckViolation before the sql/029 fix
    _apply_all(pg_conn)   # and again — replay must be repeatable, not once-more

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM device_reports "
            "WHERE vehicle_identifier = %s AND report_type = 'not_rideable'",
            (_VID,),
        )
        assert cur.fetchone()[0] == 1, "the replay must not have destroyed data"


def test_the_replayed_constraint_still_permits_every_current_type(pg_conn):
    """A replay that silently reinstated an older value list would only
    surface on the next rider report. Insert one of each."""
    from src.api_frontend_reports import _REPORT_TYPES

    _apply_all(pg_conn)
    with pg_conn.cursor() as cur:
        for report_type in _REPORT_TYPES:
            cur.execute(
                "INSERT INTO device_reports (vehicle_identifier, report_type) "
                "VALUES (%s, %s)",
                (_VID, report_type),
            )
    pg_conn.commit()


def test_the_replayed_constraint_still_rejects_the_deprecated_spelling(pg_conn):
    """The API accepts 'failed_unlock' as a deprecated alias, but
    normalises it before storage — the constraint is the backstop proving
    the alias never reaches a column."""
    _apply_all(pg_conn)
    with pg_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO device_reports (vehicle_identifier, report_type) "
                "VALUES (%s, 'failed_unlock')",
                (_VID,),
            )
    pg_conn.rollback()


def test_distance_backfill_fills_rides_that_ended_before_sql_034(pg_conn):
    """sql/034 added distance_meters without backfilling, so every ride
    completed before it ran reads as 0 m to src/badges.py while its date
    still feeds the streak set. sql/039 closes that."""
    now = datetime.now(timezone.utc)
    with pg_conn.cursor() as cur:
        account_id = upsert_account(cur, f"pgtest-replay-{uuid.uuid4()}@example.com")
        # A ride in the pre-034 shape: ended, with no distance.
        cur.execute(
            """
            INSERT INTO tracked_rides (
                account_id, vehicle_identifier, start_lat, start_lon,
                watch_expires_at, user_reported_ended_at, end_lat, end_lon,
                status, distance_meters, distance_source
            ) VALUES (%s, %s, 39.74, -104.98, %s, %s, 39.75, -104.98,
                      'completed', NULL, NULL)
            RETURNING id
            """,
            (account_id, _VID, now, now - timedelta(days=1)),
        )
        legacy_id = cur.fetchone()[0]
        # A ride that never ended must stay NULL — there is nothing to
        # measure to, and a 0 would be indistinguishable from a real one.
        cur.execute(
            """
            INSERT INTO tracked_rides (
                account_id, vehicle_identifier, start_lat, start_lon,
                watch_expires_at
            ) VALUES (%s, %s, 39.74, -104.98, %s)
            RETURNING id
            """,
            (account_id, _VID, now + timedelta(hours=3)),
        )
        unfinished_id = cur.fetchone()[0]
    pg_conn.commit()

    _apply_all(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT distance_meters, distance_source FROM tracked_rides WHERE id = %s",
            (legacy_id,),
        )
        distance, source = cur.fetchone()
        # 39.74 -> 39.75 at constant longitude is ~1113 m.
        assert 1100 < distance < 1120, distance
        assert source == "straight_line", "provenance must not overclaim"

        cur.execute(
            "SELECT distance_meters, distance_source FROM tracked_rides WHERE id = %s",
            (unfinished_id,),
        )
        assert cur.fetchone() == (None, None)


def test_the_backfill_never_overwrites_a_measured_distance(pg_conn):
    """Replay safety for sql/039 itself: `distance_meters IS NULL` is its
    whole idempotency guard, so a good waypoint-measured number must
    survive any number of replays."""
    now = datetime.now(timezone.utc)
    with pg_conn.cursor() as cur:
        account_id = upsert_account(cur, f"pgtest-replay-{uuid.uuid4()}@example.com")
        cur.execute(
            """
            INSERT INTO tracked_rides (
                account_id, vehicle_identifier, start_lat, start_lon,
                watch_expires_at, user_reported_ended_at, end_lat, end_lon,
                status, distance_meters, distance_source
            ) VALUES (%s, %s, 39.74, -104.98, %s, %s, 39.75, -104.98,
                      'completed', 4321.0, 'waypoints')
            RETURNING id
            """,
            (account_id, _VID, now, now - timedelta(days=1)),
        )
        ride_id = cur.fetchone()[0]
    pg_conn.commit()

    _apply_all(pg_conn)
    _apply_all(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT distance_meters, distance_source FROM tracked_rides WHERE id = %s",
            (ride_id,),
        )
        assert cur.fetchone() == (4321.0, "waypoints")
