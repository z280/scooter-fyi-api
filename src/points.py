"""User points ledger (requirement #10; sql/028_user_points.sql).
Append-only; the running total for an account is SUM(points), never
cached.

credit_points() is the single low-level insert primitive every
point-earning code path funnels through — same "one call, can't forget
half the contract" shape as src/ratelimit.py:enforce(). `cur` is an open
psycopg cursor in the caller's transaction; commit is the caller's
responsibility, so a point award lands atomically with whatever action
earned it.
"""

from __future__ import annotations

import logging
from typing import Any

import h3

from .geo import distance_meters

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Point values — single source of truth. Kept in Python, not the
# migration, so a value tweak is a code change, not a migration.
# ---------------------------------------------------------------------------
POINTS_REPORT_WONT_START = 10
POINTS_REPORT_NOT_FOUND = 4
POINTS_REPORT_VEHICLE_ISSUE = 10
POINTS_REPORT_IMPROPER_PARKING = 10
POINTS_PER_WAYPOINT = 2
POINTS_GBFS_TRIP_VALIDATED = 20
POINTS_QR_SCAN = 100

# TODO(needs-user-input): the source spec gave no point value for
# "complete missing profile information" (item 10's action list). This
# placeholder is a GUESS and MUST be confirmed or replaced before this
# ships — search this constant name before launch.
POINTS_PROFILE_COMPLETION = 10

# device_reports.report_type -> (user_points.action, points). Single
# source of truth for the mapping, imported by
# src/api_frontend_reports.py rather than duplicated.
#
#   "vehicle will not start"   -> failed_unlock (existing value — this IS
#                                 what failed_unlock already means)
#   "vehicle not found"        -> not_found (NEW value, sql/029)
#   "vehicle issue"            -> damaged (existing value — closest
#                                 semantic match)
#   "improper vehicle parking" -> improperly_parked (existing value)
#
# dead_battery is intentionally ABSENT: it is not in the points list, and
# this mapping faithfully preserves that asymmetry rather than guessing a
# value for it.
REPORT_TYPE_POINTS: dict[str, tuple[str, int]] = {
    "failed_unlock":     ("report_wont_start", POINTS_REPORT_WONT_START),
    "not_found":         ("report_not_found", POINTS_REPORT_NOT_FOUND),
    "damaged":           ("report_vehicle_issue", POINTS_REPORT_VEHICLE_ISSUE),
    "improperly_parked": ("report_improper_parking", POINTS_REPORT_IMPROPER_PARKING),
}


def h3_8_index_for(lat: float, lng: float) -> int:
    """Same computation as src/ingest.py's h3_8_index, resolution 8 only."""
    return int(h3.latlng_to_cell(lat, lng, 8), 16)


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
    (uuid) — pass either as a plain str(...)."""
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
    tracked_rides.id (a UUID) — pass str(ride_id)."""
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
