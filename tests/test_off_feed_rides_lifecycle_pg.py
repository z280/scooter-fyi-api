"""Postgres-backed coverage for the off-feed ride lifecycle (sql/035) —
the parts a fake cursor can't exercise:

  - the partial unique index that allows only ONE active ride per account
  - the rides_completed_is_complete CHECK, which must accept an active
    ride with half its columns NULL and still reject an incomplete
    *completed* one
  - server-side distance measured from real waypoint rows
  - ON DELETE CASCADE from rides to off_feed_ride_waypoints, which is what
    makes DELETE a true hard delete of the whole route

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

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_NOW = datetime.now(timezone.utc)


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
        pytest.skip("VEO_TEST_PG_DSN not set — off-feed rides Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM rides")
        cur.execute("DELETE FROM accounts WHERE email LIKE 'pgtest-offfeed%@example.com'")
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
        account_id = upsert_account(cur, f"pgtest-offfeed-{uuid.uuid4()}@example.com")
    pg_conn.commit()
    user = SessionUser(
        account_id=account_id, email="pgtest-offfeed@example.com", scopes=("rider",),
        expires_at=_NOW, sliding=True, method="google", token_sha256="x",
    )
    app = FastAPI()
    app.include_router(api_rides.router)
    app.dependency_overrides[require_session] = lambda: user
    return TestClient(app), account_id


def test_full_lifecycle_measures_distance_from_waypoints(pg_conn):
    c, _ = _client(pg_conn)

    start = c.post("/api/v1/rides/start", json={
        "start_lat": 39.74, "start_lon": -104.98,
        "vehicle_kind": "scooter", "operator": "Lime",
        # Pinned rather than defaulted to the server's NOW(), so the
        # duration assertion below isn't a race against the clock.
        "started_at": _NOW.isoformat(),
    })
    assert start.status_code == 200, start.text
    ride = start.json()
    rid = ride["id"]
    # An active ride is legal with ended_at/duration/polyline all NULL —
    # the completed-is-complete CHECK must not fire here.
    assert ride["status"] == "active"
    assert ride["ended_at"] is None and ride["distance_m"] is None

    assert c.get("/api/v1/rides/active").json()["active"]["id"] == rid

    # ~100 m of latitude per step, three steps.
    step = 100.0 / 111_320.0
    for i in range(1, 4):
        r = c.post(f"/api/v1/rides/{rid}/waypoints", json={
            "waypoint_at": (_NOW + timedelta(seconds=30 * i)).isoformat(),
            "lat": 39.74 + i * step, "lon": -104.98,
        })
        assert r.status_code == 200, r.text

    wps = c.get(f"/api/v1/rides/{rid}/waypoints").json()
    assert wps["count"] == 3

    end = c.patch(f"/api/v1/rides/{rid}/end", json={
        "ended_at": (_NOW + timedelta(minutes=25)).isoformat(),
        "end_lat": 39.74 + 3 * step, "end_lon": -104.98,
        "est_cost_cents": 415,
    })
    assert end.status_code == 200, end.text
    done = end.json()
    assert done["status"] == "completed"
    assert done["distance_source"] == "waypoints"
    # Three ~100 m legs, measured along the track — not the straight line.
    assert 290 <= done["distance_m"] <= 310
    assert done["duration_s"] == 1500
    assert done["polyline"]

    # Single-shot: the end can't be re-reported.
    again = c.patch(f"/api/v1/rides/{rid}/end", json={
        "ended_at": (_NOW + timedelta(minutes=30)).isoformat(),
        "end_lat": 39.75, "end_lon": -104.99,
    })
    assert again.status_code == 409


def test_only_one_active_ride_per_account(pg_conn):
    c, _ = _client(pg_conn)
    first = c.post("/api/v1/rides/start", json={"start_lat": 39.74, "start_lon": -104.98})
    assert first.status_code == 200, first.text
    second = c.post("/api/v1/rides/start", json={"start_lat": 39.75, "start_lon": -104.99})
    assert second.status_code == 409


def test_a_completed_ride_frees_the_active_slot(pg_conn):
    c, _ = _client(pg_conn)
    rid = c.post("/api/v1/rides/start",
                 json={"start_lat": 39.74, "start_lon": -104.98}).json()["id"]
    c.patch(f"/api/v1/rides/{rid}/end", json={
        "ended_at": (_NOW + timedelta(minutes=5)).isoformat(),
        "end_lat": 39.75, "end_lon": -104.98,
    })
    again = c.post("/api/v1/rides/start", json={"start_lat": 39.76, "start_lon": -104.97})
    assert again.status_code == 200, again.text


def test_end_without_waypoints_uses_the_straight_line(pg_conn):
    c, _ = _client(pg_conn)
    rid = c.post("/api/v1/rides/start",
                 json={"start_lat": 39.74, "start_lon": -104.98}).json()["id"]
    done = c.patch(f"/api/v1/rides/{rid}/end", json={
        "ended_at": (_NOW + timedelta(minutes=5)).isoformat(),
        "end_lat": 39.75, "end_lon": -104.98,
    }).json()
    assert done["distance_source"] == "straight_line"
    assert 1100 <= done["distance_m"] <= 1120


def test_one_early_waypoint_then_an_implausible_final_leg(pg_conn):
    """The common real-world case: the phone backgrounds (or saves battery,
    or goes into a tunnel) after one fix, and the rider parks 10 km later.

    TWO RULES MEET HERE AND THEY DISAGREED. The previous commit made the
    reported end close the path, because measuring only start -> last fix
    recorded ~20 m for a 10 km ride and tagged it 'waypoints' — high
    confidence in a number off by three orders of magnitude. The operator's
    3 km leg cap says the opposite about this specific leg: a 10 km jump
    between consecutive points is not something we will measure.

    The operator's cap wins, and the result is honest rather than
    confident: measure the track we believe, drop the leg we don't, and mark
    the source partial so ~20 m is never read as a whole-path measurement.
    The one thing that must NOT happen — and does not — is the ride failing
    to complete.
    """
    c, _ = _client(pg_conn)
    step = 1.0 / 111_320.0  # metres of latitude
    rid = c.post("/api/v1/rides/start",
                 json={"start_lat": 39.74, "start_lon": -104.98}).json()["id"]
    c.post(f"/api/v1/rides/{rid}/waypoints", json={
        "waypoint_at": (_NOW + timedelta(seconds=15)).isoformat(),
        "lat": 39.74 + 20 * step, "lon": -104.98,   # one fix, 20 m along
    })
    done = c.patch(f"/api/v1/rides/{rid}/end", json={
        "ended_at": (_NOW + timedelta(minutes=40)).isoformat(),
        "end_lat": 39.74 + 10_000 * step, "end_lon": -104.98,
    }).json()
    # RECONCILED with the operator's 3 km leg cap — see the tracked-ride
    # namesake, which must behave identically because badges sum both.
    assert done["distance_source"] == "waypoints_partial"
    assert 15 <= done["distance_m"] <= 25, done["distance_m"]
    # The polyline covers the same points the distance was measured over,
    # so an export can't disagree with the badge.
    from src.polyline import decode as decode_polyline
    assert len(decode_polyline(done["polyline"])) == 2


def test_waypoint_pagination_reaches_every_page(pg_conn):
    """`before` paired with an ascending sort re-served the OLDEST rows on
    every call: page 2 was the front of page 1, and waypoints past the
    first page were unreachable. Forward paging is `after`."""
    c, _ = _client(pg_conn)
    rid = c.post("/api/v1/rides/start",
                 json={"start_lat": 39.74, "start_lon": -104.98}).json()["id"]
    for i in range(5):
        r = c.post(f"/api/v1/rides/{rid}/waypoints", json={
            "waypoint_at": (_NOW + timedelta(seconds=30 * i)).isoformat(),
            "lat": 39.74 + i * 0.001, "lon": -104.98,
        })
        assert r.status_code == 200, r.text

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):  # bounded: 5 waypoints, 2 per page
        params = {"limit": 2}
        if cursor:
            params["after"] = cursor
        page = c.get(f"/api/v1/rides/{rid}/waypoints", params=params).json()
        if not page["count"]:
            break
        seen += [w["waypoint_at"] for w in page["waypoints"]]
        cursor = page["waypoints"][-1]["waypoint_at"]
    assert len(seen) == 5, seen
    assert seen == sorted(seen), "waypoints must come back oldest-first"

    # `before` is the inverse: the last `limit` rows older than the cursor,
    # still returned oldest-first.
    back = c.get(f"/api/v1/rides/{rid}/waypoints",
                 params={"limit": 2, "before": seen[4]}).json()
    assert [w["waypoint_at"] for w in back["waypoints"]] == seen[2:4]


def test_delete_cascades_to_waypoints(pg_conn):
    """The hard-delete privacy commitment covers the whole route, not just
    the ride row."""
    c, _ = _client(pg_conn)
    rid = c.post("/api/v1/rides/start",
                 json={"start_lat": 39.74, "start_lon": -104.98}).json()["id"]
    c.post(f"/api/v1/rides/{rid}/waypoints", json={
        "waypoint_at": _NOW.isoformat(), "lat": 39.741, "lon": -104.981,
    })
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM off_feed_ride_waypoints WHERE ride_id = %s", (rid,))
        assert cur.fetchone()[0] == 1

    assert c.delete(f"/api/v1/rides/{rid}").status_code == 200
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM off_feed_ride_waypoints WHERE ride_id = %s", (rid,))
        assert cur.fetchone()[0] == 0


def test_one_shot_log_is_stored_as_client_measured(pg_conn):
    c, _ = _client(pg_conn)
    r = c.post("/api/v1/rides", json={
        "started_at": _NOW.isoformat(),
        "ended_at": (_NOW + timedelta(minutes=25)).isoformat(),
        "duration_s": 1500, "distance_m": 2412,
        "started_in_zone": True, "ended_in_zone": False,
        "polyline": "_p~iF~ps|U_ulLnnqC",
        "vehicle_kind": "bicycle", "operator": "personal",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["distance_source"] == "client"
    assert body["distance_m"] == 2412
    assert body["operator"] == "personal"
    # A one-shot log does not occupy the active slot.
    assert c.get("/api/v1/rides/active").json() == {"active": None}


# ---------------------------------------------------------------------------
# 24-hour expiry (sql/040 + src/cli.py:expire_stale_off_feed_rides)
# ---------------------------------------------------------------------------
# The whole point is the interaction with idx_rides_one_active_per_account,
# a partial UNIQUE index — which is exactly what a fake cursor can't have.

def _expire(pg_conn, monkeypatch):
    """Run the real sweep against this test's connection."""
    from contextlib import contextmanager as _cm

    from src import cli

    @_cm
    def _conn():
        yield pg_conn

    monkeypatch.setattr(cli, "connection", _conn)
    return cli.expire_stale_off_feed_rides()


def test_expiry_frees_the_active_slot(pg_conn, monkeypatch):
    """THE BUG. Before sql/040 a rider who started a ride and never ended it
    was 409'd out of POST /api/v1/rides/start permanently, because the only
    thing that could release the partial unique index was DELETE — which
    destroys the ride and its whole track."""
    c, account_id = _client(pg_conn)

    first = c.post("/api/v1/rides/start",
                   json={"start_lat": 39.74, "start_lon": -104.98})
    assert first.status_code == 200, first.text
    rid = first.json()["id"]
    c.post(f"/api/v1/rides/{rid}/waypoints", json={
        "waypoint_at": _NOW.isoformat(), "lat": 39.741, "lon": -104.981,
    })

    # Still inside the window: the slot is occupied and stays occupied.
    assert _expire(pg_conn, monkeypatch) == {"rides_expired": 0}
    blocked = c.post("/api/v1/rides/start",
                     json={"start_lat": 39.75, "start_lon": -104.99})
    assert blocked.status_code == 409
    # The UniqueViolation aborts the transaction. In production each request
    # holds its own connection and this is the pool's problem; here every
    # request shares one, so the test has to clear it.
    pg_conn.rollback()

    # Age it past 24h. created_at is server-assigned, so this is the only
    # way to simulate the passage of a day.
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE rides SET created_at = NOW() - INTERVAL '25 hours' "
                    "WHERE id = %s", (rid,))
    pg_conn.commit()

    assert _expire(pg_conn, monkeypatch) == {"rides_expired": 1}

    # The slot is free and the rider can ride again.
    assert c.get("/api/v1/rides/active").json() == {"active": None}
    again = c.post("/api/v1/rides/start",
                   json={"start_lat": 39.75, "start_lon": -104.99})
    assert again.status_code == 200, again.text
    assert again.json()["id"] != rid


def test_an_expired_ride_keeps_its_data_and_gains_no_invented_end(pg_conn, monkeypatch):
    """Expiry frees a slot; it never deletes rider data and never guesses at
    an ending. Same terminal shape as an expired tracked ride."""
    c, _ = _client(pg_conn)
    rid = c.post("/api/v1/rides/start",
                 json={"start_lat": 39.74, "start_lon": -104.98}).json()["id"]
    step = 100.0 / 111_320.0
    for i in range(1, 4):
        c.post(f"/api/v1/rides/{rid}/waypoints", json={
            "waypoint_at": (_NOW + timedelta(seconds=30 * i)).isoformat(),
            "lat": 39.74 + i * step, "lon": -104.98,
        })
    measured = c.get("/api/v1/rides", params={"status": "active"}).json()["rides"][0]
    assert measured["distance_m"] > 0

    with pg_conn.cursor() as cur:
        cur.execute("UPDATE rides SET created_at = NOW() - INTERVAL '25 hours' "
                    "WHERE id = %s", (rid,))
    pg_conn.commit()
    _expire(pg_conn, monkeypatch)

    listed = c.get("/api/v1/rides", params={"status": "expired"}).json()
    assert listed["count"] == 1
    ride = listed["rides"][0]
    assert ride["id"] == rid
    assert ride["status"] == "expired"
    # Never observed, never invented.
    assert ride["ended_at"] is None
    assert ride["duration_s"] is None
    assert ride["end_lat"] is None and ride["end_lon"] is None
    # Measured up to the last fix, left exactly as it stood.
    assert ride["distance_m"] == measured["distance_m"]
    assert ride["distance_source"] == "waypoints"

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM off_feed_ride_waypoints WHERE ride_id = %s",
                    (rid,))
        assert cur.fetchone()[0] == 3


def test_an_expired_ride_cannot_be_ended_or_appended_to(pg_conn, monkeypatch):
    c, _ = _client(pg_conn)
    rid = c.post("/api/v1/rides/start",
                 json={"start_lat": 39.74, "start_lon": -104.98}).json()["id"]
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE rides SET created_at = NOW() - INTERVAL '25 hours' "
                    "WHERE id = %s", (rid,))
    pg_conn.commit()
    _expire(pg_conn, monkeypatch)

    end = c.patch(f"/api/v1/rides/{rid}/end", json={
        "ended_at": (_NOW + timedelta(minutes=20)).isoformat(),
        "end_lat": 39.75, "end_lon": -104.99,
    })
    assert end.status_code == 409
    assert end.json()["detail"]["error"] == "ride_expired"
    pg_conn.rollback()  # releases the SELECT ... FOR UPDATE the handler took

    wp = c.post(f"/api/v1/rides/{rid}/waypoints", json={
        "waypoint_at": _NOW.isoformat(), "lat": 39.741, "lon": -104.981,
    })
    assert wp.status_code == 409
    assert wp.json()["detail"]["error"] == "ride_not_active"


def test_expiry_is_idempotent(pg_conn, monkeypatch):
    """cron re-runs this every 15 minutes."""
    c, _ = _client(pg_conn)
    rid = c.post("/api/v1/rides/start",
                 json={"start_lat": 39.74, "start_lon": -104.98}).json()["id"]
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE rides SET created_at = NOW() - INTERVAL '25 hours' "
                    "WHERE id = %s", (rid,))
    pg_conn.commit()
    assert _expire(pg_conn, monkeypatch) == {"rides_expired": 1}
    assert _expire(pg_conn, monkeypatch) == {"rides_expired": 0}


def test_an_expired_ride_earns_no_badge_mileage(pg_conn, monkeypatch):
    """src/badges.py unions `status = 'completed'` from this table against
    tracked_rides' `user_reported_ended_at IS NOT NULL`. Both drop their
    expired rows for the same reason: a ride nobody ended is not evidence
    of a distance ridden, however far its waypoints got."""
    from src.badges import _ride_badges

    c, account_id = _client(pg_conn)
    rid = c.post("/api/v1/rides/start", json={
        "start_lat": 39.74, "start_lon": -104.98,
        "started_at": _NOW.isoformat(),
    }).json()["id"]
    # ~20 km of waypoints — well past miles_10 (16 093 m) had it counted.
    step = 20_000.0 / 111_320.0
    c.post(f"/api/v1/rides/{rid}/waypoints", json={
        "waypoint_at": _NOW.isoformat(), "lat": 39.74 + step, "lon": -104.98,
    })
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE rides SET created_at = NOW() - INTERVAL '25 hours' "
                    "WHERE id = %s", (rid,))
    pg_conn.commit()
    _expire(pg_conn, monkeypatch)

    with pg_conn.cursor() as cur:
        assert _ride_badges(cur, account_id) == []


def test_status_check_accepts_expired_and_still_rejects_nonsense(pg_conn):
    """sql/040 widened the CHECK; it did not remove it."""
    import psycopg

    c, _ = _client(pg_conn)
    rid = c.post("/api/v1/rides/start",
                 json={"start_lat": 39.74, "start_lon": -104.98}).json()["id"]
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE rides SET status = 'expired' WHERE id = %s", (rid,))
    pg_conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation):
        with pg_conn.cursor() as cur:
            cur.execute("UPDATE rides SET status = 'abandoned' WHERE id = %s", (rid,))
    pg_conn.rollback()


def test_completed_is_complete_still_holds_and_does_not_bind_expired(pg_conn):
    """rides_completed_is_complete is scoped to 'completed' on purpose, so
    'expired' satisfies it with every end column NULL — while a half-filled
    *completed* ride is still refused."""
    import psycopg

    c, _ = _client(pg_conn)
    rid = c.post("/api/v1/rides/start",
                 json={"start_lat": 39.74, "start_lon": -104.98}).json()["id"]

    with pg_conn.cursor() as cur:
        cur.execute("UPDATE rides SET status = 'expired' WHERE id = %s", (rid,))
    pg_conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation):
        with pg_conn.cursor() as cur:
            cur.execute("UPDATE rides SET status = 'completed' WHERE id = %s", (rid,))
    pg_conn.rollback()
