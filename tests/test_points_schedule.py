"""GET /api/v1/points/schedule — the authoritative action -> award map.

This endpoint exists for one reason: rider-facing copy is generated from it,
so a hardcoded "+5 points" string can never contradict what the ledger pays.
These tests defend that property specifically, not the numbers:

  * every action the ledger can record is published (a NEW award that forgets
    to appear here is the drift, and the coverage test below catches it);
  * the values are READ from src/points.py per request, proven by moving a
    constant and watching the payload follow — the failure mode being guarded
    is someone "simplifying" the builder into a dict literal;
  * every published value is EVEN (the owner's even-points invariant), swept
    over flat awards, formula bases and per-step increments alike.

`points.py`'s own award functions are covered by tests/test_points_logic.py.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_points
from src import points

# The five ride-mode actions, spelled as the ledger records them
# (sql/053_ride_mode_points.sql's widened `user_points_action_allowed` list)
# and as the frontend's `RideModePointsAction` union codes against them. Note
# `nav_qualitative_feedback` — the ACTION name is longer than its constant
# (POINTS_NAV_QUALITATIVE), and getting it wrong here means the wizard's
# lookup silently falls back to a baked default.
_RIDE_MODE_ACTIONS = (
    "battery_contribution",
    "nav_route_feedback",
    "nav_qualitative_feedback",
    "nav_distance_bonus",
    "ride_survey",
)

_EXISTING_ACTIONS = (
    "qr_scan",
    "gbfs_trip_validated",
    "waypoint",
    "profile_completion",
    "report_not_rideable",
    "report_not_found",
    "report_vehicle_issue",
    "report_improper_parking",
)

# The three device-feature confirmation tiers (sql/055's widened action
# list). Like the report awards, they are published FROM the mapping that
# decides them (points.FEATURE_STATUS_POINTS) rather than re-listed in the
# endpoint — the coverage test below is what holds that property.
# Awarded since sql/076-078 but unpublished until 2026-10-06.
_GROWTH_ACTIONS = ("referral", "stand_down")

_DEVICE_FEATURE_ACTIONS = (
    "device_features_first",
    "device_features_review",
    "device_features_reconfirm",
)

# Device photos (sql/056). One flat award per accepted upload; the popup's
# "📷 Take Photo" copy reads it from here for the same reason the Confirm
# Features modal does — so the number a rider is promised is the number the
# ledger pays.
_DEVICE_PHOTO_ACTIONS = ("device_photo",)

_FORMULA_ACTIONS = ("battery_contribution", "nav_distance_bonus")


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api_points.router)
    return TestClient(app)


@pytest.fixture
def schedule(client):
    r = client.get("/api/v1/points/schedule")
    assert r.status_code == 200
    return r.json()


# --- shape / access ----------------------------------------------------------

def test_public_no_bearer_required(client):
    """Deliberately unauthenticated, unlike GET /api/v1/points: this is
    published copy, and the wizard reads it before anyone signs in."""
    app = FastAPI()
    app.include_router(api_points.router)
    # No dependency_overrides[require_session] at all — a route that had
    # slipped behind the session dependency would 401 here.
    assert TestClient(app).get("/api/v1/points/schedule").status_code == 200


def test_body_is_the_bare_map_with_no_envelope(schedule):
    """The frontend types this as Record<action, entry> and indexes it
    directly; an added top-level wrapper key would break every lookup."""
    assert "schedule" not in schedule
    assert "actions" not in schedule
    for action, entry in schedule.items():
        assert isinstance(entry, dict), action


def test_cache_header(client):
    r = client.get("/api/v1/points/schedule")
    assert r.headers["Cache-Control"] == "public, max-age=3600"


def test_the_ledger_endpoint_still_needs_a_session(client):
    """Guard against the public schedule route loosening its sibling."""
    assert client.get("/api/v1/points").status_code == 401


# --- coverage ----------------------------------------------------------------

#: Ledger actions that can no longer be earned. Their rows stay valid in
#: user_points (and in the action CHECK); they are just not offered.
#: qr_scan: retired 2026-10-06 with Keep a Scooter (tests/test_retired_routes.py).
_RETIRED_ACTIONS = {"qr_scan"}


def test_every_existing_action_is_published(schedule):
    for action in set(_EXISTING_ACTIONS) - _RETIRED_ACTIONS:
        assert action in schedule, action


def test_retired_actions_are_not_offered(schedule):
    for action in _RETIRED_ACTIONS:
        assert action not in schedule, action


def test_all_five_ride_mode_actions_are_published(schedule):
    """A1 ships the COMPLETE schedule — the five ride-mode awards are
    published before A2 awards any of them, because F2's Screen 2 ℹ copy
    interpolates them on the day it deploys."""
    for action in _RIDE_MODE_ACTIONS:
        assert action in schedule, action


def test_all_three_device_feature_actions_are_published(schedule):
    """The "☑️ Confirm Features" modal interpolates these three into its
    "+N pts" copy the same way Screen 2 interpolates the ride-mode awards —
    so an award tier missing from here is a modal promising a number nobody
    pays."""
    for action in _DEVICE_FEATURE_ACTIONS:
        assert action in schedule, action


def test_the_device_photo_award_is_published(schedule):
    for action in _DEVICE_PHOTO_ACTIONS:
        assert action in schedule, action
    assert schedule["device_photo"]["points"] == points.POINTS_DEVICE_PHOTO


def test_every_device_feature_action_in_the_mapping_is_published(schedule):
    """Generated from FEATURE_STATUS_POINTS, keyed by ACTION rather than by
    the feature_status that selects it — same shape as the report awards."""
    for action, value in points.FEATURE_STATUS_POINTS.values():
        assert schedule[action] == {"points": value}


def test_no_action_is_published_that_the_schedule_does_not_explain(schedule):
    assert set(schedule) == (
        set(_EXISTING_ACTIONS) | set(_RIDE_MODE_ACTIONS)
        | set(_DEVICE_FEATURE_ACTIONS) | set(_DEVICE_PHOTO_ACTIONS)
        | set(_GROWTH_ACTIONS)
    ) - _RETIRED_ACTIONS


def test_every_report_action_in_the_mapping_is_published(schedule):
    """Generated from REPORT_TYPE_POINTS rather than re-listed, so a new
    report type appears in the schedule by existing."""
    for action, value in points.REPORT_TYPE_POINTS.values():
        assert schedule[action] == {"points": value}


#: The two tiered entries (2026-10-06) and their exact keys. The client
#: renders each of these shapes and nothing else.
_TIERED_SHAPES = {
    "nav_qualitative_feedback": {"points", "upper_points", "upper_min_chars"},
    "stand_down": {"points", "new_rider_points"},
}


def test_entries_use_only_the_documented_shapes(schedule):
    """Flat {"points"}, formula {"base","per_step","step_km"}, or one of the
    two named tiered shapes — nothing else, because the client renders
    exactly these."""
    for action, entry in schedule.items():
        if action in _FORMULA_ACTIONS:
            assert set(entry) == {"base", "per_step", "step_km"}, action
        elif action in _TIERED_SHAPES:
            assert set(entry) == _TIERED_SHAPES[action], action
        else:
            assert set(entry) == {"points"}, action
        for key, value in entry.items():
            assert isinstance(value, int) and not isinstance(value, bool), \
                (action, key, value)


# --- values come from the constants -----------------------------------------

def test_flat_values_match_the_constants(schedule):
    assert schedule["gbfs_trip_validated"]["points"] == \
        points.POINTS_GBFS_TRIP_VALIDATED
    assert schedule["waypoint"]["points"] == points.POINTS_PER_WAYPOINT
    assert schedule["profile_completion"]["points"] == \
        points.POINTS_PROFILE_COMPLETION
    assert schedule["nav_route_feedback"]["points"] == \
        points.POINTS_NAV_ROUTE_FEEDBACK
    assert schedule["nav_qualitative_feedback"]["points"] == \
        points.POINTS_NAV_QUALITATIVE
    assert schedule["ride_survey"]["points"] == points.POINTS_RIDE_SURVEY


def test_ride_mode_values_are_the_locked_decision_6_numbers(schedule):
    """Belt and braces on the drift test above: the constants themselves are
    locked by RIDE_MODE_OVERHAUL_PLAN.md Decision 6, so a "harmless" retune
    of one of them has to break a test somewhere. 6, not 5, for the
    qualitative award — that is the even-points correction, not a typo."""
    assert schedule["battery_contribution"] == {
        "base": 8, "per_step": 2, "step_km": 2}
    assert schedule["nav_route_feedback"] == {"points": 4}
    # Tiered by the owner 2026-10-06: 6, or 12 for 60+ characters.
    assert schedule["nav_qualitative_feedback"] == {
        "points": 6, "upper_points": 12, "upper_min_chars": 60}
    assert schedule["nav_distance_bonus"] == {
        "base": 0, "per_step": 2, "step_km": 3}
    assert schedule["ride_survey"] == {"points": 4}


def test_battery_formula_fields_match_the_constants(schedule):
    entry = schedule["battery_contribution"]
    assert entry["base"] == points.POINTS_BATTERY_CONTRIBUTION_BASE
    assert entry["per_step"] == points.POINTS_BATTERY_CONTRIBUTION_PER_STEP
    assert entry["step_km"] == points.BATTERY_CONTRIBUTION_STEP_KM


def test_nav_distance_formula_fields_match_the_constants(schedule):
    entry = schedule["nav_distance_bonus"]
    assert entry["per_step"] == points.POINTS_NAV_DISTANCE_PER_STEP
    assert entry["step_km"] == points.NAV_DISTANCE_STEP_KM
    # Purely per-step: stated as 0 rather than omitted so a client computing
    # base + per_step * steps gets a number instead of undefined.
    assert entry["base"] == 0


@pytest.mark.parametrize(
    "constant, action, field",
    [
        ("POINTS_RIDE_SURVEY", "ride_survey", "points"),
        ("POINTS_NAV_QUALITATIVE", "nav_qualitative_feedback", "points"),
        ("POINTS_NAV_ROUTE_FEEDBACK", "nav_route_feedback", "points"),
        ("POINTS_BATTERY_CONTRIBUTION_BASE", "battery_contribution", "base"),
        ("POINTS_BATTERY_CONTRIBUTION_PER_STEP", "battery_contribution",
         "per_step"),
        ("BATTERY_CONTRIBUTION_STEP_KM", "battery_contribution", "step_km"),
        ("POINTS_NAV_DISTANCE_PER_STEP", "nav_distance_bonus", "per_step"),
        ("NAV_DISTANCE_STEP_KM", "nav_distance_bonus", "step_km"),
    ],
)
def test_the_payload_follows_the_constant(client, monkeypatch, constant,
                                          action, field):
    """THE point of this endpoint. Move the constant, the payload moves — so
    the copy the rider reads cannot be a stale literal in a handler. Values
    are deliberately absurd (and even) so no default could coincide."""
    sentinel = 424242
    monkeypatch.setattr(points, constant, sentinel)
    body = client.get("/api/v1/points/schedule").json()
    assert body[action][field] == sentinel


def test_a_new_report_type_appears_in_the_schedule(client, monkeypatch):
    """Same drift guard for the generated half of the map."""
    mapping = dict(points.REPORT_TYPE_POINTS)
    mapping["brand_new_type"] = ("report_brand_new", 42)
    monkeypatch.setattr(points, "REPORT_TYPE_POINTS", mapping)
    body = client.get("/api/v1/points/schedule").json()
    assert body["report_brand_new"] == {"points": 42}


# --- even-points invariant ---------------------------------------------------

def test_every_published_points_value_is_even(schedule):
    """The owner's even-points invariant, swept over the whole schedule:
    flat awards, formula bases and per-step increments. An odd value here
    would be published as copy before sql/053's `CHECK (points % 2 = 0)` and
    A2's assert in credit_points ever got a chance to reject it — this sweep
    is the earliest of the three enforcement points."""
    checked = 0
    for action, entry in schedule.items():
        for field in ("points", "base", "per_step"):
            if field in entry:
                assert entry[field] % 2 == 0, (action, field, entry[field])
                checked += 1
    # A sweep that silently swept nothing would pass forever.
    assert checked >= len(_EXISTING_ACTIONS) + len(_RIDE_MODE_ACTIONS)


def test_every_formula_output_is_even(schedule):
    """`base + per_step * ceil(km / step_km)` is even for every step count,
    which is what actually reaches the ledger — an even base and an even
    per-step increment cannot sum to an odd award."""
    import math

    for action in _FORMULA_ACTIONS:
        entry = schedule[action]
        for distance_m in (0, 1, 999, 2000, 2001, 3000, 10_000, 80_000):
            steps = math.ceil(distance_m / (entry["step_km"] * 1000))
            award = entry["base"] + entry["per_step"] * steps
            assert award % 2 == 0, (action, distance_m, award)


def test_every_points_constant_in_the_module_is_even():
    """Wider than the schedule: any POINTS_* constant, published or not. A
    new odd award would be caught the moment it is defined, not when someone
    remembers to publish it."""
    for name in dir(points):
        if not name.startswith("POINTS_"):
            continue
        value = getattr(points, name)
        assert isinstance(value, int), name
        assert value % 2 == 0, (name, value)


def test_the_ride_mode_step_constants_agree_in_both_units():
    """km is canonical, metres are derived — A2's formulas divide by the
    metre forms while the published copy quotes the km forms, and a step
    retuned in one unit only would pay for a distance nobody was told
    about."""
    assert points.BATTERY_CONTRIBUTION_STEP_METERS == \
        points.BATTERY_CONTRIBUTION_STEP_KM * 1000
    assert points.NAV_DISTANCE_STEP_METERS == \
        points.NAV_DISTANCE_STEP_KM * 1000
    assert points.BATTERY_CONTRIBUTION_STEP_METERS == 2000
    assert points.NAV_DISTANCE_STEP_METERS == 3000


def test_the_documented_ten_km_worked_example(schedule):
    """RIDE_MODE_OVERHAUL_PLAN.md's own worked case: a 10 km ride with
    everything on is 18 + 18 + 4 = 40 points, under the unchanged
    MAX_POINTS_PER_RIDE of 100. If this drifts, the plan's headline number
    and the shipped schedule disagree."""
    import math

    from src.ride_limits import MAX_POINTS_PER_RIDE

    distance_m = 10_000
    battery = schedule["battery_contribution"]
    nav_dist = schedule["nav_distance_bonus"]
    battery_award = battery["base"] + battery["per_step"] * math.ceil(
        distance_m / (battery["step_km"] * 1000))
    nav_award = (schedule["nav_route_feedback"]["points"]
                 + schedule["nav_qualitative_feedback"]["points"]
                 + nav_dist["per_step"] * math.ceil(
                     distance_m / (nav_dist["step_km"] * 1000)))
    total = battery_award + nav_award + schedule["ride_survey"]["points"]
    assert (battery_award, nav_award, total) == (18, 18, 40)
    assert total < MAX_POINTS_PER_RIDE


def _awardable_actions_from_migrations() -> set[str]:
    """The user_points action CHECK as the latest migration defines it.

    Anchored on the constraint's NAME, `user_points_action_allowed`, so an
    `action IN (...)` on some other table, or in a comment, cannot replace
    the ledger set. Fails loudly if a migration names the constraint but its
    action list cannot be parsed (e.g. rewritten as `= ANY (ARRAY[...])`),
    rather than silently keeping a stale set."""
    import re
    from pathlib import Path
    latest: set[str] = set()
    pattern = re.compile(
        r"CONSTRAINT\s+user_points_action_allowed\s+CHECK\s*\(\s*action\s+IN\s*\(([^)]*)\)",
        re.I | re.S,
    )
    for f in sorted((Path(__file__).resolve().parents[1] / "sql").glob("*.sql")):
        # Drop SQL comments first: a comment can mention the constraint, and
        # 052 has one with parentheses inside the action list itself.
        text = re.sub(r"--[^\n]*", "", f.read_text())
        defines = re.search(r"(ADD\s+)?CONSTRAINT\s+user_points_action_allowed\s+CHECK", text, re.I)
        if not defines:
            continue
        found = [set(re.findall(r"'([a-z_]+)'", m.group(1))) for m in pattern.finditer(text)]
        assert found and found[-1], f"{f.name} defines user_points_action_allowed but its action list did not parse"
        latest = found[-1]
    return latest


def test_every_action_the_database_can_award_is_published(schedule):
    """The table riders read must list everything they can earn. It once
    left out referral and stand_down for months; this reads the source of
    truth (the ledger's CHECK) so a new award cannot ship unpublished."""
    awardable = _awardable_actions_from_migrations()
    assert awardable, "could not find the user_points action CHECK"
    missing = awardable - set(schedule) - _RETIRED_ACTIONS
    assert not missing, f"awardable but unpublished: {sorted(missing)}"


def test_every_published_value_is_even(schedule):
    """The owner's even-points invariant, over every number a rider is shown
    as points (thresholds and step sizes are not points)."""
    for action, entry in schedule.items():
        for key, value in entry.items():
            if key in ("step_km", "upper_min_chars"):
                continue
            assert value % 2 == 0, (action, key, value)
