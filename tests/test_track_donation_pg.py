"""Full lifecycle of POST /api/v1/tracked-rides/{ride_id}/track against a
real Postgres — the donation endpoint (PLAN_RIDE_MODE_API.md phase A2)
that composes all four A2 lanes' work in one transaction:

    src.track_verify.verify_track_chain   (verification)
    sql/051 track_donations/donated_track_points   (persistence)
    src.points.credit_battery_contribution/credit_nav_distance_bonus (points)
    src.battery_model.ingest_donated_observation   (battery feedback loop)

No lane owned this composition alone, so it has no fake-cursor coverage
anywhere else — this is the one place the full pipeline is exercised
end-to-end, including the pending_feed -> late-eligible settle path
(src/ride_watch.py:finalize_validation), against real SQL (guarded
constraints, partial unique indexes, real transactions/locks) rather than
a canned fetch sequence.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable Postgres —
same pattern as tests/test_tracked_rides_lifecycle_pg.py.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from src import api_tracked_rides, ride_watch  # noqa: E402
from src.accounts import SessionUser, require_session, upsert_account  # noqa: E402
from src.ingest import TaggedDevice  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
VID = "ccccdddd00000000"
_METERS_PER_DEG_LAT = 111_320.0
# A real data/range_soc_lut.json rank -> exactly 51% (same value
# tests/test_ride_session_fields.py uses, so this is the production LUT,
# not a stub).
_RANGE_METERS_AT_51_PERCENT = 20708


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
        pytest.skip("VEO_TEST_PG_DSN not set — track donation Postgres integration test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)

    # WIPE user_points BEFORE replaying migrations, not after — load-bearing
    # ordering, not tidiness. sql/037's rewrite of user_points_action_allowed
    # is a plain, UNGUARDED DROP/re-ADD (unlike sql/029's later-fixed twin
    # for device_reports, or sql/053's own guarded widening — see sql/053's
    # own header comment, which flags sql/037's shape without fixing it,
    # since in a REAL deployment sql/037 only ever runs once, before
    # sql/053 exists, against an empty table). This test file is the first
    # one in the suite to ever write a `battery_contribution` row (or any
    # of sql/053's other four new actions) — and every _pg fixture in this
    # suite (this file's own included) replays the WHOLE sql/ directory on
    # EVERY test. If a prior test committed such a row and this fixture
    # replayed migrations before clearing it, sql/037's ADD CONSTRAINT would
    # 500 on a CheckViolation before sql/053 ever gets a chance to re-widen
    # it back — exactly the failure class tests/test_migration_replay_pg.py
    # exists to catch (its own docstring: "a migration set that cannot be
    # replayed cannot build a database from scratch"), just not yet pinned
    # there for THIS constraint. Guarded against UndefinedTable for a truly
    # fresh cluster where user_points doesn't exist yet.
    with conn.cursor() as cur:
        try:
            cur.execute("DELETE FROM user_points")
        except psycopg.errors.UndefinedTable:
            conn.rollback()
    conn.commit()

    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()

    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM tracked_rides")
        cur.execute("DELETE FROM device_state WHERE vehicle_identifier = %s", (VID,))
        # No feed observation seeded for this vehicle -- start_and_walk's
        # tests rely on the check-5 fallback anchor (start_lat/lon, not
        # feed_start_lat/lon). A dedicated VID (unlike the shared
        # "aaaa.../bbbb..." ones other _pg files use) avoids collisions,
        # but clean up defensively anyway in case a prior run left rows.
        cur.execute("DELETE FROM raw_telemetry_points WHERE vehicle_identifier = %s", (VID,))
        cur.execute("DELETE FROM battery_trip_observations WHERE vehicle_identifier = %s", (VID,))
        cur.execute("DELETE FROM accounts WHERE email LIKE 'pgtest-donation%@example.com'")
        cur.execute(
            """
            INSERT INTO device_state (
                vehicle_identifier, vehicle_plate, current_lat, current_lon,
                first_observed_at_location, first_ever_observed_at, last_observed_at,
                current_vehicle_model_name
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (VID, "9998887", 39.74, -104.98, now, now, now, "Cosmo"),
        )
    conn.commit()

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_tracked_rides, "connection", _fake_connection)
    monkeypatch.setattr(ride_watch, "connection", _fake_connection)
    monkeypatch.setattr(api_tracked_rides, "enforce", lambda cur, **kw: None)
    try:
        yield conn
    finally:
        # Wipe user_points again on the way OUT, not just on the way in:
        # this test's own donations commit `battery_contribution` rows
        # mid-test (the handler calls conn.commit() itself), so the
        # rollback below cannot undo them — and leaving them behind would
        # trip the SAME sql/037 replay hazard the very next time ANY
        # _pg fixture in the whole suite (not just this file's) replays
        # migrations, not only this file's own next test.
        try:
            conn.rollback()  # clear anything left uncommitted by a failed test
            with conn.cursor() as cur:
                cur.execute("DELETE FROM user_points")
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
        conn.close()


def _seed_cycle(conn) -> uuid.UUID:
    """Mirrors cycle.py:_start_cycle()'s INSERT — the FK
    gbfs_left_feed_cycle_id/gbfs_reappeared_cycle_id depend on a real,
    committed observation_cycles row (same helper as
    tests/test_tracked_rides_lifecycle_pg.py)."""
    cid = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO observation_cycles (cycle_id, start_ts, job_status) VALUES (%s, %s, 'in_progress')",
            (str(cid), datetime.now(timezone.utc)),
        )
    conn.commit()
    return cid


def _client(pg_conn):
    with pg_conn.cursor() as cur:
        account_id = upsert_account(cur, f"pgtest-donation-{uuid.uuid4()}@example.com")
    pg_conn.commit()
    user = SessionUser(
        account_id=account_id, email="pgtest-donation@example.com", scopes=("rider",),
        expires_at=datetime.now(timezone.utc),
        sliding=True, method="google", token_sha256="x",
    )
    app = FastAPI()
    app.include_router(api_tracked_rides.router)
    app.dependency_overrides[require_session] = lambda: user
    return TestClient(app), account_id


# ---------------------------------------------------------------------------
# Chain builder (deliberately independent of src/track_verify.py — the same
# rule tests/test_track_verify.py's own builder follows).
# ---------------------------------------------------------------------------

def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _walk_north(start_lat, start_lon, t0_ms, *, n_segments, dt_ms, meters_per_segment, acc=5):
    """(fixes, t1_ms) — fixes is [(abs_ms, lat, lon, acc), ...], n_segments+1
    points total, moving due north (constant longitude) so distance is
    predictable from _METERS_PER_DEG_LAT."""
    lat, lon, t = start_lat, start_lon, t0_ms
    fixes = [(t, lat, lon, acc)]
    for _ in range(n_segments):
        lat += meters_per_segment / _METERS_PER_DEG_LAT
        t += dt_ms
        fixes.append((t, lat, lon, acc))
    return fixes, t


def _seal_chain(fixes, *, ride_id, nonce_hex, key_b64, tamper=None):
    """One batch per fix-pair-free chunking: every fix in its own seq —
    simplest to reason about, and CHECK_KEYS' chain check doesn't care how
    batches are chunked (batch sealing size/span is a client convention,
    not a server rule). `tamper`, if given, is called with the fully-built
    list of batch dicts (header, payload) before signing, for negative
    tests (wrong kid/rid/non, flipped seq, etc)."""
    key = base64.urlsafe_b64decode(key_b64 + "=" * (-len(key_b64) % 4))
    prev_hex = ""
    batches = []
    # One batch covering the whole walk (t0/t1 span the full chain) is the
    # simplest legal encoding and keeps this helper small; nothing in
    # checks 1-6 requires multiple batches.
    t0, t1 = fixes[0][0], fixes[-1][0]
    pts = [[f[0] - t0, round(f[1], 6), round(f[2], 6), int(round(f[3]))] for f in fixes]
    header = {"alg": "HS256", "typ": "sfyi-track+jws", "kid": ride_id}
    payload = {"v": 1, "rid": ride_id, "non": nonce_hex, "seq": 0, "prev": prev_hex,
               "t0": t0, "t1": t1, "pts": pts, "rec": False}
    if tamper is not None:
        header, payload = tamper(header, payload)
    h_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    p_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(key, f"{h_b64}.{p_b64}".encode("ascii"), hashlib.sha256).digest()
    batches.append(f"{h_b64}.{p_b64}.{_b64url_encode(sig)}")
    return batches


def _start_and_walk(client, *, reported_start_battery=82.0, ride_options=None):
    """POST /tracked-rides, then a 13-point ~2.9 km / 12 min walk starting
    5 s after track_key_issued_at (comfortably inside the 120 s bounds
    slack). Returns (ride, fixes, t1_ms)."""
    ride_options = ride_options if ride_options is not None else {
        "save_tracks": True, "battery_modeling": True, "nav_improvement": True,
    }
    r = client.post("/api/v1/tracked-rides", json={
        "vehicle_identifier": VID, "start_lat": 39.74, "start_lon": -104.98,
        "reported_start_battery_percent": reported_start_battery,
        "ride_options": ride_options,
    })
    assert r.status_code == 200, r.text
    ride = r.json()
    issued_at = datetime.fromisoformat(ride["track_signing"]["issued_at"])
    t0_ms = int(issued_at.timestamp() * 1000) + 5_000
    fixes, t1_ms = _walk_north(
        39.74, -104.98, t0_ms, n_segments=12, dt_ms=60_000, meters_per_segment=240.0,
    )
    return ride, fixes, t1_ms


def _end_ride(client, ride_id, t1_ms, *, reported_battery_percent=61.0):
    ended_at = datetime.fromtimestamp(t1_ms / 1000, tz=timezone.utc) + timedelta(seconds=30)
    r = client.patch(f"/api/v1/tracked-rides/{ride_id}/end", json={
        "ended_at": ended_at.isoformat(),
        "end_lat": 39.74 + (12 * 240.0) / _METERS_PER_DEG_LAT, "end_lon": -104.98,
        "reported_battery_percent": reported_battery_percent,
    })
    assert r.status_code == 200, r.text
    return r.json()


def _left_feed_cycle(pg_conn, ride_id):
    """cycle1: the vehicle absent -> 'watching' -> 'left_feed', with
    gbfs_left_feed_at close enough to track_key_issued_at that check 5's
    start correlation never trips."""
    cycle = _seed_cycle(pg_conn)
    stats = ride_watch.update_watches_for_cycle(cycle, datetime.now(timezone.utc), devices=[])
    assert stats.newly_left_feed == 1
    return stats


def _reappear_cycle(pg_conn, *, at_ms, lat, lon):
    """cycle2: the vehicle reappears at (lat, lon) at `at_ms` -> 'left_feed'
    -> 'resolved', stamping gbfs_reappeared_at/gbfs_end_lat/gbfs_end_lon —
    and (src/ride_watch.py's own wiring) automatically invokes
    finalize_validation for the newly-reappeared ride."""
    dev = TaggedDevice(
        device_id="bike-9", vehicle_type_id="3", form_factor="scooter",
        lat=lat, lon=lon, spatial_status="denver_core",
        vehicle_identifier=VID, current_range_meters=_RANGE_METERS_AT_51_PERCENT,
    )
    cycle = _seed_cycle(pg_conn)
    snapshot_time = datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc)
    stats = ride_watch.update_watches_for_cycle(cycle, snapshot_time, devices=[dev])
    assert stats.newly_reappeared == 1
    return stats


# ---------------------------------------------------------------------------
# Happy path: GBFS already resolved at donation time -> immediate eligible
# ---------------------------------------------------------------------------

def test_eligible_donation_awards_battery_points_and_ingests_observation(pg_conn):
    client, account_id = _client(pg_conn)
    ride, fixes, t1_ms = _start_and_walk(client)
    ride_id = ride["id"]
    pg_conn.commit()

    _left_feed_cycle(pg_conn, ride_id)
    _end_ride(client, ride_id, t1_ms)
    pg_conn.commit()

    # GBFS reappears exactly at the last fix's position/time -> resolved
    # BEFORE donation, so verify_track_chain's check 5 reads "ok" and the
    # donation settles "eligible" immediately.
    last_ms, last_lat, last_lon, _ = fixes[-1]
    _reappear_cycle(pg_conn, at_ms=last_ms, lat=last_lat, lon=last_lon)
    pg_conn.commit()

    batches = _seal_chain(
        fixes, ride_id=ride_id,
        nonce_hex=ride["track_signing"]["nonce"], key_b64=ride["track_signing"]["key"],
    )
    r = client.post(f"/api/v1/tracked-rides/{ride_id}/track", json={"batches": batches})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["verification"] == {
        "chain": "ok", "monotonic": "ok", "speed": "ok",
        "gbfs_start": "ok", "gbfs_end": "ok", "volume": "ok",
    }
    assert body["validation"] == {"status": "eligible", "reasons": []}
    assert body["waypoint_count"] == len(fixes)
    # 12 segments * 240 m = 2880 m -> 8 + 2*ceil(2880/2000) = 8 + 4 = 12.
    # ride_routes (A3) doesn't exist yet in this migration tree, so
    # nav_distance_bonus is gracefully skipped -- battery_contribution only.
    assert body["points"] == [{"action": "battery_contribution", "points": 12}]
    assert 2800 < body["distance_meters"] < 2900

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT distance_meters, waypoint_count, batch_count, points_awarded, "
            "account_id, tracked_ride_id, chain_root_hash "
            "FROM track_donations WHERE tracked_ride_id = %s",
            (ride_id,),
        )
        donation = cur.fetchone()
        assert donation is not None
        assert donation[3] == 12  # points_awarded
        assert donation[4] == account_id
        assert donation[6]  # chain_root_hash is a non-empty hex string

        cur.execute(
            "SELECT COUNT(*) FROM donated_track_points WHERE donation_id = "
            "(SELECT id FROM track_donations WHERE tracked_ride_id = %s)",
            (ride_id,),
        )
        (point_count,) = cur.fetchone()
        assert point_count == len(fixes)

        cur.execute("SELECT track_donated_at, validation_status FROM tracked_rides WHERE id = %s",
                    (ride_id,))
        track_donated_at, validation_status = cur.fetchone()
        assert track_donated_at is not None
        assert validation_status == "eligible"

        cur.execute(
            "SELECT points FROM user_points WHERE source_table = 'tracked_rides' "
            "AND source_id = %s AND action = 'battery_contribution'",
            (ride_id,),
        )
        (ledger_points,) = cur.fetchone()
        assert ledger_points == 12

        cur.execute(
            "SELECT source, burn_percent, soc_start_percent, soc_end_percent "
            "FROM battery_trip_observations WHERE vehicle_identifier = %s",
            (VID,),
        )
        obs = cur.fetchone()
        assert obs is not None, "battery ingestion did not run on an eligible donation"
        assert obs[0] == "donated_ride"
        assert obs[2] == 82.0  # reported_start_battery_percent (no feed observation seeded)
        assert obs[3] == 61.0  # reported_battery_percent
        assert obs[1] == pytest.approx(21.0)  # burn = 82 - 61


def test_a_second_donation_is_rejected_already_donated(pg_conn):
    client, _ = _client(pg_conn)
    ride, fixes, t1_ms = _start_and_walk(client)
    ride_id = ride["id"]
    pg_conn.commit()
    _left_feed_cycle(pg_conn, ride_id)
    _end_ride(client, ride_id, t1_ms)
    pg_conn.commit()
    last_ms, last_lat, last_lon, _ = fixes[-1]
    _reappear_cycle(pg_conn, at_ms=last_ms, lat=last_lat, lon=last_lon)
    pg_conn.commit()

    batches = _seal_chain(fixes, ride_id=ride_id, nonce_hex=ride["track_signing"]["nonce"],
                          key_b64=ride["track_signing"]["key"])
    first = client.post(f"/api/v1/tracked-rides/{ride_id}/track", json={"batches": batches})
    assert first.status_code == 200, first.text
    pg_conn.commit()

    second = client.post(f"/api/v1/tracked-rides/{ride_id}/track", json={"batches": batches})
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "already_donated"


def test_donating_before_the_ride_ended_is_409(pg_conn):
    client, _ = _client(pg_conn)
    ride, fixes, _t1_ms = _start_and_walk(client)
    ride_id = ride["id"]
    pg_conn.commit()

    batches = _seal_chain(fixes, ride_id=ride_id, nonce_hex=ride["track_signing"]["nonce"],
                          key_b64=ride["track_signing"]["key"])
    r = client.post(f"/api/v1/tracked-rides/{ride_id}/track", json={"batches": batches})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "ride_not_ended"


def test_donating_without_save_tracks_opted_in_is_422(pg_conn):
    client, _ = _client(pg_conn)
    ride, fixes, t1_ms = _start_and_walk(client, ride_options={"save_tracks": False})
    ride_id = ride["id"]
    pg_conn.commit()
    _end_ride(client, ride_id, t1_ms)
    pg_conn.commit()

    batches = _seal_chain(fixes, ride_id=ride_id, nonce_hex=ride["track_signing"]["nonce"],
                          key_b64=ride["track_signing"]["key"])
    r = client.post(f"/api/v1/tracked-rides/{ride_id}/track", json={"batches": batches})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "tracking_not_opted"

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM track_donations WHERE tracked_ride_id = %s", (ride_id,))
        assert cur.fetchone()[0] == 0


def test_a_chain_signed_with_the_wrong_key_is_422_chain_invalid_and_writes_nothing(pg_conn):
    client, _ = _client(pg_conn)
    ride, fixes, t1_ms = _start_and_walk(client)
    ride_id = ride["id"]
    pg_conn.commit()
    _end_ride(client, ride_id, t1_ms)
    pg_conn.commit()

    wrong_key = base64.urlsafe_b64encode(b"0" * 32).rstrip(b"=").decode()
    batches = _seal_chain(fixes, ride_id=ride_id, nonce_hex=ride["track_signing"]["nonce"],
                          key_b64=wrong_key)
    r = client.post(f"/api/v1/tracked-rides/{ride_id}/track", json={"batches": batches})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "chain_invalid"
    assert detail["failing_check"] == "chain"
    assert detail["batch_seq"] == 0

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM track_donations WHERE tracked_ride_id = %s", (ride_id,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT track_donated_at FROM tracked_rides WHERE id = %s", (ride_id,))
        assert cur.fetchone()[0] is None, "a rejected chain must not consume the donation slot"


def test_413_too_many_batches(pg_conn):
    client, _ = _client(pg_conn)
    ride, fixes, t1_ms = _start_and_walk(client)
    ride_id = ride["id"]
    pg_conn.commit()
    _end_ride(client, ride_id, t1_ms)
    pg_conn.commit()

    batches = _seal_chain(fixes, ride_id=ride_id, nonce_hex=ride["track_signing"]["nonce"],
                          key_b64=ride["track_signing"]["key"])
    too_many = batches * (api_tracked_rides.MAX_TRACK_DONATION_BATCHES + 1)
    r = client.post(f"/api/v1/tracked-rides/{ride_id}/track", json={"batches": too_many})
    assert r.status_code == 413
    assert r.json()["detail"]["error"] == "too_many_batches"


def test_an_internal_verifier_error_is_a_clean_200_and_writes_no_row(pg_conn, monkeypatch):
    """track_verify.py's own defensive verdict="error" catch-all carries
    chain_root_hash=None, same as chain_invalid -- but
    track_donations.chain_root_hash is NOT NULL, so there is nothing
    honest to persist. Unlike chain_invalid this is OUR bug, not the
    client's: donate_track must respond 200 (Screen 10 renders this as a
    real, if rare, narrative branch, not a network failure) and leave the
    donation slot open for a retry, not 500 on a NotNullViolation."""
    from src import track_verify

    client, _ = _client(pg_conn)
    ride, fixes, t1_ms = _start_and_walk(client)
    ride_id = ride["id"]
    pg_conn.commit()
    _end_ride(client, ride_id, t1_ms)
    pg_conn.commit()

    def _blow_up(cur, ride_row, batches):
        return track_verify.VerificationResult(
            verdict="error", reasons=["internal_error"],
            chain_root_hash=None, distance_meters=0.0, waypoint_count=0,
            per_check={k: "skipped" for k in track_verify.CHECK_KEYS},
        )

    monkeypatch.setattr(api_tracked_rides, "verify_track_chain", _blow_up)

    batches = _seal_chain(fixes, ride_id=ride_id, nonce_hex=ride["track_signing"]["nonce"],
                          key_b64=ride["track_signing"]["key"])
    r = client.post(f"/api/v1/tracked-rides/{ride_id}/track", json={"batches": batches})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["validation"] == {"status": "error", "reasons": ["internal_error"]}
    assert body["donation_id"] is None
    assert body["points"] == []

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM track_donations WHERE tracked_ride_id = %s", (ride_id,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT track_donated_at FROM tracked_rides WHERE id = %s", (ride_id,))
        assert cur.fetchone()[0] is None, "an internal-error verdict must leave the slot open for a retry"


# ---------------------------------------------------------------------------
# pending_feed at donation time -> settled later by finalize_validation
# ---------------------------------------------------------------------------

def test_pending_feed_donation_settles_eligible_on_a_later_gbfs_reappearance(pg_conn):
    client, account_id = _client(pg_conn)
    ride, fixes, t1_ms = _start_and_walk(client)
    ride_id = ride["id"]
    pg_conn.commit()

    _left_feed_cycle(pg_conn, ride_id)
    _end_ride(client, ride_id, t1_ms)
    pg_conn.commit()
    # Deliberately NO reappear cycle yet -- GBFS is still unresolved
    # ('left_feed') when the donation lands.

    batches = _seal_chain(fixes, ride_id=ride_id, nonce_hex=ride["track_signing"]["nonce"],
                          key_b64=ride["track_signing"]["key"])
    r = client.post(f"/api/v1/tracked-rides/{ride_id}/track", json={"batches": batches})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verification"]["gbfs_end"] == "pending_feed"
    assert body["validation"] == {"status": "pending_feed", "reasons": []}
    assert body["points"] == [], "distance-dependent points must be HELD, not paid yet"
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT points_settled_at, points_awarded FROM track_donations "
            "WHERE tracked_ride_id = %s", (ride_id,),
        )
        settled_at, points_awarded = cur.fetchone()
        assert settled_at is None
        assert points_awarded == 0
        cur.execute("SELECT track_donated_at FROM tracked_rides WHERE id = %s", (ride_id,))
        assert cur.fetchone()[0] is not None, "a pending_feed donation still consumes the slot"
        cur.execute("SELECT COUNT(*) FROM battery_trip_observations WHERE vehicle_identifier = %s",
                    (VID,))
        assert cur.fetchone()[0] == 0, "no ingestion yet -- GBFS has not resolved"

    # Now GBFS resolves, matching the last waypoint -- ride_watch's own
    # wiring calls finalize_validation automatically for the newly-
    # reappeared ride, which must settle this donation to eligible, credit
    # the held points, and ingest the battery observation.
    last_ms, last_lat, last_lon, _ = fixes[-1]
    _reappear_cycle(pg_conn, at_ms=last_ms, lat=last_lat, lon=last_lon)
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT validation_status FROM tracked_rides WHERE id = %s", (ride_id,))
        assert cur.fetchone()[0] == "eligible"

        cur.execute(
            "SELECT points_settled_at, points_awarded FROM track_donations "
            "WHERE tracked_ride_id = %s", (ride_id,),
        )
        settled_at, points_awarded = cur.fetchone()
        assert settled_at is not None
        assert points_awarded == 12

        cur.execute(
            "SELECT points FROM user_points WHERE source_table = 'tracked_rides' "
            "AND source_id = %s AND action = 'battery_contribution'",
            (ride_id,),
        )
        row = cur.fetchone()
        assert row is not None, "the late-eligible settle must credit the held battery award"
        assert row[0] == 12

        cur.execute("SELECT source FROM battery_trip_observations WHERE vehicle_identifier = %s",
                    (VID,))
        obs = cur.fetchone()
        assert obs is not None, "the late-eligible settle must also run battery ingestion"
        assert obs[0] == "donated_ride"


def test_pending_feed_donation_settles_ineligible_when_gbfs_never_matches(pg_conn):
    """A late reappearance far from the last waypoint settles end_mismatch,
    not a resurrected pending_feed -- and never pays the held points."""
    client, _ = _client(pg_conn)
    ride, fixes, t1_ms = _start_and_walk(client)
    ride_id = ride["id"]
    pg_conn.commit()
    _left_feed_cycle(pg_conn, ride_id)
    _end_ride(client, ride_id, t1_ms)
    pg_conn.commit()

    batches = _seal_chain(fixes, ride_id=ride_id, nonce_hex=ride["track_signing"]["nonce"],
                          key_b64=ride["track_signing"]["key"])
    r = client.post(f"/api/v1/tracked-rides/{ride_id}/track", json={"batches": batches})
    assert r.status_code == 200, r.text
    assert r.json()["validation"]["status"] == "pending_feed"
    pg_conn.commit()

    last_ms, _, _, _ = fixes[-1]
    # Reappears far from where the track ended.
    _reappear_cycle(pg_conn, at_ms=last_ms, lat=39.90, lon=-105.20)
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT validation_status, validation_reasons FROM tracked_rides WHERE id = %s",
                    (ride_id,))
        status, reasons = cur.fetchone()
        assert status == "ineligible"
        assert reasons == ["end_mismatch"]

        cur.execute(
            "SELECT points_settled_at, points_awarded FROM track_donations "
            "WHERE tracked_ride_id = %s", (ride_id,),
        )
        settled_at, points_awarded = cur.fetchone()
        assert settled_at is not None, "points_settled_at is stamped on denial too (starts de-id clock)"
        assert points_awarded == 0

        cur.execute(
            "SELECT COUNT(*) FROM user_points WHERE source_table = 'tracked_rides' AND source_id = %s",
            (ride_id,),
        )
        assert cur.fetchone()[0] == 0
