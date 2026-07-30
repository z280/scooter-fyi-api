"""User points ledger (requirement #10; sql/028_user_points.sql).
Append-only; the running total for an account is SUM(points), never
cached.

credit_points() is the single low-level insert primitive every
point-earning code path funnels through — same "one call, can't forget
half the contract" shape as src/ratelimit.py:enforce(). `cur` is an open
psycopg cursor in the caller's transaction; commit is the caller's
responsibility, so a point award lands atomically with whatever action
earned it.

It is also the single enforcement point for the operator's per-ride points
cap (src/ride_limits.py:MAX_POINTS_PER_RIDE): a ride cannot award more than
100 points across every action attributable to it, and the ledger records
the capped amount rather than the requested one. The cap is FORWARD-ONLY —
no ledger row written before it shipped is rewritten or clawed back. The
ledger is append-only and is the record of what riders were actually
granted; retroactively deleting points people earned under the old rules
would be a worse breach of it than the overpayment was.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import h3

from .geo import distance_meters
from .ride_limits import MAX_POINTS_PER_RIDE

log = logging.getLogger(__name__)

# Sources whose (source_table, source_id) identifies A RIDE, and are
# therefore subject to the operator's per-ride points cap. 'rides' is listed
# even though off-feed rides award nothing today (src/api_rides.py awards no
# points anywhere, deliberately) — if that ever changes, the cap already
# covers it rather than being remembered by whoever adds the award.
#
# Anything NOT in this set is uncapped by this mechanism: `qr_scan` is a
# device scan worth 100 on its own, `profile_completion` is per-account, and
# device reports are per-report. None of them is a ride.
_RIDE_SOURCE_TABLES = frozenset({"tracked_rides", "rides"})

# ---------------------------------------------------------------------------
# Point values — single source of truth. Kept in Python, not the
# migration, so a value tweak is a code change, not a migration.
# ---------------------------------------------------------------------------
POINTS_REPORT_NOT_RIDEABLE = 10
POINTS_REPORT_NOT_FOUND = 4
POINTS_REPORT_VEHICLE_ISSUE = 10
POINTS_REPORT_IMPROPER_PARKING = 10
# Per-waypoint, and therefore UNBOUNDED on its own — waypoint count is
# whatever the rider's phone posted. src/ride_limits.py:MAX_POINTS_PER_RIDE
# is what bounds it; see _apply_ride_cap below. Do not re-derive a ceiling
# from this value.
POINTS_PER_WAYPOINT = 2
POINTS_GBFS_TRIP_VALIDATED = 20
# NOT a ride award and NOT subject to the per-ride cap — a device scan is
# its own thing, and it is worth the whole cap on its own by design.
POINTS_QR_SCAN = 100

# TODO(needs-user-input): the source spec gave no point value for
# "complete missing profile information" (item 10's action list). This
# placeholder is a GUESS and MUST be confirmed or replaced before this
# ships — search this constant name before launch.
POINTS_PROFILE_COMPLETION = 10

# --- Ride Mode awards (PLAN_RIDE_MODE_API.md phase A2; values locked by
# RIDE_MODE_OVERHAUL_PLAN.md Decision 6) ------------------------------------
#
# The VALUES land in A1, ahead of the award machinery, on purpose: the whole
# published schedule (GET /api/v1/points/schedule, src/api_points.py) is
# generated from the constants below, and frontend F2's Screen 2 ℹ copy and
# Screen 9 header interpolate it the day they deploy. A2 wires the awards and
# needs no further edits here. Nothing about the numbers waits on the awards.
#
# EVEN-POINTS INVARIANT (owner rule): every point value in this program is
# even, including every formula output. That is why the qualitative nav award
# is 6 and not the owner's original 5 — the correction is the rule working,
# not a typo. Enforced three ways: `CHECK (points % 2 = 0)` on user_points
# (sql/053), an assert in credit_points (A2), and a sweep over every constant
# and every published schedule value in the tests. If you add a value here,
# it is even.
#
# Formulas (A2 owns the implementations; both read distance from the
# track_donations row, and BOTH ROUND UP — the step is "per STARTED km"):
#     battery_contribution = 8 + 2 * ceil(distance_m / 2000)
#     nav_distance_bonus   =     2 * ceil(distance_m / 3000)
POINTS_BATTERY_CONTRIBUTION_BASE = 8
POINTS_BATTERY_CONTRIBUTION_PER_STEP = 2
POINTS_NAV_ROUTE_FEEDBACK = 4
POINTS_NAV_QUALITATIVE = 6      # even-points rule: owner corrected 5 -> 6
POINTS_NAV_DISTANCE_PER_STEP = 2
POINTS_RIDE_SURVEY = 4

# Step sizes for the two distance formulas above. Canonical unit is
# KILOMETRES because that is the unit the rider-facing copy and
# /points/schedule's `step_km` are written in ("+2 points per 2 km"); the
# metre forms are DERIVED so a step can never be retuned in one unit and not
# the other, which is exactly the drift this endpoint exists to prevent.
BATTERY_CONTRIBUTION_STEP_KM = 2
NAV_DISTANCE_STEP_KM = 3
BATTERY_CONTRIBUTION_STEP_METERS = BATTERY_CONTRIBUTION_STEP_KM * 1000
NAV_DISTANCE_STEP_METERS = NAV_DISTANCE_STEP_KM * 1000

# device_reports.report_type -> (user_points.action, points). Single
# source of truth for the mapping, imported by
# src/api_frontend_reports.py rather than duplicated.
#
#   "vehicle not rideable"     -> not_rideable (renamed from failed_unlock
#                                 in sql/037 — broader than "the unlock
#                                 failed": could you ride it or not?)
#   "vehicle not found"        -> not_found (NEW value, sql/029)
#   "vehicle issue"            -> damaged (existing value — closest
#                                 semantic match)
#   "improper vehicle parking" -> improperly_parked (existing value)
#
# dead_battery is intentionally ABSENT: it is not in the points list, and
# this mapping faithfully preserves that asymmetry rather than guessing a
# value for it.
REPORT_TYPE_POINTS: dict[str, tuple[str, int]] = {
    "not_rideable":      ("report_not_rideable", POINTS_REPORT_NOT_RIDEABLE),
    "not_found":         ("report_not_found", POINTS_REPORT_NOT_FOUND),
    "damaged":           ("report_vehicle_issue", POINTS_REPORT_VEHICLE_ISSUE),
    "improperly_parked": ("report_improper_parking", POINTS_REPORT_IMPROPER_PARKING),
}


def h3_8_index_for(lat: float, lng: float) -> int:
    """Same computation as src/ingest.py's h3_8_index, resolution 8 only."""
    return int(h3.latlng_to_cell(lat, lng, 8), 16)


def _apply_ride_cap(
    cur, *, action: str, points: int,
    source_table: str | None, source_id: str | None,
) -> int | None:
    """Points actually creditable for this award under the per-ride cap.

    Returns the (possibly reduced) award, or None when the ride has already
    been paid its full MAX_POINTS_PER_RIDE and there is nothing left to
    grant. Non-ride sources pass through untouched.

    The headroom is computed from the ledger itself — SUM(points) over every
    row already attributed to this ride — rather than from a counter, for
    the same reason the account total is never cached: the ledger is the
    only source of truth, and a second copy of the number is a second thing
    that can be wrong.

    Concurrency: both ride credits run inside the caller's transaction,
    after end_tracked_ride has taken `SELECT ... FOR UPDATE` on the ride
    row, so two concurrent end reports for the same ride serialize behind
    that lock and cannot each read the same headroom. The dedupe index is
    the backstop if they somehow do.
    """
    if source_table not in _RIDE_SOURCE_TABLES or source_id is None:
        return points

    cur.execute(
        """
        SELECT COALESCE(SUM(points), 0) FROM user_points
         WHERE source_table = %s AND source_id = %s
        """,
        (source_table, source_id),
    )
    (already,) = cur.fetchone()
    headroom = MAX_POINTS_PER_RIDE - int(already)

    if headroom <= 0:
        log.info(
            "points: ride cap reached, no-op source=%s/%s action=%s "
            "requested=%d already=%d cap=%d",
            source_table, source_id, action, points, int(already),
            MAX_POINTS_PER_RIDE,
        )
        return None
    if points > headroom:
        log.info(
            "points: ride cap binds source=%s/%s action=%s "
            "requested=%d credited=%d already=%d cap=%d",
            source_table, source_id, action, points, headroom, int(already),
            MAX_POINTS_PER_RIDE,
        )
        return headroom
    return points


def credit_points(
    cur,
    *,
    account_id: int,
    action: str,
    points: int,
    lat: float,
    lng: float,
    vehicle_identifier: str | None = None,
    source_table: str | None = None,
    source_id: str | None = None,
) -> dict[str, Any] | None:
    """Insert one ledger row. Returns None (no-op) when
    (source_table, source_id, action) already has a row
    (idx_user_points_source_dedupe) — an idempotency guard for
    ride-completion credits against retries. source_id is TEXT because
    sources include both device_reports.id (bigint) and tracked_rides.id
    (uuid) — pass either as a plain str(...).

    THIS IS WHERE THE PER-RIDE POINTS CAP IS ENFORCED, and it is the only
    place. Every point-awarding path in the codebase funnels through this
    function, so capping here means a ride cannot exceed
    MAX_POINTS_PER_RIDE no matter how many different awards are attributed
    to it — including an award nobody has written yet. Capping at the call
    sites instead (in credit_waypoint_points and credit_gbfs_validation_points)
    would have left exactly that hole: a third award would have to remember
    to opt in, and the one that forgot would silently break the invariant.

    When the cap binds, the ledger row is written with the CAPPED value,
    not the requested one. The ledger is the record of what was granted, so
    a row claiming more than the rider actually received would make
    SUM(points) — the only definition of a rider's total — disagree with
    itself. A request that lands with zero headroom left writes no row at
    all and returns None, the same shape as the dedupe no-op.
    """
    points = _apply_ride_cap(
        cur, action=action, points=points,
        source_table=source_table, source_id=source_id,
    )
    if points is None:
        return None

    # EVEN-POINTS INVARIANT (RIDE_MODE_OVERHAUL_PLAN.md Decision 6, sql/053).
    # Safe against cap trimming: MAX_POINTS_PER_RIDE (100) and every
    # POINTS_* constant this module defines are even, and even minus even is
    # even, so a trimmed remainder from _apply_ride_cap is always even too.
    # This is the second of three enforcement points (the others: sql/053's
    # `CHECK (points % 2 = 0)` on user_points, and a test sweeping every
    # constant and formula output) — an AssertionError here means a caller
    # requested an odd award, which is a bug in the caller, not in a rider's
    # input.
    assert points % 2 == 0, f"odd points award: action={action} points={points}"

    h3_8 = h3_8_index_for(lat, lng)
    cur.execute(
        """
        INSERT INTO user_points (
            account_id, action, points, lat, lng, h3_8_index,
            vehicle_identifier, source_table, source_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_table, source_id, action)
            WHERE source_table IS NOT NULL AND source_id IS NOT NULL
            DO NOTHING
        RETURNING id, created_at
        """,
        (account_id, action, points, lat, lng, h3_8,
         vehicle_identifier, source_table, source_id),
    )
    row = cur.fetchone()
    if row is None:
        log.info("points: no-op (already credited) source=%s/%s action=%s",
                 source_table, source_id, action)
        return None
    new_id, created_at = row
    log.info("points credited: account=%d action=%s points=%d id=%d",
             account_id, action, points, new_id)
    return {"id": int(new_id), "action": action, "points": points,
            "created_at": created_at.isoformat()}


def credit_report_points(
    cur, *, account_id: int, report_type: str, lat: float | None, lng: float | None,
    vehicle_identifier: str, report_id: int,
) -> dict[str, Any] | None:
    """Called from api_frontend_reports.submit_device_report right after a
    fresh (non-deduped) insert, only for an authenticated caller. Returns
    None for report types outside REPORT_TYPE_POINTS (e.g. dead_battery)
    or when no location is resolvable — every points row requires a real
    lat/lng, so this skips rather than fabricates one."""
    mapping = REPORT_TYPE_POINTS.get(report_type)
    if mapping is None:
        return None
    if lat is None or lng is None:
        log.warning(
            "points: report %d has no resolvable location — skipping "
            "points credit for account=%d action=%s",
            report_id, account_id, report_type,
        )
        return None
    action, points = mapping
    return credit_points(
        cur, account_id=account_id, action=action, points=points,
        lat=lat, lng=lng, vehicle_identifier=vehicle_identifier,
        source_table="device_reports", source_id=str(report_id),
    )


def credit_qr_scan_points(
    cur, *, account_id: int, vehicle_identifier: str, lat: float, lng: float,
) -> dict[str, Any] | None:
    """+100 pts on the FIRST scan of `vehicle_identifier` by THIS account.
    (Read as per-account, not global-first-scanner-only — the latter
    would mean only one rider, ever, out of a whole fleet's worth of
    scanners could earn this bonus for a given device.) Advisory-locks
    (account, vehicle) to close the same check-then-insert TOCTOU window
    src/ratelimit.py's docstring calls out for its own table."""
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"qr_scan:{account_id}:{vehicle_identifier}",),
    )
    cur.execute(
        "SELECT 1 FROM user_points WHERE account_id = %s AND action = 'qr_scan' "
        "AND vehicle_identifier = %s",
        (account_id, vehicle_identifier),
    )
    if cur.fetchone() is not None:
        return None
    return credit_points(
        cur, account_id=account_id, action="qr_scan", points=POINTS_QR_SCAN,
        lat=lat, lng=lng, vehicle_identifier=vehicle_identifier,
    )


def credit_waypoint_points(
    cur, *, account_id: int, vehicle_identifier: str | None,
    waypoint_count: int, end_lat: float, end_lng: float, ride_id: str,
) -> dict[str, Any] | None:
    """Call ONLY after a ride is marked complete (spec: "only counts if
    ride marked complete"). One ledger row per ride, not per waypoint:
    every waypoint is attributed to the ride's FINAL destination location
    (spec: "Attribute points to final destination location"). ride_id is
    tracked_rides.id (a UUID) — pass str(ride_id).

    The award computed here is a REQUEST, not a guarantee: credit_points
    reduces it to whatever the ride has left under MAX_POINTS_PER_RIDE. A
    600-waypoint ride asks for 1200 and is granted 100. Deliberately not
    capped here — see _apply_ride_cap for why the ceiling has one owner."""
    if waypoint_count <= 0:
        return None
    return credit_points(
        cur, account_id=account_id, action="waypoint",
        points=POINTS_PER_WAYPOINT * waypoint_count,
        lat=end_lat, lng=end_lng, vehicle_identifier=vehicle_identifier,
        source_table="tracked_rides", source_id=str(ride_id),
    )


def credit_gbfs_validation_points(
    cur, *, account_id: int, vehicle_identifier: str,
    end_lat: float, end_lng: float,
    reappear_lat: float | None, reappear_lng: float | None,
    ride_id: str, max_meters: float = 20.0,
) -> dict[str, Any] | None:
    """+20 pts when the vehicle reappeared on GBFS within `max_meters` of
    the reported ride end location. The distance check lives HERE (not in
    the ride-lifecycle endpoint) so the 20m threshold has one owner."""
    if reappear_lat is None or reappear_lng is None:
        return None
    if distance_meters(end_lat, end_lng, reappear_lat, reappear_lng) > max_meters:
        return None
    return credit_points(
        cur, account_id=account_id, action="gbfs_trip_validated",
        points=POINTS_GBFS_TRIP_VALIDATED,
        lat=end_lat, lng=end_lng, vehicle_identifier=vehicle_identifier,
        source_table="tracked_rides", source_id=str(ride_id),
    )


def credit_battery_contribution(
    cur, *, account_id: int, vehicle_identifier: str | None,
    distance_m: float, start_lat: float, start_lng: float, ride_id: str,
) -> dict[str, Any] | None:
    """PLAN_RIDE_MODE_API.md phase A2 / RIDE_MODE_OVERHAUL_PLAN.md Decision 6:
    `POINTS_BATTERY_CONTRIBUTION_BASE` plus
    `POINTS_BATTERY_CONTRIBUTION_PER_STEP` for every started
    `BATTERY_CONTRIBUTION_STEP_METERS` of verified track distance, rounded
    UP — `8 + 2 * ceil(distance_m / 2000)`. `distance_m` is the verified
    distance off the `track_donations` row, not a client claim.

    Every PRECONDITION (a verified donation, both start/end batteries
    known, `ride_options.battery_modeling` on, not an own-device ride) is
    the CALLER's to check — the donation handler / `finalize_validation`,
    per the A2 spec — this function is only the formula and the ledger
    write, same division of labor as credit_waypoint_points /
    credit_gbfs_validation_points above.

    lat/lng = the ride's START point (start_lat/start_lng), NOT its end —
    RIDE_MODE_OVERHAUL_PLAN.md's Risk 3 rule for the reshaped awards,
    deliberately unlike the two superseded ride awards above, which file at
    the ride's end.

    source_table='tracked_rides', source_id=str(ride_id): this is what
    makes MAX_POINTS_PER_RIDE (src/ride_limits.py) actually bind via
    _apply_ride_cap/_RIDE_SOURCE_TABLES, and what makes a retried donation
    dedupe against itself through credit_points' ON CONFLICT. Any other
    source_table would silently bypass both."""
    points = POINTS_BATTERY_CONTRIBUTION_BASE + POINTS_BATTERY_CONTRIBUTION_PER_STEP * math.ceil(
        distance_m / BATTERY_CONTRIBUTION_STEP_METERS
    )
    return credit_points(
        cur, account_id=account_id, action="battery_contribution", points=points,
        lat=start_lat, lng=start_lng, vehicle_identifier=vehicle_identifier,
        source_table="tracked_rides", source_id=str(ride_id),
    )


def credit_nav_distance_bonus(
    cur, *, account_id: int, vehicle_identifier: str | None,
    distance_m: float, start_lat: float, start_lng: float, ride_id: str,
) -> dict[str, Any] | None:
    """PLAN_RIDE_MODE_API.md phase A2 / RIDE_MODE_OVERHAUL_PLAN.md Decision 6:
    `POINTS_NAV_DISTANCE_PER_STEP` for every started `NAV_DISTANCE_STEP_METERS`
    of verified track distance, rounded UP — `2 * ceil(distance_m / 3000)`.
    `distance_m` is the same verified `track_donations` distance
    credit_battery_contribution reads; there is no flat base term (a 1 km
    trip earns exactly 2 points, per the owner's copy).

    Same division of labor as credit_battery_contribution:
    `ride_options.nav_improvement` on and a `ride_routes` row existing are
    the CALLER's preconditions (PLAN_RIDE_MODE_API.md phase A3), not
    checked here.

    lat/lng = the ride's START point, same Risk 3 rule as above.
    source_table='tracked_rides', source_id=str(ride_id): same
    per-ride-cap/dedupe reason as credit_battery_contribution — this is the
    award the spec calls out explicitly as a real bug risk if gotten
    wrong."""
    points = POINTS_NAV_DISTANCE_PER_STEP * math.ceil(distance_m / NAV_DISTANCE_STEP_METERS)
    return credit_points(
        cur, account_id=account_id, action="nav_distance_bonus", points=points,
        lat=start_lat, lng=start_lng, vehicle_identifier=vehicle_identifier,
        source_table="tracked_rides", source_id=str(ride_id),
    )


def maybe_credit_profile_completion(cur, account_id: int) -> dict[str, Any] | None:
    """Call after any successful write to the accounts row that could
    newly satisfy profile completion — wired into
    src/api_profile.py:put_profile. Idempotent: re-checks whether this
    account already has a profile_completion row first, so it's safe/cheap
    to call on every profile save speculatively.

    NOTE: accounts.email is not required (nullable since sql/025, a phone
    number satisfies the "has contact info" requirement on its own) and
    accounts.rate_plan has a non-null DEFAULT 'visitor' (sql/012), so in
    practice this criterion set reduces to phone_number + (home or work
    location) unless the rider has also explicitly set an email and
    changed rate_plan away from the default. Flagging in case "pricing
    plan" was meant as "explicitly changed away from the default," which
    would need a behavior this module doesn't currently implement.
    """
    cur.execute(
        "SELECT 1 FROM user_points WHERE account_id = %s AND action = 'profile_completion'",
        (account_id,),
    )
    if cur.fetchone() is not None:
        return None

    cur.execute(
        """
        SELECT email, rate_plan, phone_number, home_lat, home_lng, work_lat, work_lng
        FROM accounts WHERE id = %s
        """,
        (account_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    email, rate_plan, phone_number, home_lat, home_lng, work_lat, work_lng = row
    has_location = (home_lat is not None and home_lng is not None) or \
                   (work_lat is not None and work_lng is not None)
    if not (email and rate_plan and phone_number and has_location):
        return None

    lat, lng = (home_lat, home_lng) if home_lat is not None else (work_lat, work_lng)
    return credit_points(
        cur, account_id=account_id, action="profile_completion",
        points=POINTS_PROFILE_COMPLETION, lat=lat, lng=lng,
    )
