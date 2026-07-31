"""Ride-mode points reshape: the two distance-formula award functions
(PLAN_RIDE_MODE_API.md phase A2 / RIDE_MODE_OVERHAUL_PLAN.md Decision 6),
`credit_points`'s even-points assert, and sql/053's widened action
vocabulary + `user_points_points_even` CHECK.

Same fake-cursor idiom as tests/test_points_logic.py: a scripted
fetchone() queue standing in for the two statements credit_points issues
(the per-ride cap's headroom probe, then the INSERT ... RETURNING).

WHAT THIS FILE DOES NOT TEST: `ride_options.battery_modeling` /
`nav_improvement` gating, "both batteries known", "not own-device", and
"a ride_routes row exists" are the DONATION HANDLER's preconditions
(src/api_tracked_rides.py's POST .../track, per the A2 spec) — they never
reach credit_battery_contribution / credit_nav_distance_bonus, which see
only a distance already decided to be award-eligible. Mirroring
credit_waypoint_points / credit_gbfs_validation_points's own test file,
this one tests the formula math, the ledger dedupe, and the
source_table/source_id wiring the per-ride cap depends on — not gates
that belong to a different module.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src import points
from src.points import (
    _RIDE_SOURCE_TABLES,
    credit_battery_contribution,
    credit_nav_distance_bonus,
    credit_points,
)

_NOW = datetime.now(timezone.utc)
_RIDE_ID = "ride-uuid-1"
_VID = "aaaa000000000000"
_START = (39.741234, -104.987654)  # deliberately NOT the ride's end point


class _FakeCursor:
    def __init__(self, fetches):
        self._fetches = list(fetches)
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._fetches.pop(0)


def _ample_cursor(returned_id=1):
    """Headroom probe says nothing credited yet on this ride, and the
    INSERT succeeds — the shape every "just check the math" test wants."""
    return _FakeCursor([(0,), (returned_id, _NOW)])


# ---------------------------------------------------------------------------
# Ceil-math tables — literal expected values, not re-derived math.ceil calls,
# so a bug in the formula itself (e.g. floor instead of ceil, or a step-size
# typo) cannot cancel out against an equally-wrong test.
# ---------------------------------------------------------------------------

# (distance_m, expected_points) for 8 + 2 * ceil(distance_m / 2000).
_BATTERY_TABLE = [
    (0, 8),
    (1, 10),
    (1999, 10),
    (2000, 10),       # exact multiple: NOT rounded up an extra step
    (2001, 12),
    (3999, 12),
    (4000, 12),
    (4001, 14),
    (10_000, 18),      # RIDE_MODE_OVERHAUL_PLAN.md's 10 km worked example
    (20_000, 28),
    (79_999, 88),
    (80_000, 88),      # PLAN_RIDE_MODE_API.md's 80 km worked example
    (80_001, 90),
]

# (distance_m, expected_points) for 2 * ceil(distance_m / 3000).
_NAV_DISTANCE_TABLE = [
    (0, 0),
    (1, 2),
    (1000, 2),         # owner's copy: "a 1 km trip gets 2 points"
    (2999, 2),
    (3000, 2),         # exact multiple: NOT rounded up an extra step
    (3001, 4),
    (6000, 4),
    (6001, 6),
    (10_000, 8),       # the 10 km worked example's nav-distance component
    (80_000, 54),      # the 80 km worked example's nav-distance component
]


@pytest.mark.parametrize("distance_m,expected", _BATTERY_TABLE)
def test_battery_contribution_ceil_math(distance_m, expected):
    cur = _ample_cursor()
    result = credit_battery_contribution(
        cur, account_id=1, vehicle_identifier=_VID, distance_m=distance_m,
        start_lat=_START[0], start_lng=_START[1], ride_id=_RIDE_ID,
    )
    assert result["points"] == expected


@pytest.mark.parametrize("distance_m,expected", _NAV_DISTANCE_TABLE)
def test_nav_distance_bonus_ceil_math(distance_m, expected):
    cur = _ample_cursor()
    result = credit_nav_distance_bonus(
        cur, account_id=1, vehicle_identifier=_VID, distance_m=distance_m,
        start_lat=_START[0], start_lng=_START[1], ride_id=_RIDE_ID,
    )
    assert result["points"] == expected


def test_the_10km_worked_example_totals_40():
    """RIDE_MODE_OVERHAUL_PLAN.md's own worked case: a 10 km ride with
    everything on is 18 (battery) + 18 (nav: 4 + 6 + 8) + 4 (survey) = 40.
    nav_route_feedback/nav_qualitative/ride_survey's credit_* functions are
    A3's to add; the constants they will use already exist (A1), so the
    total is checked arithmetically against them rather than by calling
    functions this lane does not own."""
    battery = credit_battery_contribution(
        _ample_cursor(), account_id=1, vehicle_identifier=_VID, distance_m=10_000,
        start_lat=_START[0], start_lng=_START[1], ride_id=_RIDE_ID,
    )
    nav_distance = credit_nav_distance_bonus(
        _ample_cursor(), account_id=1, vehicle_identifier=_VID, distance_m=10_000,
        start_lat=_START[0], start_lng=_START[1], ride_id=_RIDE_ID,
    )
    assert battery["points"] == 18
    assert nav_distance["points"] == 8
    total = (battery["points"] + points.POINTS_NAV_ROUTE_FEEDBACK
             + points.POINTS_NAV_QUALITATIVE + nav_distance["points"]
             + points.POINTS_RIDE_SURVEY)
    assert total == 40


# ---------------------------------------------------------------------------
# Cap interplay — the 80 km / capped-at-100 example, exercised through THIS
# module's own two functions (not a re-derivation of _apply_ride_cap).
# ---------------------------------------------------------------------------

def test_80km_battery_is_credited_in_full_when_nothing_else_has_landed_yet():
    cur = _FakeCursor([(0,), (99, _NOW)])
    result = credit_battery_contribution(
        cur, account_id=1, vehicle_identifier=_VID, distance_m=80_000,
        start_lat=_START[0], start_lng=_START[1], ride_id=_RIDE_ID,
    )
    assert result["points"] == 88


def test_80km_nav_distance_bonus_is_trimmed_by_the_ride_cap():
    """PLAN_RIDE_MODE_API.md's worked example: an 80 km ride requests
    88 (battery) + 64 (nav: 4 + 6 + 54) + 4 (survey) = 156, trimmed to the
    unchanged MAX_POINTS_PER_RIDE = 100. Simulated here as 88 already
    landed on this ride (the battery award above) and 12 headroom left:
    nav_distance_bonus asks for 54 and is credited only 12 — proving the
    trim actually reaches this formula's output via credit_points, not
    just that the formula computes 54 in isolation."""
    cur = _FakeCursor([(88,), (100, _NOW)])
    result = credit_nav_distance_bonus(
        cur, account_id=1, vehicle_identifier=_VID, distance_m=80_000,
        start_lat=_START[0], start_lng=_START[1], ride_id=_RIDE_ID,
    )
    assert result["points"] == 12


def test_no_headroom_left_is_a_noop_not_a_short_row():
    """A ride already at the cap gets no row at all for a further award,
    same shape as the dedupe no-op — never a row claiming more than the
    rider actually received."""
    cur = _FakeCursor([(100,)])
    result = credit_battery_contribution(
        cur, account_id=1, vehicle_identifier=_VID, distance_m=2_000,
        start_lat=_START[0], start_lng=_START[1], ride_id=_RIDE_ID,
    )
    assert result is None


# ---------------------------------------------------------------------------
# Dedupe — a retried donation must not double-award.
# ---------------------------------------------------------------------------

def test_battery_contribution_dedupes_via_the_source_conflict():
    """(source_table, source_id, action) already has a row: the INSERT's
    ON CONFLICT ... DO NOTHING fires, credit_points sees no returned row,
    and this function passes that through as None — a retried donation
    upload must not double-pay."""
    cur = _FakeCursor([(0,), None])
    result = credit_battery_contribution(
        cur, account_id=1, vehicle_identifier=_VID, distance_m=2_000,
        start_lat=_START[0], start_lng=_START[1], ride_id=_RIDE_ID,
    )
    assert result is None


def test_nav_distance_bonus_dedupes_via_the_source_conflict():
    cur = _FakeCursor([(0,), None])
    result = credit_nav_distance_bonus(
        cur, account_id=1, vehicle_identifier=_VID, distance_m=3_000,
        start_lat=_START[0], start_lng=_START[1], ride_id=_RIDE_ID,
    )
    assert result is None


# ---------------------------------------------------------------------------
# source_table / source_id / action / lat-lng wiring — getting this wrong is
# the real bug PLAN_RIDE_MODE_API.md calls out explicitly: any source_table
# other than 'tracked_rides' silently bypasses the per-ride cap entirely.
# ---------------------------------------------------------------------------

def test_battery_contribution_files_source_table_tracked_rides():
    cur = _ample_cursor()
    credit_battery_contribution(
        cur, account_id=5, vehicle_identifier=_VID, distance_m=2_000,
        start_lat=_START[0], start_lng=_START[1], ride_id=_RIDE_ID,
    )
    insert_sql, params = cur.executed[-1]
    assert insert_sql.startswith("INSERT INTO user_points")
    (account_id, action, award_points, lat, lng, h3_8,
     vid, source_table, source_id) = params
    assert action == "battery_contribution"
    assert source_table == "tracked_rides"
    assert source_id == _RIDE_ID
    assert (lat, lng) == _START, "the award must file at the ride's START point"
    assert vid == _VID
    assert account_id == 5


def test_nav_distance_bonus_files_source_table_tracked_rides():
    cur = _ample_cursor()
    credit_nav_distance_bonus(
        cur, account_id=5, vehicle_identifier=_VID, distance_m=3_000,
        start_lat=_START[0], start_lng=_START[1], ride_id=_RIDE_ID,
    )
    insert_sql, params = cur.executed[-1]
    (account_id, action, award_points, lat, lng, h3_8,
     vid, source_table, source_id) = params
    assert action == "nav_distance_bonus"
    assert source_table == "tracked_rides"
    assert source_id == _RIDE_ID
    assert (lat, lng) == _START


def test_ride_id_is_stringified_for_the_source_id():
    """ride_id is tracked_rides.id, a UUID; source_id is TEXT (sources also
    include device_reports.id, a bigint), so the caller's ride_id must
    survive as-is if already a str and not raise if handed a UUID-shaped
    object with a __str__."""
    class _FakeUUID:
        def __str__(self):
            return "not-a-real-uuid-but-str-shaped"

    cur = _ample_cursor()
    credit_battery_contribution(
        cur, account_id=1, vehicle_identifier=_VID, distance_m=2_000,
        start_lat=_START[0], start_lng=_START[1], ride_id=_FakeUUID(),
    )
    _, params = cur.executed[-1]
    assert params[-1] == "not-a-real-uuid-but-str-shaped"
    assert isinstance(params[-1], str)


def test_vehicle_identifier_may_be_none():
    """Defensive: a private/own-device ride has no vehicle_identifier the
    caller could pass, even though in practice these awards never fire for
    one (battery_modeling/nav_improvement are disabled for own-device)."""
    cur = _ample_cursor()
    result = credit_battery_contribution(
        cur, account_id=1, vehicle_identifier=None, distance_m=2_000,
        start_lat=_START[0], start_lng=_START[1], ride_id=_RIDE_ID,
    )
    assert result is not None


# ---------------------------------------------------------------------------
# The regression this whole design depends on: _RIDE_SOURCE_TABLES must
# still contain 'tracked_rides', or every award above silently stops being
# capped no matter how correctly it binds source_table.
# ---------------------------------------------------------------------------

def test_ride_source_tables_still_contains_tracked_rides():
    assert "tracked_rides" in _RIDE_SOURCE_TABLES


# ---------------------------------------------------------------------------
# credit_points: the even-points assert.
# ---------------------------------------------------------------------------

def test_credit_points_asserts_an_odd_award_is_rejected():
    """A hypothetical odd points value must never reach the INSERT — this
    is the SECOND of three enforcement points (sql/053's CHECK is the
    database-level backstop; the schedule/formula sweeps below are the
    first, catching a bad constant before it is ever requested)."""
    cur = _FakeCursor([])
    with pytest.raises(AssertionError):
        credit_points(cur, account_id=1, action="qr_scan", points=5,
                       lat=39.74, lng=-104.99)
    assert cur.executed == [], "an odd award must not reach the database at all"


def test_credit_points_assert_also_catches_an_odd_value_surviving_the_cap():
    """Even if _apply_ride_cap somehow handed back an odd remainder (it
    cannot, by construction — see sql/053's comment — but this pins the
    assert as the backstop regardless of how an odd value would arrive),
    the assert fires before the INSERT, not after."""
    cur = _FakeCursor([(97,)])  # a headroom of 3 out of an (invalid) 100 cap
    with pytest.raises(AssertionError):
        credit_points(cur, account_id=1, action="battery_contribution", points=5,
                       lat=39.74, lng=-104.99,
                       source_table="tracked_rides", source_id=_RIDE_ID)
    insert_calls = [c for c in cur.executed if c[0].startswith("INSERT")]
    assert insert_calls == []


def test_credit_points_accepts_a_normal_even_award():
    """Sanity check alongside the two assert tests above: the assert does
    not reject legitimate even awards."""
    cur = _FakeCursor([(1, _NOW)])
    result = credit_points(cur, account_id=1, action="qr_scan", points=100,
                            lat=39.74, lng=-104.99)
    assert result["points"] == 100


# ---------------------------------------------------------------------------
# Even-points sweep — every POINTS_* constant, and both new formulas across
# a range of distances. Owner's rule (RIDE_MODE_OVERHAUL_PLAN.md Decision
# 6): "Intentionally points should always be even."
# ---------------------------------------------------------------------------

def test_every_points_constant_is_even():
    """ALL of them, not just the five new ones — a future odd constant
    added anywhere in the module is caught here regardless of which phase
    added it."""
    checked = []
    for name in dir(points):
        if not name.startswith("POINTS_"):
            continue
        value = getattr(points, name)
        assert isinstance(value, int) and not isinstance(value, bool), name
        assert value % 2 == 0, (name, value)
        checked.append(name)
    # A sweep that silently iterated over nothing would pass forever.
    assert len(checked) >= 13, checked


@pytest.mark.parametrize("distance_m", [
    0, 1, 500, 999, 1000, 1999, 2000, 2001, 2999, 3000, 3001,
    4000, 5000, 9999, 10_000, 12_345, 50_000, 79_999, 80_000, 123_457,
])
def test_battery_contribution_output_is_always_even(distance_m):
    cur = _ample_cursor()
    result = credit_battery_contribution(
        cur, account_id=1, vehicle_identifier=_VID, distance_m=distance_m,
        start_lat=_START[0], start_lng=_START[1], ride_id=_RIDE_ID,
    )
    assert result["points"] % 2 == 0, (distance_m, result["points"])


@pytest.mark.parametrize("distance_m", [
    0, 1, 500, 999, 1000, 1999, 2000, 2001, 2999, 3000, 3001,
    4000, 5000, 9999, 10_000, 12_345, 50_000, 79_999, 80_000, 123_457,
])
def test_nav_distance_bonus_output_is_always_even(distance_m):
    cur = _ample_cursor()
    result = credit_nav_distance_bonus(
        cur, account_id=1, vehicle_identifier=_VID, distance_m=distance_m,
        start_lat=_START[0], start_lng=_START[1], ride_id=_RIDE_ID,
    )
    assert result["points"] % 2 == 0, (distance_m, result["points"])


def test_battery_contribution_step_constants_agree_with_the_formula():
    """The formula divides by BATTERY_CONTRIBUTION_STEP_METERS, not a
    hardcoded 2000 — this pins that the constant actually IS 2000 so the
    ceil-math table above is testing the real step, not a coincidence."""
    assert points.BATTERY_CONTRIBUTION_STEP_METERS == 2000
    assert points.NAV_DISTANCE_STEP_METERS == 3000
