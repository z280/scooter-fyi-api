"""Tests for src/api_tracked_rides.py's pure response-shaping logic
(_row_to_ride's anti-fraud GBFS redaction is the highest-stakes piece —
tested directly, no DB needed) plus the request-validation guards that
follow the same fake-cursor idiom as the other API validation tests."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_tracked_rides
from src.accounts import SessionUser, require_session
from src.polyline import decode as decode_polyline, encode as encode_polyline

_RIDE_ID = uuid.uuid4()
_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    expires_at=_NOW, sliding=True, method="google", token_sha256="x",
)


def _row(
    *, reported: bool = False, gbfs_reappeared: bool = False,
    path_polyline: str = "",
    distance_meters: float | None = None,
    distance_source: str | None = None,
    distance_clamped_from_m: float | None = None,
    reported_minutes: int | None = None,
    reported_plan: str | None = None,
    ride_options: dict | None = None,
    validation_status: str = "pending",
    validation_reasons: list | None = None,
) -> tuple:
    """Column order must track _RIDE_COLS in src/api_tracked_rides.py."""
    return (
        _RIDE_ID, "watching", _NOW, 39.74, -104.98, _NOW,
        _NOW if gbfs_reappeared else None,           # gbfs_left_feed_at
        _NOW if gbfs_reappeared else None,            # gbfs_reappeared_at
        39.75 if gbfs_reappeared else None,           # gbfs_end_lat
        -104.99 if gbfs_reappeared else None,         # gbfs_end_lon
        42 if gbfs_reappeared else None,              # gbfs_end_battery_percent
        _NOW if reported else None,                   # user_reported_ended_at
        39.751 if reported else None,                 # end_lat
        -104.991 if reported else None,               # end_lon
        78.2 if reported else None,                   # reported_battery_percent
        350 if reported else None,                    # total_cost_cents
        {}, path_polyline, "aaaa000000000000", _NOW, _NOW,
        distance_meters, distance_source, distance_clamped_from_m,
        # sql/047 (FEATURE_PLAN §10) and sql/049 (ride sessions), appended in
        # _RIDE_COLS order. The signing columns are deliberately NOT here:
        # they live in _RIDE_COLS_OWNER, which no list response selects.
        reported_minutes, reported_plan,
        {} if ride_options is None else ride_options,
        validation_status,
        [] if validation_reasons is None else validation_reasons,
    )


def _end_select(*, already_ended: bool = False, gbfs_end: tuple | None = None,
                ride_options: dict | None = None) -> tuple:
    """The narrower SELECT ... FOR UPDATE that PATCH .../end issues before
    it writes. Distinct from _row above — different column list."""
    gbfs_lat, gbfs_lon = gbfs_end if gbfs_end else (None, None)
    return (
        _NOW if already_ended else None,   # user_reported_ended_at
        "aaaa000000000000",                # vehicle_identifier
        None,                              # gbfs_reappeared_at
        gbfs_lat, gbfs_lon,
        39.74, -104.98,                    # start_lat, start_lon
        # sql/049 — read under the same lock so the provisional
        # validation_status is computed off the row already held FOR UPDATE.
        {} if ride_options is None else ride_options,
    )


# ---------- _row_to_ride: anti-fraud GBFS redaction -------------------------

def test_gbfs_fields_are_redacted_until_reported():
    ride = api_tracked_rides._row_to_ride(_row(reported=False, gbfs_reappeared=True))
    assert ride["gbfs_reappeared_at"] is None
    assert ride["gbfs_end_lat"] is None
    assert ride["gbfs_end_lon"] is None
    assert ride["gbfs_end_battery_percent"] is None


def test_gbfs_fields_are_visible_once_reported():
    ride = api_tracked_rides._row_to_ride(_row(reported=True, gbfs_reappeared=True))
    assert ride["gbfs_reappeared_at"] is not None
    assert ride["gbfs_end_lat"] == 39.75
    assert ride["gbfs_end_battery_percent"] == 42


def test_reported_end_fields_are_never_redacted():
    """Only the GBFS side is gated on the report — the rider's own report
    is always visible in their own response."""
    ride = api_tracked_rides._row_to_ride(_row(reported=True))
    assert ride["end_lat"] == 39.751
    assert ride["reported_battery_percent"] == 78.2


def test_path_geojson_decodes_the_polyline():
    points = [(39.74, -104.98), (39.741, -104.981)]
    ride = api_tracked_rides._row_to_ride(_row(path_polyline=encode_polyline(points)))
    assert ride["path_geojson"]["type"] == "LineString"
    assert len(ride["path_geojson"]["coordinates"]) == 2


def test_path_geojson_is_none_for_a_ride_with_no_waypoints_yet():
    ride = api_tracked_rides._row_to_ride(_row(path_polyline=""))
    assert ride["path_geojson"] is None


def test_path_geojson_omitted_when_requested():
    ride = api_tracked_rides._row_to_ride(_row(), path_geojson=False)
    assert "path_geojson" not in ride
    assert "path_polyline" not in ride


# ---------- request validation (fake cursor) --------------------------------

class _FakeCursor:
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


class _FakeConn:
    def __init__(self, fetches, waypoints=()):
        self._fetches = fetches
        self._waypoints = waypoints
        self.cur: _FakeCursor | None = None

    def cursor(self):
        self.cur = _FakeCursor(self._fetches, self._waypoints)
        return self.cur

    def commit(self):
        pass


def _app():
    app = FastAPI()
    app.include_router(api_tracked_rides.router)
    app.dependency_overrides[require_session] = lambda: _USER
    return app


def _client(monkeypatch, fetches, waypoints=()):
    conn = _FakeConn(fetches, waypoints)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_tracked_rides, "connection", _fake_connection)
    monkeypatch.setattr(api_tracked_rides, "enforce", lambda cur, **kw: None)
    return TestClient(_app()), conn


def test_start_ride_404_on_unknown_vehicle(monkeypatch):
    c, _ = _client(monkeypatch, fetches=[None])  # device_state lookup -> not found
    r = c.post("/api/v1/tracked-rides", json={
        "vehicle_identifier": "aaaa000000000000", "start_lat": 39.74, "start_lon": -104.98,
    })
    assert r.status_code == 404


def test_start_ride_409_when_already_active(monkeypatch):
    c, _ = _client(monkeypatch, fetches=[(1,), (1,)])  # device found, active ride found
    r = c.post("/api/v1/tracked-rides", json={
        "vehicle_identifier": "aaaa000000000000", "start_lat": 39.74, "start_lon": -104.98,
    })
    assert r.status_code == 409


def test_start_ride_rejects_bad_vehicle_identifier_shape():
    r = TestClient(_app()).post("/api/v1/tracked-rides", json={
        "vehicle_identifier": "not-16-hex", "start_lat": 39.74, "start_lon": -104.98,
    })
    assert r.status_code == 422


def test_end_ride_requires_tz_aware_timestamp():
    r = TestClient(_app()).patch(f"/api/v1/tracked-rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-01T12:00:00", "end_lat": 39.75, "end_lon": -104.99,
    })
    assert r.status_code == 400
    assert "ended_at" in r.json()["detail"]


def test_end_ride_404_when_not_found(monkeypatch):
    c, _ = _client(monkeypatch, fetches=[None])
    r = c.patch(f"/api/v1/tracked-rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-01T12:00:00Z", "end_lat": 39.75, "end_lon": -104.99,
    })
    assert r.status_code == 404


def test_end_ride_409_when_already_reported(monkeypatch):
    # (user_reported_ended_at, vehicle_identifier, gbfs_reappeared_at,
    # gbfs_end_lat, gbfs_end_lon) — already reported.
    c, _ = _client(monkeypatch, fetches=[_end_select(already_ended=True)])
    r = c.patch(f"/api/v1/tracked-rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-01T12:00:00Z", "end_lat": 39.75, "end_lon": -104.99,
    })
    assert r.status_code == 409


def test_end_ride_no_longer_credits_waypoint_points(monkeypatch):
    """SUPERSEDED as of PLAN_RIDE_MODE_API.md phase A2 (Decision 6 / Risk 5):
    PATCH .../end used to award `waypoint` points (2 * waypoint_count) here.
    It no longer queries ride_waypoints or writes to user_points at all —
    the reshaped ride-mode awards are credited from POST .../track and
    POST .../survey instead. Only the initial SELECT ... FOR UPDATE and the
    final re-SELECT fetch a row now; _track_points still reads the waypoint
    track (via fetchall, not fetchone) to measure the path, so `waypoints=`
    still matters for distance/polyline — it just no longer feeds an award.
    """
    fetches = [_end_select(), _row()]
    c, conn = _client(
        monkeypatch, fetches,
        waypoints=[(39.74 + 0.001, -104.98), (39.74 + 0.002, -104.98), (39.74 + 0.003, -104.98)],
    )
    r = c.patch(f"/api/v1/tracked-rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-01T12:00:00Z", "end_lat": 39.75, "end_lon": -104.99,
    })
    assert r.status_code == 200, r.text
    assert not any(c[0].startswith("SELECT COUNT(*) FROM ride_waypoints") for c in conn.cur.executed)
    assert not any(c[0].startswith("INSERT INTO user_points") for c in conn.cur.executed)


def test_end_ride_no_waypoints_and_no_gbfs_data_credits_nothing(monkeypatch):
    fetches = [_end_select(), _row()]
    c, conn = _client(monkeypatch, fetches)
    r = c.patch(f"/api/v1/tracked-rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-01T12:00:00Z", "end_lat": 39.75, "end_lon": -104.99,
    })
    assert r.status_code == 200, r.text
    assert not any(c[0].startswith("INSERT INTO user_points") for c in conn.cur.executed)


# ---------- distance on end (sql/034) ---------------------------------------

def _end_update(conn) -> tuple:
    return next(c for c in conn.cur.executed
                if c[0].lstrip().startswith("UPDATE tracked_rides SET")
                and "status = 'completed'" in c[0])


def test_end_ride_without_waypoints_falls_back_to_straight_line(monkeypatch):
    """No waypoints -> distance is start->end as the crow flies, tagged so
    downstream readers know it's the weak measurement."""
    c, conn = _client(monkeypatch, [_end_select(), _row()])
    r = c.patch(f"/api/v1/tracked-rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-01T12:00:00Z", "end_lat": 39.75, "end_lon": -104.98,
    })
    assert r.status_code == 200, r.text
    params = _end_update(conn)[1]
    assert "straight_line" in params
    # 39.74 -> 39.75 at constant longitude is ~1113 m.
    distance = next(p for p in params if isinstance(p, float) and p > 1000)
    assert 1100 < distance < 1120


def test_end_ride_drops_an_implausible_final_leg_and_says_so(monkeypatch):
    """RECONCILES two rules that disagreed.

    The previous commit made PATCH .../end measure the leg from the last
    fix to the reported end, because a phone that backgrounds stops
    reporting long before the rider parks — one fix 20 m along and a park
    10 km later used to record 20 m.

    The operator's 3 km leg cap says that 10 km jump is not a leg we are
    willing to measure. The cap wins: the leg is dropped, the ride records
    only the track it believes, and `distance_source` carries `_partial`
    so nobody reads the result as a whole-path measurement. The ride still
    COMPLETES — the end report is never refused.

    Same expectation as the off-feed namesake in
    tests/test_api_rides_validation.py; badges sum both tables.
    """
    step = 1.0 / 111_320.0  # metres of latitude
    c, conn = _client(
        monkeypatch,
        # end SELECT, final SELECT — no waypoint-count/points crediting
        # since PLAN_RIDE_MODE_API.md phase A2 superseded that at /end.
        [_end_select(), _row()],
        waypoints=[(39.74 + 20 * step, -104.98)],
    )
    r = c.patch(f"/api/v1/tracked-rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-01T12:00:00Z",
        "end_lat": 39.74 + 10_000 * step, "end_lon": -104.98,
    })
    assert r.status_code == 200, "an implausible final leg must never strand the ride"
    sql, params = _end_update(conn)
    assert "waypoints_partial" in params
    # (ended_at, end_lat, end_lon, battery, cost, metadata, polyline,
    #  distance, source, clamped_from, ride_id)
    assert 15 < params[7] < 25, "the 10 km jump was measured after all"
    # The polyline covers the same points the distance was measured over —
    # the excluded end point is not in either.
    assert "path_polyline = %s" in sql
    assert len(decode_polyline(params[6])) == 2  # start, fix. No reported end.


def test_end_ride_without_waypoints_does_not_fabricate_a_path(monkeypatch):
    """A straight-line distance is an honest fallback; a two-point
    path_polyline would be a route we never observed."""
    c, conn = _client(monkeypatch, [_end_select(), _row()])
    r = c.patch(f"/api/v1/tracked-rides/{_RIDE_ID}/end", json={
        "ended_at": "2026-07-01T12:00:00Z", "end_lat": 39.75, "end_lon": -104.98,
    })
    assert r.status_code == 200, r.text
    assert "path_polyline" not in _end_update(conn)[0]


def test_row_to_ride_exposes_distance_without_redacting_it():
    """distance is derived from the rider's own data, so unlike the gbfs_*
    fields it is visible before they report their end."""
    ride = api_tracked_rides._row_to_ride(
        _row(reported=False, distance_meters=1234.56, distance_source="waypoints"))
    assert ride["distance_meters"] == 1234.6
    assert ride["distance_source"] == "waypoints"


def test_row_to_ride_distance_is_none_before_it_is_computed():
    ride = api_tracked_rides._row_to_ride(_row())
    assert ride["distance_meters"] is None
    assert ride["distance_source"] is None


def test_waypoint_requires_tz_aware_timestamp():
    r = TestClient(_app()).post(f"/api/v1/tracked-rides/{_RIDE_ID}/waypoints", json={
        "waypoint_at": "2026-07-01T12:00:00", "lat": 39.74, "lon": -104.98,
    })
    assert r.status_code == 400


def test_waypoint_409_when_ride_not_found(monkeypatch):
    c, _ = _client(monkeypatch, fetches=[None])
    r = c.post(f"/api/v1/tracked-rides/{_RIDE_ID}/waypoints", json={
        "waypoint_at": "2026-07-01T12:00:00Z", "lat": 39.74, "lon": -104.98,
    })
    assert r.status_code == 404


def test_waypoint_409_when_ride_already_ended(monkeypatch):
    # (user_reported_ended_at, gbfs_reappeared_at, watch_expires_at,
    # start_lat, start_lon) — ended.
    c, _ = _client(monkeypatch, fetches=[(_NOW, None, _NOW, 39.74, -104.98)])
    r = c.post(f"/api/v1/tracked-rides/{_RIDE_ID}/waypoints", json={
        "waypoint_at": "2026-07-01T12:00:00Z", "lat": 39.74, "lon": -104.98,
    })
    assert r.status_code == 409


def test_waypoint_409_when_watch_expired(monkeypatch):
    from datetime import timedelta
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    c, _ = _client(monkeypatch, fetches=[(None, None, past, 39.74, -104.98)])
    r = c.post(f"/api/v1/tracked-rides/{_RIDE_ID}/waypoints", json={
        "waypoint_at": "2026-07-01T12:00:00Z", "lat": 39.74, "lon": -104.98,
    })
    assert r.status_code == 409


def test_active_route_registered_before_ride_id_route():
    """Regression guard: /active must resolve to active_tracked_ride, not
    get_tracked_ride(ride_id='active') — this only holds if the route is
    registered first, since Starlette matches path routes in order."""
    paths = [r.path for r in api_tracked_rides.router.routes]
    assert paths.index("/api/v1/tracked-rides/active") < paths.index("/api/v1/tracked-rides/{ride_id}")


def test_bad_ride_id_returns_400_not_500():
    r = TestClient(_app()).get("/api/v1/tracked-rides/not-a-uuid")
    assert r.status_code == 400


def test_list_rejects_bad_status_filter():
    r = TestClient(_app()).get("/api/v1/tracked-rides", params={"status": "bogus"})
    assert r.status_code == 422
