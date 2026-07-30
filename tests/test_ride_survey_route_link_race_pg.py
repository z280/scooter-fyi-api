"""Concurrency regression for the ride_route_id linking race (review fix,
src/api_ride_surveys.py's survey submission handler).

Two different rides' surveys naming the SAME ride_route_id must not both
be able to claim it: without a lock on the `ride_routes` row, two
concurrent transactions can both observe `tracked_ride_id IS NULL`, both
decide the route is theirs to link (and both award route-dependent
points in the caller), and then race on the UPDATE — the last writer wins
the link while the loser keeps points for a link that no longer exists.

This exercises the EXACT SQL pattern `submit_survey` now uses (`SELECT
tracked_ride_id FROM ride_routes WHERE id = %s AND account_id = %s FOR
UPDATE`, then a conditional `UPDATE ... SET tracked_ride_id`) directly
against two real connections/transactions, rather than reverse-engineering
a pause point inside the full FastAPI handler — the handler has no seam
to deterministically freeze it mid-transaction, but the locking behavior
being proven is exactly the same two statements it runs.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
"""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from src.accounts import upsert_account  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_VID = "eeee111100000000"


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture()
def dsn():
    value = os.environ.get("VEO_TEST_PG_DSN")
    if not value:
        pytest.skip("VEO_TEST_PG_DSN not set — route-link race test skipped")
    if not _reachable(value):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({value})")
    return value


@pytest.fixture()
def setup(dsn):
    """One migrated connection to seed two rides + one unlinked route."""
    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("DELETE FROM ride_routes WHERE account_id IN ("
                    "SELECT id FROM accounts WHERE email LIKE 'pgtest-routerace%@example.com')")
        cur.execute("DELETE FROM tracked_rides WHERE vehicle_identifier = %s", (_VID,))
        cur.execute("DELETE FROM accounts WHERE email LIKE 'pgtest-routerace%@example.com'")
        account_id = upsert_account(cur, f"pgtest-routerace-{uuid.uuid4()}@example.com")

        ride_a = uuid.uuid4()
        ride_b = uuid.uuid4()
        for ride_id in (ride_a, ride_b):
            cur.execute(
                """
                INSERT INTO tracked_rides (
                    id, account_id, vehicle_identifier, start_lat, start_lon,
                    watch_expires_at
                ) VALUES (%s, %s, %s, 39.74, -104.98, NOW() + interval '3 hours')
                """,
                (str(ride_id), account_id, _VID),
            )

        route_id = uuid.uuid4()
        cur.execute(
            """
            INSERT INTO ride_routes (
                id, account_id, profile, origin_lat, origin_lon, dest_lat, dest_lon,
                route_polyline, distance_meters, duration_seconds
            ) VALUES (%s, %s, 'safe', 39.74, -104.98, 39.75, -104.97, 'abc', 500, 120)
            """,
            (str(route_id), account_id),
        )
    conn.commit()
    conn.close()
    return {"account_id": account_id, "ride_a": ride_a, "ride_b": ride_b, "route_id": route_id}


def _claim(dsn, account_id, ride_id, route_id, *, hold_lock: threading.Event | None,
           proceed: threading.Event | None) -> str | None:
    """The exact two-statement pattern `submit_survey` runs (its own
    critical section, isolated). Returns the ride_id (as str) the route
    was linked to BY THIS CALL, or None if it declined (already linked to
    a different ride)."""
    conn = psycopg.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tracked_ride_id FROM ride_routes WHERE id = %s AND account_id = %s "
                "FOR UPDATE",
                (str(route_id), account_id),
            )
            (linked_ride_id,) = cur.fetchone()

            if hold_lock is not None:
                # Signal the other thread it's safe to attempt its own claim
                # (it will block on FOR UPDATE until this transaction ends),
                # then hold this transaction open a moment so the other
                # thread's blocking behavior is actually exercised.
                hold_lock.set()
                if proceed is not None:
                    proceed.wait(timeout=5)

            if linked_ride_id is not None and str(linked_ride_id) != str(ride_id):
                conn.rollback()
                return None

            cur.execute(
                "UPDATE ride_routes SET tracked_ride_id = %s WHERE id = %s",
                (str(ride_id), str(route_id)),
            )
        conn.commit()
        return str(ride_id)
    finally:
        conn.close()


def test_two_rides_racing_for_the_same_route_only_one_wins_the_link(dsn, setup):
    account_id = setup["account_id"]
    route_id = setup["route_id"]
    ride_a, ride_b = setup["ride_a"], setup["ride_b"]

    first_has_lock = threading.Event()
    let_first_finish = threading.Event()
    results: dict[str, str | None] = {}

    def run_first():
        results["a"] = _claim(
            dsn, account_id, ride_a, route_id,
            hold_lock=first_has_lock, proceed=let_first_finish,
        )

    def run_second():
        first_has_lock.wait(timeout=5)
        # This blocks on FOR UPDATE until `run_first`'s transaction ends —
        # exactly the serialization the review fix relies on.
        results["b"] = _claim(dsn, account_id, ride_b, route_id, hold_lock=None, proceed=None)

    t1 = threading.Thread(target=run_first)
    t2 = threading.Thread(target=run_second)
    t1.start()
    t2.start()
    # Give the second thread a moment to actually be blocked on the lock
    # before releasing the first — otherwise this wouldn't prove anything
    # about lock ordering.
    first_has_lock.wait(timeout=5)
    import time
    time.sleep(0.3)
    let_first_finish.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # Exactly one of the two claimed it; the other correctly declined.
    assert {results.get("a"), results.get("b")} == {str(ride_a), None}

    conn = psycopg.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tracked_ride_id FROM ride_routes WHERE id = %s", (str(route_id),))
            (linked,) = cur.fetchone()
        assert str(linked) == str(ride_a)
    finally:
        conn.close()
