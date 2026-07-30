"""The operator's three hard ride invariants (src/ride_limits.py).

    A. no ride awards more than 100 points, total
    B. no two consecutive path points are more than 3 km apart
    C. no ride is more than 80 km

These are operator mandates rather than tuned bounds, so the tests below
lean hard on the BOUNDARIES: a cap that rejects the value it says is
allowed is as wrong as one that lets a violation through, and off-by-one
is the failure mode a hand-written comparison actually has.

The Postgres half — the CHECK constraints, the migration's clamp of
existing history, and replay safety — is tests/test_ride_hard_caps_pg.py.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_rides, api_tracked_rides
from src.accounts import SessionUser, require_session
from src.points import POINTS_PER_WAYPOINT, credit_points, credit_waypoint_points
from src.ride_limits import (
    MAX_LEG_METERS,
    MAX_POINTS_PER_RIDE,
    MAX_RIDE_DISTANCE_METERS,
    clamp_distance,
    close_out_path,
    leg_is_plausible,
    measure_path,
)

_NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
_RIDE_ID = uuid.uuid4()
_DEG_PER_M = 1.0 / 111_320.0
_LAT, _LON = 39.74, -104.98


def _north(meters: float) -> tuple[float, float]:
    """A point `meters` due north of the canonical start."""
    return (_LAT + meters * _DEG_PER_M, _LON)


# Anchored at the equator/prime meridian, where the flat-earth arithmetic in
# src/geo.py round-trips EXACTLY: (lat - 0.0) * 111320 has no cancellation.
# From 39.74 the same construction lands on 3000.000000000176 m, which is a
# float artefact of the test helper rather than a real leg length — and an
# exact-boundary test has to be exact to mean anything.
_ORIGIN = (0.0, 0.0)


def _north_of_origin(meters: float) -> tuple[float, float]:
    return (meters * _DEG_PER_M, 0.0)


# ---------------------------------------------------------------------------
# The constants are what the operator said, and nothing has "optimized" them
# ---------------------------------------------------------------------------

def test_the_three_invariants_are_the_operator_numbers():
    """A guard against a well-meaning tune. If one of these has to change,
    the operator changes it and this test changes with it — deliberately,
    not as collateral damage from making some other test pass."""
    assert MAX_POINTS_PER_RIDE == 100
    assert MAX_LEG_METERS == 3_000.0
    assert MAX_RIDE_DISTANCE_METERS == 80_000.0


# ---------------------------------------------------------------------------
# B. the 3 km leg cap — boundary behaviour
# ---------------------------------------------------------------------------

def test_a_leg_of_exactly_three_km_is_allowed():
    """'No two points more than 3 km apart' — 3000 m is not more than 3000 m."""
    assert leg_is_plausible(_ORIGIN, _north_of_origin(3_000.0))


def test_a_leg_just_over_three_km_is_not():
    assert not leg_is_plausible(_ORIGIN, _north_of_origin(3_000.0 + 1.0))


def test_plausibility_is_exactly_the_documented_comparison():
    """Guards the boundary independently of how the test builds its points:
    whatever distance_meters says, the predicate is `<= MAX_LEG_METERS`."""
    for meters in (0, 1, 1_500, 2_999, 3_000, 3_001, 15_000):
        a, b = _ORIGIN, _north_of_origin(meters)
        from src.geo import distance_meters
        assert leg_is_plausible(a, b) == (distance_meters(*a, *b) <= MAX_LEG_METERS)


def test_measure_path_excludes_only_the_implausible_leg():
    """The rest of the path still counts — one bad fix doesn't void a ride."""
    points = [(_LAT, _LON), _north(1_000), _north(1_500), _north(20_000)]
    distance, excluded = measure_path(points, cap_legs=True)
    assert excluded == 1
    # 1000 + 500 kept; the 18.5 km jump dropped.
    assert 1_450 < distance < 1_550


def test_measure_path_without_the_leg_cap_keeps_everything():
    """cap_legs=False is the trackless ride, where start -> end is the whole
    ride rather than a sampling gap."""
    points = [(_LAT, _LON), _north(20_000)]
    distance, excluded = measure_path(points, cap_legs=False)
    assert excluded == 0
    assert 19_900 < distance < 20_100


# ---------------------------------------------------------------------------
# C. the 80 km ride cap — boundary behaviour
# ---------------------------------------------------------------------------

def test_exactly_eighty_km_is_not_clamped():
    recorded, clamped_from = clamp_distance(80_000.0)
    assert recorded == 80_000.0
    assert clamped_from is None


def test_just_over_eighty_km_is_clamped_and_remembers_what_it_saw():
    recorded, clamped_from = clamp_distance(80_000.5)
    assert recorded == 80_000.0
    assert clamped_from == 80_000.5


# ---------------------------------------------------------------------------
# /end NEVER refuses — the invariant close_out_path exists to guarantee
# ---------------------------------------------------------------------------

def test_end_never_raises_however_absurd_the_input():
    """Stranding a rider is the harshest failure this system can produce:
    the active-ride unique index keeps answering 'you are still on a ride'
    until the 24-hour sweep. So there is no input that makes closing a ride
    fail — only inputs that make it record less."""
    cases = [
        # antipodal reported end, with and without a track
        ((_LAT, _LON), [_north(100)], (-39.74, 75.02)),
        ((_LAT, _LON), [], (-39.74, 75.02)),
        # no start point at all
        ((None, None), [_north(100)], _north(200)),
        # a track that is itself nonsense
        ((_LAT, _LON), [_north(500_000), _north(-500_000)], _north(10)),
    ]
    for (slat, slon), track, (elat, elon) in cases:
        points, distance, source, clamped_from = close_out_path(
            slat, slon, track, elat, elon)
        assert distance <= MAX_RIDE_DISTANCE_METERS
        assert distance >= 0
        assert source in {"waypoints", "straight_line",
                          "waypoints_partial", "straight_line_partial"}


def test_a_trackless_ride_longer_than_the_cap_completes_clamped():
    """No waypoints, so start -> end is the whole ride and the leg cap does
    not apply to it (that would silently cap every trackless ride at 3 km).
    The ride cap is what bounds it, and the row says so."""
    _, distance, source, clamped_from = close_out_path(
        _LAT, _LON, [], *_north(500_000))
    assert distance == MAX_RIDE_DISTANCE_METERS
    assert source == "straight_line"
    assert clamped_from > 400_000, "the measurement we clamped away was lost"


def test_a_normal_trackless_ride_is_untouched():
    """The overwhelmingly common case must not acquire a partial marker or
    a clamp record just because the caps exist."""
    _, distance, source, clamped_from = close_out_path(
        _LAT, _LON, [], *_north(4_000))
    assert 3_950 < distance < 4_050, "a 4 km trackless ride is not a 3 km leg"
    assert source == "straight_line"
    assert clamped_from is None


def test_a_believable_final_leg_is_still_measured():
    """The leg cap must not undo the previous commit's fix for every ride —
    a 2 km final leg is a normal sampling gap and still counts."""
    _, distance, source, _ = close_out_path(
        _LAT, _LON, [_north(100)], *_north(2_100))
    assert 2_050 < distance < 2_150
    assert source == "waypoints"


# ---------------------------------------------------------------------------
# A. the 100-point per-ride cap
# ---------------------------------------------------------------------------

class _PointsCursor:
    """Serves the headroom probe, then the INSERT ... RETURNING."""

    def __init__(self, already: int):
        self.already = already
        self.executed: list[tuple[str, tuple]] = []
        self._last = ""

    def execute(self, sql, params=()):
        self._last = " ".join(sql.split())
        self.executed.append((self._last, params))

    def fetchone(self):
        if self._last.startswith("SELECT COALESCE(SUM(points)"):
            return (self.already,)
        return (1, _NOW)

    @property
    def inserts(self) -> list[tuple[str, tuple]]:
        return [e for e in self.executed if e[0].startswith("INSERT INTO user_points")]


def _credit(cur, points: int, action: str = "waypoint"):
    return credit_points(
        cur, account_id=1, action=action, points=points,
        lat=_LAT, lng=_LON, source_table="tracked_rides",
        source_id=str(_RIDE_ID),
    )


def test_a_600_waypoint_ride_is_capped_at_a_hundred():
    """THE VECTOR. 2 points per waypoint, unbounded, meant a 600-waypoint
    ride paid 1200 — and waypoint count is whatever the rider's phone
    posted."""
    cur = _PointsCursor(already=0)
    result = credit_waypoint_points(
        cur, account_id=1, vehicle_identifier="aaaa000000000000",
        waypoint_count=600, end_lat=_LAT, end_lng=_LON, ride_id=str(_RIDE_ID))
    assert result["points"] == MAX_POINTS_PER_RIDE
    assert POINTS_PER_WAYPOINT * 600 == 1200, "the uncapped award, for the record"


def test_the_ledger_records_the_capped_value_not_the_requested_one():
    """The ledger must never claim more than was granted: SUM(points) over
    it is the only definition of a rider's total, so a row saying 1200 next
    to a grant of 100 makes the total disagree with itself."""
    cur = _PointsCursor(already=0)
    result = _credit(cur, 1200)
    assert result["points"] == 100
    (_, params) = cur.inserts[0]
    assert params[2] == 100, "the INSERT wrote the requested amount, not the cap"


def test_the_cap_binds_at_exactly_one_hundred():
    """A ride awarded exactly the cap is fine and is written in full."""
    cur = _PointsCursor(already=0)
    assert _credit(cur, 100)["points"] == 100
    assert cur.inserts[0][1][2] == 100


def test_a_second_award_gets_only_the_remaining_headroom():
    """The 20-point GBFS validation bonus on a ride whose waypoints already
    took 90 gets 10, not 20 — the cap is across every award for the ride,
    not per award."""
    cur = _PointsCursor(already=90)
    assert _credit(cur, 20, action="gbfs_trip_validated")["points"] == 10


def test_a_ride_already_at_the_cap_writes_no_row_at_all():
    cur = _PointsCursor(already=100)
    assert _credit(cur, 20, action="gbfs_trip_validated") is None
    assert cur.inserts == [], "a zero-point ledger row is still a ledger row"


def test_a_ride_somehow_over_the_cap_grants_nothing_further():
    """Ledger rows written before the cap shipped are left alone (the cap is
    forward-only), but they still consume headroom."""
    cur = _PointsCursor(already=1220)
    assert _credit(cur, 20) is None


def test_the_cap_is_enforced_below_every_award_helper():
    """The point of putting it in credit_points: a future third award for a
    ride is capped without having to know the cap exists.

    already=96 (not 95): sql/053's even-points invariant means a real
    ledger's already-credited sum for a ride is always even (every
    POINTS_* constant is even, and even + even is even), so 95 headroom
    is not a state credit_points can actually reach any more — 4 is."""
    cur = _PointsCursor(already=96)
    result = credit_points(
        cur, account_id=1, action="some_future_ride_award", points=998,
        lat=_LAT, lng=_LON, source_table="tracked_rides", source_id=str(_RIDE_ID))
    assert result["points"] == 4


def test_qr_scan_is_not_a_ride_award_and_keeps_its_hundred():
    """A device scan is not a ride and is deliberately exempt — it is worth
    the whole cap on its own."""
    cur = _PointsCursor(already=0)
    result = credit_points(
        cur, account_id=1, action="qr_scan", points=100,
        lat=_LAT, lng=_LON, vehicle_identifier="aaaa000000000000")
    assert result["points"] == 100
    # No headroom probe: nothing about a scan is per-ride.
    assert not any(e[0].startswith("SELECT COALESCE(SUM(points)") for e in cur.executed)


# ---------------------------------------------------------------------------
# B + C at waypoint append, through the real endpoint
# ---------------------------------------------------------------------------

class _WaypointCursor:
    """Routes each read by the statement that asked for it, so the two
    different fetchall shapes (_ordered_track's triples vs _track_points'
    pairs) can coexist in one fake."""

    def __init__(self, track: list[tuple[datetime, float, float]], status="active",
                 start: tuple[float, float] = (_LAT, _LON)):
        self.track = track
        self.status = status
        self.start = start
        self.executed: list[tuple[str, tuple]] = []
        self._last = ""

    def execute(self, sql, params=()):
        self._last = " ".join(sql.split())
        self.executed.append((self._last, params))

    def fetchall(self):
        if self._last.startswith("SELECT waypoint_at"):
            return list(self.track)
        return [(lat, lon) for _, lat, lon in self.track]

    def fetchone(self):
        if self._last.startswith("SELECT status"):
            return (self.status, *self.start)
        if self._last.startswith("INSERT INTO off_feed_ride_waypoints"):
            return (1, _NOW)
        return self.start

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _waypoint_client(monkeypatch, track, start=(_LAT, _LON)):
    cur = _WaypointCursor(track, start=start)

    class _Conn:
        def cursor(self):
            return cur

        def commit(self):
            pass

    @contextmanager
    def _conn():
        yield _Conn()

    monkeypatch.setattr(api_rides, "connection", _conn)
    monkeypatch.setattr(api_rides, "enforce", lambda c, **kw: None)
    app = FastAPI()
    app.include_router(api_rides.router)
    app.dependency_overrides[require_session] = lambda: SessionUser(
        account_id=1, email="rider@example.com", scopes=("rider",),
        expires_at=_NOW, sliding=True, method="google", token_sha256="x",
    )
    return TestClient(app), cur


def _post_waypoint(client, point, at=_NOW):
    lat, lon = point
    return client.post(f"/api/v1/rides/{_RIDE_ID}/waypoints", json={
        "waypoint_at": at.isoformat(), "lat": lat, "lon": lon,
    })


def test_a_waypoint_over_three_km_from_the_start_is_refused(monkeypatch):
    c, cur = _waypoint_client(monkeypatch, track=[])
    r = _post_waypoint(c, _north(3_500))
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "waypoint_too_far"
    assert not any(e[0].startswith("INSERT INTO off_feed_ride_waypoints")
                   for e in cur.executed), "the bad fix was stored anyway"


def test_a_waypoint_exactly_three_km_out_is_accepted(monkeypatch):
    """Off-by-one guard on the gentle path: the boundary value must not cost
    an honest rider a fix. Anchored at the origin so 3 km is exactly 3 km."""
    c, _ = _waypoint_client(monkeypatch, track=[], start=_ORIGIN)
    assert _post_waypoint(c, _north_of_origin(3_000.0)).status_code == 200


def test_the_opposite_side_of_the_world_waypoint_is_refused(monkeypatch):
    """The vector PR #29 flagged and deferred: two waypoints on opposite
    sides of the world recorded ~15 000 km."""
    c, _ = _waypoint_client(monkeypatch, track=[])
    r = _post_waypoint(c, (-39.74, 75.02))
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "waypoint_too_far"


def test_a_late_arriving_fix_is_checked_against_both_neighbours(monkeypatch):
    """Waypoints arrive out of order (retry, offline buffer flush), so a new
    fix can land in the MIDDLE of the path. Checking only the leg from the
    last-stored fix would miss the leg on the far side of it entirely —
    here the new point is 1 km from its predecessor and 9 km from its
    successor."""
    track = [
        (_NOW, *_north(1_000)),
        (_NOW + timedelta(minutes=10), *_north(11_000)),
    ]
    c, _ = _waypoint_client(monkeypatch, track)
    r = _post_waypoint(c, _north(2_000), at=_NOW + timedelta(minutes=5))
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "waypoint_too_far"


def _long_track(legs: int, spacing: float) -> list[tuple[datetime, float, float]]:
    return [
        (_NOW + timedelta(seconds=30 * i), *_north(spacing * i))
        for i in range(1, legs + 1)
    ]


def test_a_waypoint_that_would_pass_eighty_km_is_refused(monkeypatch):
    """Refusing the next fix is gentler than retroactively rewriting a
    finished ride, and it is the only option that never strands anyone."""
    c, _ = _waypoint_client(monkeypatch, _long_track(27, 2_900))  # 78.3 km
    r = _post_waypoint(c, _north(2_900 * 28), at=_NOW + timedelta(hours=2))
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "ride_distance_cap_reached"


def test_a_ride_just_under_eighty_km_still_accepts_waypoints(monkeypatch):
    """The other half of the off-by-one: the cap must not fire early."""
    c, _ = _waypoint_client(monkeypatch, _long_track(27, 2_900))  # 78.3 km
    r = _post_waypoint(c, _north(2_900 * 27 + 1_000), at=_NOW + timedelta(hours=2))
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Both ride tables enforce identically
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module", [api_rides, api_tracked_rides])
def test_both_ride_modules_reject_the_same_leg(module):
    """src/badges.py sums distance across both tables, so what each is
    willing to record must not depend on which one you are talking to."""
    points = [(_LAT, _LON), _north(3_500)]
    with pytest.raises(Exception) as excinfo:
        module._check_appendable(points, 1)
    assert excinfo.value.detail["error"] == "waypoint_too_far"


@pytest.mark.parametrize("module", [api_rides, api_tracked_rides])
def test_both_ride_modules_accept_the_same_boundary_leg(module):
    module._check_appendable([_ORIGIN, _north_of_origin(3_000.0)], 1)


@pytest.mark.parametrize("module", [api_rides, api_tracked_rides])
def test_both_ride_modules_reject_the_same_over_cap_ride(module):
    points = [(_LAT, _LON)] + [_north(2_900 * i) for i in range(1, 29)]
    with pytest.raises(Exception) as excinfo:
        module._check_appendable(points, len(points) - 1)
    assert excinfo.value.detail["error"] == "ride_distance_cap_reached"
