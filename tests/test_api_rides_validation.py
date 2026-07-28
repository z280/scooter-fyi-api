"""Off-feed ride endpoints (src/api_rides.py): `before` validation plus the
start -> waypoints -> end lifecycle.

Uses a fake cursor/connection so the tests exercise real FastAPI request
handling (query parsing, dependency injection, HTTPException status) without
a live Postgres. The end-to-end path against real Postgres is covered by
tests/test_off_feed_rides_lifecycle_pg.py.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_rides
from src.accounts import SessionUser, require_session


class _FakeCursor:
    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def commit(self):
        pass


@contextmanager
def _fake_connection():
    yield _FakeConn()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api_rides, "connection", _fake_connection)
    app = FastAPI()
    app.include_router(api_rides.router)
    app.dependency_overrides[require_session] = lambda: SessionUser(
        account_id=1, email="rider@example.com", scopes=("rider",),
        expires_at=datetime.now(timezone.utc),
        sliding=True, method="google", token_sha256="x",
    )
    return TestClient(app)


def test_before_without_timezone_is_rejected(client):
    r = client.get("/api/v1/rides", params={"before": "2026-06-01T00:00:00"})
    assert r.status_code == 400
    assert "timezone" in r.json()["detail"]


def test_before_with_z_suffix_is_accepted(client):
    r = client.get("/api/v1/rides", params={"before": "2026-06-01T00:00:00Z"})
    assert r.status_code == 200


def test_before_with_explicit_offset_is_accepted(client):
    r = client.get("/api/v1/rides", params={"before": "2026-06-01T00:00:00+00:00"})
    assert r.status_code == 200


def test_before_omitted_is_fine(client):
    r = client.get("/api/v1/rides")
    assert r.status_code == 200


# ---------- lifecycle: start -> waypoints -> end ----------------------------

_RIDE_ID = uuid.uuid4()
_NOW = datetime(2026, 7, 27, 16, 20, tzinfo=timezone.utc)


def _ride_row(
    *, status: str = "active", ended: bool = False,
    distance_m: int | None = None, distance_source: str | None = None,
    distance_clamped_from_m: float | None = None,
) -> tuple:
    """Column order must track _RIDE_COLS in src/api_rides.py."""
    return (
        _RIDE_ID, _NOW, _NOW,
        _NOW if ended else None,          # ended_at
        1500 if ended else None,          # duration_s
        distance_m,
        None,                             # est_cost_cents
        None,                             # rate_plan
        True if ended else None,          # started_in_zone
        False if ended else None,         # ended_in_zone
        "" if ended else None,            # polyline
        status, "scooter", "Lime",
        39.74, -104.98,
        39.75 if ended else None,         # end_lat
        -104.99 if ended else None,       # end_lon
        distance_source,
        distance_clamped_from_m,
    )


class _SeqCursor:
    def __init__(self, fetches, waypoints=()):
        self._fetches = list(fetches)
        self._waypoints = list(waypoints)
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._fetches.pop(0)

    def fetchall(self):
        # The only fetchall in this module is the waypoint track read.
        return list(self._waypoints)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _SeqConn:
    def __init__(self, fetches, waypoints=()):
        self._fetches = fetches
        self._waypoints = waypoints
        self.cur: _SeqCursor | None = None

    def cursor(self):
        self.cur = _SeqCursor(self._fetches, self._waypoints)
        return self.cur

    def commit(self):
        pass


def _seq_client(monkeypatch, fetches, waypoints=()):
    conn = _SeqConn(fetches, waypoints)

    @contextmanager
    def _conn():
        yield conn

    monkeypatch.setattr(api_rides, "connection", _conn)
    monkeypatch.setattr(api_rides, "enforce", lambda cur, **kw: None)
    app = FastAPI()
    app.include_router(api_rides.router)
    app.dependency_overrides[require_session] = lambda: SessionUser(
        account_id=1, email="rider@example.com", scopes=("rider",),
        expires_at=datetime.now(timezone.utc),
        sliding=True, method="google", token_sha256="x",
    )
    return TestClient(app), conn


def _end_update(conn) -> tuple:
    return next(c for c in conn.cur.executed
                if c[0].startswith("UPDATE rides SET status = 'completed'"))


def test_start_ride_returns_an_active_ride(monkeypatch):
    c, _ = _seq_client(monkeypatch, [_ride_row()])
    r = c.post("/api/v1/rides/start", json={
        "start_lat": 39.74, "start_lon": -104.98,
        "vehicle_kind": "scooter", "operator": "Lime",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["operator"] == "Lime"
    # An active ride has no end yet — these must serialize as null, not crash.
    assert body["ended_at"] is None
    assert body["distance_m"] is None
    assert body["distance_source"] is None


def test_start_ride_409_when_one_is_already_active(monkeypatch):
    """The unique partial index is the arbiter, so the handler has to
    translate its UniqueViolation rather than pre-checking."""
    from psycopg import errors as pg_errors

    class _RaisingCursor(_SeqCursor):
        def execute(self, sql, params=()):
            super().execute(sql, params)
            if sql.lstrip().startswith("INSERT INTO rides"):
                raise pg_errors.UniqueViolation("duplicate")

    conn = _SeqConn([])
    conn.cursor = lambda: setattr(conn, "cur", _RaisingCursor([])) or conn.cur

    @contextmanager
    def _conn():
        yield conn

    monkeypatch.setattr(api_rides, "connection", _conn)
    monkeypatch.setattr(api_rides, "enforce", lambda cur, **kw: None)
    app = FastAPI()
    app.include_router(api_rides.router)
    app.dependency_overrides[require_session] = lambda: SessionUser(
        account_id=1, email="rider@example.com", scopes=("rider",),
        expires_at=datetime.now(timezone.utc),
        sliding=True, method="google", token_sha256="x",
    )
    r = TestClient(app).post("/api/v1/rides/start",
                             json={"start_lat": 39.74, "start_lon": -104.98})
    assert r.status_code == 409


def test_active_ride_is_always_wrapped(monkeypatch):
    c, _ = _seq_client(monkeypatch, [None])
    assert c.get("/api/v1/rides/active").json() == {"active": None}


def test_waypoint_409_when_ride_not_active(monkeypatch):
    c, _ = _seq_client(monkeypatch, [("completed", 39.74, -104.98)])
    r = c.post(f"/api/v1/rides/{_RIDE_ID}/waypoints", json={
        "waypoint_at": "2026-07-27T16:31:00Z", "lat": 39.745, "lon": -104.985,
    })
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "ride_not_active"


def test_waypoint_404_for_someone_elses_ride(monkeypatch):
    c, _ = _seq_client(monkeypatch, [None])
    r = c.post(f"/api/v1/rides/{_RIDE_ID}/waypoints", json={
        "waypoint_at": "2026-07-27T16:31:00Z", "lat": 39.745, "lon": -104.985,
    })
    assert r.status_code == 404


def test_waypoint_requires_tz_aware_timestamp():
    app = FastAPI()
    app.include_router(api_rides.router)
    app.dependency_overrides[require_session] = lambda: SessionUser(
        account_id=1, email="rider@example.com", scopes=("rider",),
        expires_at=datetime.now(timezone.utc),
        sliding=True, method="google", token_sha256="x",
    )
    r = TestClient(app).post(f"/api/v1/rides/{_RIDE_ID}/waypoints", json={
        "waypoint_at": "2026-07-27T16:31:00", "lat": 39.745, "lon": -104.985,
    })
    assert r.status_code == 400


def test_end_ride_409_when_already_completed(monkeypatch):
    c, _ = _seq_client(monkeypatch, [("completed", _NOW, 39.74, -104.98)])
    r = c.patch(f"/api/v1/rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-27T16:45:00Z", "end_lat": 39.75, "end_lon": -104.99,
    })
    assert r.status_code == 409


def _end_distance(conn) -> tuple[int, str]:
    """(distance_m, distance_source) as written by PATCH .../end. The
    trailing params are (..., distance, source, clamped_from, ride_id)."""
    params = _end_update(conn)[1]
    return params[-4], params[-3]


def test_end_ride_without_waypoints_falls_back_to_straight_line(monkeypatch):
    c, conn = _seq_client(monkeypatch, [
        ("active", _NOW, 39.74, -104.98),
        _ride_row(status="completed", ended=True, distance_m=1113,
                  distance_source="straight_line"),
    ])
    r = c.patch(f"/api/v1/rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-27T16:45:00Z", "end_lat": 39.75, "end_lon": -104.98,
    })
    assert r.status_code == 200, r.text
    distance, source = _end_distance(conn)
    assert source == "straight_line"
    # 39.74 -> 39.75 at constant longitude is ~1113 m.
    assert 1100 < distance < 1120
    # No track, so no fabricated polyline — the '' the schema needs stands.
    assert "polyline = COALESCE(polyline, '')" in _end_update(conn)[0]


def test_end_ride_drops_an_implausible_final_leg_and_says_so(monkeypatch):
    """RECONCILES two rules that disagreed.

    The previous commit made PATCH .../end measure the leg from the last
    fix to the reported end: a phone that backgrounds, saves battery or
    hits a tunnel stops reporting long before the rider parks, and one fix
    20 m along followed by a park 10 km later used to record 20 m for a
    10 km ride, tagged high-confidence.

    The operator's 3 km leg cap says a 10 km jump between consecutive
    points is not something we are willing to measure. The cap wins, and
    the honest result is the narrow one: measure the track we believe,
    drop the leg we don't, and mark the source `_partial` so the number is
    never mistaken for a whole-path measurement. What must NOT happen is
    the ride failing to complete.
    """
    step = 1.0 / 111_320.0  # metres of latitude
    c, conn = _seq_client(
        monkeypatch,
        [
            ("active", _NOW, 39.74, -104.98),
            _ride_row(status="completed", ended=True, distance_m=20,
                      distance_source="waypoints_partial"),
        ],
        waypoints=[(39.74 + 20 * step, -104.98)],  # one fix, 20 m along
    )
    r = c.patch(f"/api/v1/rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-27T16:45:00Z",
        "end_lat": 39.74 + 10_000 * step, "end_lon": -104.98,
    })
    assert r.status_code == 200, "an implausible final leg must never strand the ride"
    distance, source = _end_distance(conn)
    assert source == "waypoints_partial"
    assert 15 < distance < 25, "the 10 km jump was measured after all"
    # The stored polyline covers exactly the points the distance was
    # measured over, so the two still can't disagree — the dropped end
    # point is in neither.
    sql, params = _end_update(conn)
    assert "polyline = %s" in sql
    from src.polyline import decode as decode_polyline
    assert len(decode_polyline(params[-5])) == 2  # start, fix. No reported end.


def test_end_ride_with_waypoints_still_reports_a_tracked_length(monkeypatch):
    """A well-behaved client that streamed the whole route keeps measuring
    along the track, not the crow-flies line between its ends."""
    step = 1.0 / 111_320.0
    # An L: 300 m north, then 300 m east. Straight line ≈ 424 m.
    c, conn = _seq_client(
        monkeypatch,
        [
            ("active", _NOW, 39.74, -104.98),
            _ride_row(status="completed", ended=True, distance_m=600,
                      distance_source="waypoints"),
        ],
        waypoints=[(39.74 + 300 * step, -104.98)],
    )
    east = 300.0 / (111_320.0 * 0.7677)  # cos(39.74°)
    r = c.patch(f"/api/v1/rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-27T16:45:00Z",
        "end_lat": 39.74 + 300 * step, "end_lon": -104.98 + east,
    })
    assert r.status_code == 200, r.text
    distance, source = _end_distance(conn)
    assert source == "waypoints"
    assert 580 < distance < 620, "measured the straight line, not the track"


def test_end_ride_rejects_end_before_start(monkeypatch):
    c, _ = _seq_client(monkeypatch, [("active", _NOW, 39.74, -104.98)])
    r = c.patch(f"/api/v1/rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-27T10:00:00Z", "end_lat": 39.75, "end_lon": -104.99,
    })
    assert r.status_code == 400


def test_one_shot_post_records_client_supplied_distance(monkeypatch):
    c, conn = _seq_client(monkeypatch, [
        _ride_row(status="completed", ended=True, distance_m=2412,
                  distance_source="client"),
    ])
    r = c.post("/api/v1/rides", json={
        "started_at": "2026-07-27T16:20:00Z", "ended_at": "2026-07-27T16:45:00Z",
        "duration_s": 1500, "distance_m": 2412, "started_in_zone": True,
        "ended_in_zone": False, "polyline": "_p~iF~ps|U_ulLnnqC",
        "vehicle_kind": "scooter", "operator": "Lime",
    })
    assert r.status_code == 200, r.text
    assert r.json()["distance_source"] == "client"
    insert = next(c for c in conn.cur.executed if c[0].startswith("INSERT INTO rides"))
    assert "'client'" in insert[0]


def test_one_shot_post_no_longer_requires_supporter(monkeypatch):
    """Regression guard for the decommercialization (sql/036): logging a
    ride used to require a paid status. Signed-in is now the only gate."""
    c, _ = _seq_client(monkeypatch, [_ride_row(status="completed", ended=True)])
    r = c.post("/api/v1/rides", json={
        "started_at": "2026-07-27T16:20:00Z", "ended_at": "2026-07-27T16:45:00Z",
        "duration_s": 1500, "distance_m": 2412, "started_in_zone": True,
        "ended_in_zone": False, "polyline": "_p~iF~ps|U_ulLnnqC",
    })
    assert r.status_code == 200, r.text


def test_one_shot_post_rejects_undecodable_polyline(monkeypatch):
    c, _ = _seq_client(monkeypatch, [])
    r = c.post("/api/v1/rides", json={
        "started_at": "2026-07-27T16:20:00Z", "ended_at": "2026-07-27T16:45:00Z",
        "duration_s": 1500, "distance_m": 2412, "started_in_zone": True,
        "ended_in_zone": False, "polyline": "!!!not-a-polyline!!!",
    })
    assert r.status_code == 400


# ---------- GeoJSON export geometry -----------------------------------------
# A LineString needs >= 2 positions (RFC 7946 §3.1.4). Emitting an empty one
# doesn't degrade a single feature — QGIS/GDAL/geojson.io reject the whole
# FeatureCollection, so one waypoint-less ride used to break the rider's
# entire export.

def _exported(ride: dict) -> dict:
    return api_rides._ride_geometry(ride)


def _export_ride(**overrides) -> dict:
    ride = api_rides._row_to_ride(_ride_row(status="completed", ended=True,
                                            distance_m=1113,
                                            distance_source="straight_line"))
    ride.update(overrides)
    return ride


def test_export_geometry_uses_the_polyline_when_there_is_one():
    from src.polyline import encode as encode_polyline
    geom = _exported(_export_ride(
        polyline=encode_polyline([(39.74, -104.98), (39.75, -104.98), (39.76, -104.97)])))
    assert geom["type"] == "LineString"
    assert len(geom["coordinates"]) == 3
    assert geom["coordinates"][0] == pytest.approx([-104.98, 39.74])  # lon, lat


def test_export_geometry_falls_back_to_the_ride_endpoints():
    """A ride with no waypoints stores polyline '' but still knows where it
    started and ended — that IS its geometry."""
    geom = _exported(_export_ride(polyline=""))
    assert geom["type"] == "LineString"
    assert geom["coordinates"] == [[-104.98, 39.74], [-104.99, 39.75]]


def test_export_geometry_never_emits_an_empty_linestring():
    for polyline in ("", None, "!!!undecodable!!!"):
        geom = _exported(_export_ride(polyline=polyline))
        assert geom is None or len(geom["coordinates"]) >= 2


def test_export_geometry_is_null_when_there_is_nothing_to_draw():
    """An active ride has no end yet. `null` geometry is valid GeoJSON for a
    Feature, so the row's properties still export."""
    geom = _exported(_export_ride(polyline="", end_lat=None, end_lon=None))
    assert geom is None


def test_export_emits_valid_geojson_for_a_waypointless_ride(monkeypatch):
    c, _ = _seq_client(monkeypatch, [])
    monkeypatch.setattr(_SeqCursor, "fetchall",
                        lambda self: [_ride_row(status="completed", ended=True,
                                                distance_m=1113,
                                                distance_source="straight_line")])
    r = c.get("/api/v1/rides/export", params={"format": "geojson"})
    assert r.status_code == 200, r.text
    feature = r.json()["features"][0]
    assert feature["geometry"]["type"] == "LineString"
    assert len(feature["geometry"]["coordinates"]) == 2


# ---------- 24-hour expiry (sql/040) ----------------------------------------
# An off-feed ride left 'active' forever holds the account's only
# one-active-ride slot (idx_rides_one_active_per_account), which used to 409
# its owner out of POST /api/v1/rides/start permanently. The sweep is
# src/cli.py:expire_stale_off_feed_rides; these cover the API's half.

def test_end_ride_409s_with_ride_expired_once_it_has_expired(monkeypatch):
    """Distinguished from 'already ended': nothing was ended, the window
    simply closed, and the rider needs to be told their slot is free."""
    c, _ = _seq_client(monkeypatch, [("expired", _NOW, 39.74, -104.98)])
    r = c.patch(f"/api/v1/rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-27T16:45:00Z", "end_lat": 39.75, "end_lon": -104.99,
    })
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "ride_expired"


def test_active_lookup_ignores_expired_rides(monkeypatch):
    """The endpoint and the unique index read the same column, so an expired
    ride can never show as active while the slot it held is free."""
    c, conn = _seq_client(monkeypatch, [None])
    assert c.get("/api/v1/rides/active").json() == {"active": None}
    select = next(q for q, _ in conn.cur.executed if q.lstrip().startswith("SELECT"))
    assert "status = 'active'" in select


def test_list_accepts_expired_as_a_status_filter(monkeypatch):
    c, _ = _seq_client(monkeypatch, [])
    assert c.get("/api/v1/rides", params={"status": "expired"}).status_code == 200


def test_list_still_rejects_an_unknown_status(monkeypatch):
    c, _ = _seq_client(monkeypatch, [])
    assert c.get("/api/v1/rides", params={"status": "abandoned"}).status_code == 422


# ---------- Plausibility of a client-asserted ride --------------------------
# POST /api/v1/rides stores whatever distance the client sends, and
# src/badges.py counts it: miles_100 is 160 934 m, inside the 200 000 m
# per-ride ceiling, so one request used to earn the top mileage badge.
# Badges must keep counting off-feed rides, so the fix is to disbelieve
# impossible ones rather than to stop counting real ones.

from src.polyline import encode as _encode  # noqa: E402

_DEG_PER_M = 1.0 / 111_320.0


def _route(meters: float) -> str:
    """A two-point north-south polyline of about `meters` metres."""
    return _encode([(39.74, -104.98), (39.74 + meters * _DEG_PER_M, -104.98)])


def _one_shot(c, **overrides):
    body = {
        "started_at": "2026-07-27T16:20:00Z", "ended_at": "2026-07-27T16:45:00Z",
        "duration_s": 1500, "distance_m": 2000, "started_in_zone": True,
        "ended_in_zone": False, "polyline": _route(2000),
    }
    body.update(overrides)
    return c.post("/api/v1/rides", json=body)


def test_a_normal_ride_is_still_accepted(monkeypatch):
    """5 km in 20 minutes (4.2 m/s), route matching. The guard must not cost
    an honest rider their log."""
    c, _ = _seq_client(monkeypatch, [_ride_row(status="completed", ended=True)])
    r = _one_shot(c, distance_m=5000, duration_s=1200, polyline=_route(5000))
    assert r.status_code == 200, r.text


def test_the_badge_farming_request_is_rejected(monkeypatch):
    """The exact vector: 160 934 m — miles_100 in one request — claimed
    over a 90-second ride.

    Now refused one step earlier than it used to be. 160 934 m is above the
    operator's 80 000 m ride cap, so `RideIn.distance_m` rejects it as a
    field violation before `_check_plausible` ever sees it; the speed bound
    that used to catch it is still there and still catches everything under
    the cap (below). Either way it is a 422 and nothing is stored.
    """
    c, conn = _seq_client(monkeypatch, [])
    r = _one_shot(c, distance_m=160_934, duration_s=90, polyline=_route(160_934))
    assert r.status_code == 422
    # No cursor was ever opened: the ride is refused at the door, so nothing
    # is stored and then argued about.
    assert conn.cur is None


def test_implausible_speed_still_binds_under_the_distance_cap(monkeypatch):
    """The 80 km cap does NOT make the speed bound redundant — they bind on
    different axes. 79 km inside the cap, covered in 90 seconds, is still
    nonsense and still `implausible_speed`."""
    c, _ = _seq_client(monkeypatch, [])
    r = _one_shot(c, distance_m=79_000, duration_s=90, polyline=_route(79_000))
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "implausible_speed"


def test_distance_with_zero_duration_is_rejected(monkeypatch):
    c, _ = _seq_client(monkeypatch, [])
    r = _one_shot(c, distance_m=50_000, duration_s=0, polyline=_route(50_000))
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "implausible_speed"


def test_speed_ceiling_leaves_headroom_for_a_fast_real_ride(monkeypatch):
    """A 45 km/h class-3 e-bike average — legal, and comfortably inside the
    20 m/s bound, which is deliberately ~3x the fastest thing this table is
    for because it applies to a whole-ride average."""
    c, _ = _seq_client(monkeypatch, [_ride_row(status="completed", ended=True)])
    r = _one_shot(c, distance_m=12_500, duration_s=1000, polyline=_route(12_500))
    assert r.status_code == 200, r.text


def test_distance_far_beyond_the_submitted_route_is_rejected(monkeypatch):
    """Slow enough to clear the speed bound, but the polyline shows 100 m."""
    c, _ = _seq_client(monkeypatch, [])
    r = _one_shot(c, distance_m=60_000, duration_s=20_000, polyline=_route(100))
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "distance_exceeds_polyline"


def test_a_degenerate_polyline_buys_at_most_the_absolute_slack(monkeypatch):
    """Two coincident points decode to a zero-length route, which would make
    the multiplicative rule reject every short ride. The absolute floor is
    what a farmer is left with: 1 km per request, not 200."""
    c, _ = _seq_client(monkeypatch, [])
    flat = _encode([(39.74, -104.98), (39.74, -104.98)])
    assert _one_shot(c, distance_m=5000, duration_s=3600,
                     polyline=flat).status_code == 422

    c2, _ = _seq_client(monkeypatch, [_ride_row(status="completed", ended=True)])
    assert _one_shot(c2, distance_m=800, duration_s=600,
                     polyline=flat).status_code == 200


def test_a_coarsely_sampled_track_is_not_punished(monkeypatch):
    """An encoded polyline is a SAMPLED path, so its decoded length
    undercounts the real route. 2.5x short is well within normal."""
    c, _ = _seq_client(monkeypatch, [_ride_row(status="completed", ended=True)])
    r = _one_shot(c, distance_m=5000, duration_s=1800, polyline=_route(2000))
    assert r.status_code == 200, r.text


def test_claiming_less_than_the_route_is_never_rejected(monkeypatch):
    """One-sided on purpose: undercounting is not a farming vector, and a
    client reporting a vehicle odometer is being honest, not evasive."""
    c, _ = _seq_client(monkeypatch, [_ride_row(status="completed", ended=True)])
    r = _one_shot(c, distance_m=100, duration_s=1800, polyline=_route(9000))
    assert r.status_code == 200, r.text


def test_a_zero_distance_ride_is_plausible(monkeypatch):
    """A cancelled unlock that went nowhere — the one case where a zero
    duration is legitimate too."""
    c, _ = _seq_client(monkeypatch, [_ride_row(status="completed", ended=True)])
    r = _one_shot(c, distance_m=0, duration_s=0, polyline=_route(0))
    assert r.status_code == 200, r.text
