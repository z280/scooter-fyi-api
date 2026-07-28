"""Postgres-backed coverage for the operator's hard ride caps (sql/041) —
the parts a fake cursor cannot exercise:

  - the CHECK constraints that make "no ride over 80 km" true of the DATA
    and not merely of the code paths that happen to write it
  - the migration's clamp of pre-existing history, which is the risky half:
    sql/041 runs at boot against a populated production database, and a
    constraint that fails there does not fail a migration, it fails the
    API's startup
  - replay safety, since src/pg.py re-runs every file on every boot
  - the full lifecycle under the caps, against real waypoint rows

SKIPS unless a reachable, migratable test database is provided via
VEO_TEST_PG_DSN (see tests/test_tracked_rides_lifecycle_pg.py).
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

from src import api_rides  # noqa: E402
from src.accounts import SessionUser, require_session, upsert_account  # noqa: E402
from src.ride_limits import MAX_RIDE_DISTANCE_METERS  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_NOW = datetime.now(timezone.utc)
_DEG_PER_M = 1.0 / 111_320.0
_LAT, _LON = 39.74, -104.98


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
        pytest.skip("VEO_TEST_PG_DSN not set — ride cap Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM rides")
        cur.execute("DELETE FROM accounts WHERE email LIKE 'pgtest-caps%@example.com'")
    conn.commit()

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_rides, "connection", _fake_connection)
    monkeypatch.setattr(api_rides, "enforce", lambda cur, **kw: None)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _client(pg_conn):
    with pg_conn.cursor() as cur:
        account_id = upsert_account(cur, f"pgtest-caps-{uuid.uuid4()}@example.com")
    pg_conn.commit()
    user = SessionUser(
        account_id=account_id, email="pgtest-caps@example.com", scopes=("rider",),
        expires_at=_NOW, sliding=True, method="google", token_sha256="x",
    )
    app = FastAPI()
    app.include_router(api_rides.router)
    app.dependency_overrides[require_session] = lambda: user
    return TestClient(app), account_id


def _north(meters: float) -> tuple[float, float]:
    return (_LAT + meters * _DEG_PER_M, _LON)


def _replay(pg_conn, filename: str = "041_ride_hard_caps.sql") -> None:
    with pg_conn.cursor() as cur:
        cur.execute((SQL_DIR / filename).read_text())
    pg_conn.commit()


def _start(c, at=None):
    return c.post("/api/v1/rides/start", json={
        "start_lat": _LAT, "start_lon": _LON,
        "started_at": (at or _NOW).isoformat(),
    }).json()["id"]


# ---------------------------------------------------------------------------
# The cap is true of the data, not just of the code that writes it
# ---------------------------------------------------------------------------

def test_the_check_constraint_refuses_an_over_cap_distance(pg_conn):
    c, _ = _client(pg_conn)
    rid = _start(c)
    with pytest.raises(psycopg.errors.CheckViolation):
        with pg_conn.cursor() as cur:
            cur.execute("UPDATE rides SET distance_m = 80001 WHERE id = %s", (rid,))
    pg_conn.rollback()


def test_the_check_constraint_allows_exactly_the_cap(pg_conn):
    """Off-by-one, at the schema level this time."""
    c, _ = _client(pg_conn)
    rid = _start(c)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE rides SET distance_m = 80000 WHERE id = %s", (rid,))
    pg_conn.commit()


def test_the_check_constraint_allows_null_for_an_unfinished_ride(pg_conn):
    """An active ride has no distance yet — the cap is a statement about
    recorded distances, not a requirement that one exist."""
    c, _ = _client(pg_conn)
    rid = _start(c)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT distance_m FROM rides WHERE id = %s", (rid,))
        assert cur.fetchone()[0] is None


def test_tracked_rides_carries_the_same_constraint(pg_conn):
    """src/badges.py sums both tables, so a cap on one of them is not a cap."""
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
             WHERE conname = 'tracked_rides_distance_within_cap'
               AND conrelid = 'tracked_rides'::regclass
            """
        )
        row = cur.fetchone()
    assert row is not None, "tracked_rides has no distance cap"
    assert "80000" in row[0]


def test_the_sql_cap_matches_the_python_constant(pg_conn):
    """A CHECK cannot read src/ride_limits.py, so 80000 is written twice.
    This is the test the migration's closing note points at: the two copies
    cannot drift without a failure here."""
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
             WHERE conname = 'rides_distance_within_cap'
               AND conrelid = 'rides'::regclass
            """
        )
        definition = cur.fetchone()[0]
    assert str(int(MAX_RIDE_DISTANCE_METERS)) in definition


def test_distance_source_accepts_the_partial_marker(pg_conn):
    c, _ = _client(pg_conn)
    rid = _start(c)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE rides SET distance_source = 'waypoints_partial' "
                    "WHERE id = %s", (rid,))
    pg_conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation):
        with pg_conn.cursor() as cur:
            cur.execute("UPDATE rides SET distance_source = 'vibes' WHERE id = %s",
                        (rid,))
    pg_conn.rollback()


# ---------------------------------------------------------------------------
# THE RISKY HALF: what sql/041 does to history that already breaks the cap
# ---------------------------------------------------------------------------

def _make_over_cap_row(pg_conn, rid: str, distance: int) -> None:
    """Recreate the pre-migration state: a stored ride above the cap.

    The constraint has to come off first, which is exactly the situation
    sql/041 meets on the production database — rows that predate it.
    """
    with pg_conn.cursor() as cur:
        cur.execute("ALTER TABLE rides DROP CONSTRAINT rides_distance_within_cap")
        cur.execute(
            "UPDATE rides SET distance_m = %s, distance_clamped_from_m = NULL "
            "WHERE id = %s", (distance, rid))
    pg_conn.commit()


def test_the_migration_clamps_pre_existing_over_cap_history(pg_conn):
    """The decision recorded in sql/041's header: existing violations are
    CLAMPED, not left alone. Leaving them would mean the invariant is false
    for exactly the rows badges.py is summing."""
    c, _ = _client(pg_conn)
    rid = _start(c)
    _make_over_cap_row(pg_conn, rid, 412_883)

    _replay(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT distance_m, distance_clamped_from_m FROM rides "
                    "WHERE id = %s", (rid,))
        distance, clamped_from = cur.fetchone()
    assert distance == 80_000, "history above the cap survived the migration"
    assert clamped_from == 412_883, "what we measured before clamping was lost"


def test_the_migration_does_not_touch_rows_inside_the_cap(pg_conn):
    """The overwhelming majority of rows. They must come out bit-identical
    and must NOT acquire a clamp record."""
    c, _ = _client(pg_conn)
    rid = _start(c)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE rides SET distance_m = 5000 WHERE id = %s", (rid,))
    pg_conn.commit()

    _replay(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT distance_m, distance_clamped_from_m FROM rides "
                    "WHERE id = %s", (rid,))
        assert cur.fetchone() == (5000, None)


def test_replaying_the_migration_never_reclamps(pg_conn):
    """src/pg.py re-runs every file on every boot. A second pass must not
    rewrite distance_clamped_from_m to 80 000 — that would erase the
    original measurement and replace it with the cap, which is the one
    thing the COALESCE in sql/041 exists to prevent."""
    c, _ = _client(pg_conn)
    rid = _start(c)
    _make_over_cap_row(pg_conn, rid, 250_000)

    _replay(pg_conn)
    _replay(pg_conn)
    _replay(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT distance_m, distance_clamped_from_m FROM rides "
                    "WHERE id = %s", (rid,))
        assert cur.fetchone() == (80_000, 250_000)


def test_the_migration_replays_clean_on_an_untouched_database(pg_conn):
    """No rows to clamp, every constraint already present: the whole file
    must still be a no-op rather than erroring on a duplicate constraint."""
    _replay(pg_conn)
    _replay(pg_conn)


def test_the_expiry_sweep_still_works_on_a_clamped_row(pg_conn, monkeypatch):
    """Why the constraint is VALIDATED rather than NOT VALID. A NOT VALID
    CHECK is still enforced on UPDATE, so an over-cap legacy row would have
    made the sql/040 sweep throw CheckViolation in production — an
    unrelated code path, failing long after anyone would connect it here."""
    from src import cli

    c, _ = _client(pg_conn)
    rid = _start(c)
    _make_over_cap_row(pg_conn, rid, 500_000)
    _replay(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute("UPDATE rides SET created_at = NOW() - INTERVAL '25 hours' "
                    "WHERE id = %s", (rid,))
    pg_conn.commit()

    @contextmanager
    def _conn():
        yield pg_conn

    monkeypatch.setattr(cli, "connection", _conn)
    assert cli.expire_stale_off_feed_rides() == {"rides_expired": 1}


# ---------------------------------------------------------------------------
# The lifecycle, against real waypoint rows
# ---------------------------------------------------------------------------

def test_a_waypoint_over_three_km_out_is_refused_and_not_stored(pg_conn):
    c, _ = _client(pg_conn)
    rid = _start(c)
    r = c.post(f"/api/v1/rides/{rid}/waypoints", json={
        "waypoint_at": _NOW.isoformat(), "lat": _north(5_000)[0], "lon": _LON,
    })
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "waypoint_too_far"
    pg_conn.rollback()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM off_feed_ride_waypoints WHERE ride_id = %s",
                    (rid,))
        assert cur.fetchone()[0] == 0


def test_the_ride_survives_a_refused_waypoint(pg_conn):
    """The whole reason for rejecting at append: the rider loses one fix,
    not the ride."""
    c, _ = _client(pg_conn)
    rid = _start(c)
    assert c.post(f"/api/v1/rides/{rid}/waypoints", json={
        "waypoint_at": _NOW.isoformat(), "lat": _north(9_000)[0], "lon": _LON,
    }).status_code == 422
    pg_conn.rollback()

    good = c.post(f"/api/v1/rides/{rid}/waypoints", json={
        "waypoint_at": (_NOW + timedelta(seconds=30)).isoformat(),
        "lat": _north(500)[0], "lon": _LON,
    })
    assert good.status_code == 200, good.text
    assert c.get("/api/v1/rides/active").json()["active"]["id"] == rid


def test_an_implausible_final_leg_still_completes_the_ride(pg_conn):
    """NEVER STRAND THE RIDE. The end report is not refusable — the ride
    completes, the disbelieved leg is left out, and distance_source says
    the path is partial."""
    c, _ = _client(pg_conn)
    rid = _start(c)
    c.post(f"/api/v1/rides/{rid}/waypoints", json={
        "waypoint_at": (_NOW + timedelta(seconds=30)).isoformat(),
        "lat": _north(400)[0], "lon": _LON,
    })

    end = c.patch(f"/api/v1/rides/{rid}/end", json={
        "ended_at": (_NOW + timedelta(minutes=20)).isoformat(),
        "end_lat": _north(60_000)[0], "end_lon": _LON,
    })
    assert end.status_code == 200, end.text
    done = end.json()
    assert done["status"] == "completed"
    assert done["distance_source"] == "waypoints_partial"
    assert 350 < done["distance_m"] < 450, "the 60 km jump was measured"
    # The rider's reported end is still recorded — we keep their report and
    # simply decline to measure a leg we don't believe.
    assert done["end_lat"] == pytest.approx(_north(60_000)[0])
    # And the slot is free.
    assert c.get("/api/v1/rides/active").json() == {"active": None}


def test_a_trackless_ride_over_the_cap_completes_clamped(pg_conn):
    """No waypoints, so start -> end is the whole ride and the leg cap does
    not apply. The ride cap does, and the row records that it bound."""
    c, _ = _client(pg_conn)
    rid = _start(c)
    end = c.patch(f"/api/v1/rides/{rid}/end", json={
        "ended_at": (_NOW + timedelta(hours=6)).isoformat(),
        "end_lat": _north(300_000)[0], "end_lon": _LON,
    })
    assert end.status_code == 200, end.text
    done = end.json()
    assert done["distance_m"] == 80_000
    assert done["distance_source"] == "straight_line"
    assert done["distance_clamped_from_m"] > 290_000


def test_an_ordinary_ride_is_completely_unaffected(pg_conn):
    """The regression that matters most: none of this may change what a
    normal ride records."""
    c, _ = _client(pg_conn)
    rid = _start(c)
    for i in range(1, 4):
        r = c.post(f"/api/v1/rides/{rid}/waypoints", json={
            "waypoint_at": (_NOW + timedelta(seconds=30 * i)).isoformat(),
            "lat": _north(100 * i)[0], "lon": _LON,
        })
        assert r.status_code == 200, r.text

    done = c.patch(f"/api/v1/rides/{rid}/end", json={
        "ended_at": (_NOW + timedelta(minutes=25)).isoformat(),
        "end_lat": _north(400)[0], "end_lon": _LON,
    }).json()
    assert done["distance_source"] == "waypoints"
    assert 390 < done["distance_m"] < 410
    assert done["distance_clamped_from_m"] is None


def test_badges_read_the_clamped_number_not_the_measured_one(pg_conn):
    """The cap has to bind where it matters: badges.py sums distance_m, so
    a clamped ride must contribute the cap, not what it claimed."""
    from src.badges import _ride_badges

    c, account_id = _client(pg_conn)
    rid = _start(c)
    c.patch(f"/api/v1/rides/{rid}/end", json={
        "ended_at": (_NOW + timedelta(hours=6)).isoformat(),
        "end_lat": _north(300_000)[0], "end_lon": _LON,
    })
    with pg_conn.cursor() as cur:
        badges = {b["id"] for b in _ride_badges(cur, account_id)}
    # 80 km clears miles_10 (16 093 m) but NOT miles_100 (160 934 m), which
    # 300 km would have.
    assert "miles_10" in badges
    assert "miles_100" not in badges
