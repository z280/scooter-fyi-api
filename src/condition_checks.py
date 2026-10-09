"""Rider condition checks (docs/FLEET_REPORTS_PLAN.md §4.4, Phase 1b).

A sticky report needs a way to be cleared by riders, not only by movement or
an admin. A rider standing at the scooter — usually straight after
confirming its features — is shown the vehicle's STANDING negative
rideability reports and asked, for each, "Still a problem? Y/N", then "Did
you do a test ride? Y/N".

WHICH REPORTS ARE ASKED ABOUT. fleet_reports.CONDITION_CHECK_TYPES:
uncleared (src/fleet_reports.py's rules; signed in or anonymous) device
reports of type `inaccessible`, `not_rideable` (with its reason), `damaged`
and `dead_battery` — every negative type minus `not_found`. A standing
`not_found` is never asked: a rider who test-rode the scooter has found it,
so a test-ridden check resolves those automatically (`found`).
`improperly_parked` never appears: it is not a negative report. Map-pin
`negative_reports` rows have no report to resolve and are not listed.

TEST RIDE = NO DISCARDS EVERYTHING. Every condition answer is thrown away:
no answer rows, no report changes, no points. One minimal audit row is kept
(account, vehicle, time, `test_ride = false`) so the reporter view can see
an account that files checks it never rides.

TEST RIDE = YES. "No longer a problem" RESOLVES the report through
fleet_reports.resolve_report — the same write path an admin void uses —
with `resolution_source = 'rider_check'`, the rider's account and the check
id, so the audit always says which kind of resolution it was. "Still a
problem" RECONFIRMS it: `last_reconfirmed_at` / `reconfirm_count` on the
report plus an answer row (who, when) — and it RE-BASELINES the report
(owner, 2026-10-09): `baseline_pending` holds it through the test ride, and
once the ride settles (`settle_baselines`, below) its baseline position and
charge become the vehicle's then, so a later clear needs a NEW >= 100 m move
(plus a charge rise, for a rideability report) from there. Other reports are
untouched and follow the ordinary rules (src/fleet_reports.py).

PROOF OF PRESENCE IS REQUIRED. A false "no longer a problem" un-hides a
vehicle — the mirror image of griefing — so a check must carry one of: a
plate-valid feature confirmation by the same account on the same vehicle in
the last PROOF_FEATURE_REPORT_MAX_AGE, the typed plate, or the scanned QR.

POINTS (src/points.py). At most 50 per check: 10 for a test-ridden check
(whatever the answers), +40 when the feed confirms the ride. Withheld, with
the reason stored on the check, when:
  * `own_reports_only` — every report the check acted on was filed by this
    same account. A rider may correct their own report (it is still
    resolved or reconfirmed), but is never paid for it: otherwise "report
    it, then check it" is a points loop;
  * `cooldown` — already paid for this vehicle in the last 24 hours;
  * `daily_cap` — already paid for 10 checks in the last 24 hours;
  * `no_location` — the vehicle has no position to credit at.

FEED CONFIRMATION (the +40). Run once per ingest cycle, after device_state
is updated (src/cycle.py). A check is CONFIRMED when device_state shows,
within FEED_WINDOW of the submission (either side — "Did you do a test
ride?" is asked in the past tense, so the ride usually starts BEFORE the
form is sent):
  * `reserved`             a rental episode (device_state.rental_started_at)
                           that started inside the window;
  * `moved`                a move after the check (first_observed_at_location
                           advanced past its value at check time) that landed
                           inside the window;
  * `rental_before_check`  the rental the check saw at submission started
                           inside the window;
  * `moved_before_check`   the vehicle's current spot was reached inside
                           the window before the check.
A pending check whose window has closed with none of these is
`unconfirmed`. Feed confirmation is recorded for EVERY test-ridden check —
it is corroboration the reporter view shows — but the +40 is paid only
after a paid 10.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from . import fleet_reports, points
from .pg import connection

log = logging.getLogger(__name__)

#: How far either side of the submission a rental episode or a move counts
#: as the test ride. Wide enough for "unlock, ride round the block, end the
#: ride, answer the form" and for a 2-minute cycle's lag; narrow enough
#: that somebody else's ride an hour later is not this rider's test ride.
FEED_WINDOW = timedelta(minutes=20)
FEED_WINDOW_MINUTES = int(FEED_WINDOW.total_seconds() // 60)

#: How recent a plate-valid feature confirmation must be to prove presence.
PROOF_FEATURE_REPORT_MAX_AGE = timedelta(hours=1)

MAX_ANSWERS = 50

FEED_PENDING = "pending"
FEED_CONFIRMED = "confirmed"
FEED_UNCONFIRMED = "unconfirmed"
FEED_NOT_APPLICABLE = "not_applicable"

OUTCOME_RESOLVED = "resolved"
OUTCOME_RECONFIRMED = "reconfirmed"
OUTCOME_FOUND = "found"
OUTCOME_STALE = "stale"


class CheckError(Exception):
    """A refused check. `status` is the HTTP status, `code` a stable
    machine-readable reason, `extra` any detail fields."""

    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


# ---------------------------------------------------------------------------
# Reading what a rider would be asked
# ---------------------------------------------------------------------------

def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def standing_conditions(
    cur, cycle_id: Any, vehicle_identifier: str, account_id: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(conditions to ask about, not_found reports a test ride resolves),
    each newest-priority first, for one vehicle. Never carries the reporter,
    only whether it is the asking rider's own report."""
    ids = fleet_reports.standing_report_ids(cur, cycle_id, vehicle_identifier)
    if not ids:
        return [], []
    cur.execute(
        """
        SELECT id, report_type, reason, observed_at, reported_at,
               last_reconfirmed_at, reconfirm_count, account_id
          FROM device_reports
         WHERE id = ANY(%s)
        """,
        (ids,),
    )
    by_id = {int(r[0]): r for r in cur.fetchall()}
    asked, found = [], []
    for rid in ids:
        r = by_id.get(rid)
        if r is None:
            continue
        item = {
            "report_id": rid,
            "report_type": r[1],
            "reason": r[2],
            "observed_at": _iso(r[3] or r[4]),
            "reported_at": _iso(r[4]),
            "last_reconfirmed_at": _iso(r[5]),
            "reconfirm_count": int(r[6] or 0),
            "own_report": account_id is not None and r[7] == account_id,
        }
        if r[1] in fleet_reports.CONDITION_CHECK_TYPES:
            asked.append({**item, "auto_resolves": False})
        elif r[1] in fleet_reports.FOUND_ON_CHECK_TYPES:
            found.append({**item, "auto_resolves": True})
    return asked, found


def points_preview(cur, *, account_id: int, vehicle_identifier: str,
                   asked: list[dict], found: list[dict]) -> dict[str, Any]:
    """What a test-ridden check would pay right now, and why not if not.
    Read-only: the blocker query's advisory lock lasts the transaction."""
    reason = None
    acted = asked + found
    if acted and all(c["own_report"] for c in acted):
        reason = "own_reports_only"
    else:
        reason = points.condition_check_points_blocker(
            cur, account_id=account_id, vehicle_identifier=vehicle_identifier)
    return {
        "base": points.POINTS_CONDITION_CHECK,
        "feed_confirmed": points.POINTS_CONDITION_CHECK_FEED_CONFIRMED,
        "max": points.POINTS_CONDITION_CHECK_MAX,
        "eligible": reason is None,
        "withheld_reason": reason,
    }


# ---------------------------------------------------------------------------
# Proof of presence
# ---------------------------------------------------------------------------

def verify_presence(
    cur, *, account_id: int, vehicle_identifier: str, stored_plate: str | None,
    feature_report_id: int | None, submitted_plate: str | None,
    qr_raw_value: str | None,
) -> tuple[str, int | None]:
    """('feature_report' | 'plate' | 'qr', feature_report_id or None), or
    CheckError 422 `presence_not_proven`. The first proof offered that
    holds wins; a failing one does not fall through to a weaker claim
    silently — every offered proof is tried, and only if none holds is the
    check refused."""
    from .api_device_features import normalise_plate
    from .identity import hash_plate
    from .qr import extract_plate

    if feature_report_id is not None:
        cur.execute(
            """
            SELECT 1 FROM device_feature_reports
             WHERE id = %s AND account_id = %s AND vehicle_identifier = %s
               AND plate_valid AND reported_at >= NOW() - %s::interval
            """,
            (feature_report_id, account_id, vehicle_identifier,
             PROOF_FEATURE_REPORT_MAX_AGE),
        )
        if cur.fetchone() is not None:
            return "feature_report", feature_report_id
    if qr_raw_value:
        plate = extract_plate(qr_raw_value)
        if plate and hash_plate(plate) == vehicle_identifier:
            return "qr", None
    if submitted_plate:
        typed = normalise_plate(submitted_plate)
        if typed and stored_plate and normalise_plate(stored_plate) == typed:
            return "plate", None
    raise CheckError(
        422, "presence_not_proven",
        "send a plate-valid feature_report_id from the last hour, the plate, "
        "or the scanned QR of this scooter")


# ---------------------------------------------------------------------------
# Submitting a check
# ---------------------------------------------------------------------------

def submit_check(
    cur, *, cycle_id: Any, account_id: int, vehicle_identifier: str,
    answers: list[tuple[int, bool]], test_ride: bool,
    feature_report_id: int | None, submitted_plate: str | None,
    qr_raw_value: str | None,
) -> dict[str, Any]:
    """Apply one condition check inside the caller's transaction. See the
    module docstring for the rules. Raises CheckError."""
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"condition_check_vehicle:{vehicle_identifier}",),
    )
    cur.execute(
        """
        SELECT vehicle_plate, first_observed_at_location, rental_started_at,
               current_lat, current_lon
          FROM device_state WHERE vehicle_identifier = %s
        """,
        (vehicle_identifier,),
    )
    state = cur.fetchone()
    if state is None:
        raise CheckError(404, "unknown_vehicle", "unknown vehicle_identifier")
    plate, parked_since, rental_started, lat, lon = state

    proof, proof_feature_id = verify_presence(
        cur, account_id=account_id, vehicle_identifier=vehicle_identifier,
        stored_plate=plate, feature_report_id=feature_report_id,
        submitted_plate=submitted_plate, qr_raw_value=qr_raw_value)

    asked, found = standing_conditions(cur, cycle_id, vehicle_identifier, account_id)
    asked_by_id = {c["report_id"]: c for c in asked}

    # Answers must name reports on THIS vehicle. A report that has stopped
    # standing since the GET (it moved, was charged or was resolved) is
    # accepted and recorded as `stale`; one on another vehicle is a client bug.
    answered = dict(answers)
    if answered:
        cur.execute(
            "SELECT id, account_id, report_type FROM device_reports "
            "WHERE id = ANY(%s) AND vehicle_identifier = %s",
            (list(answered), vehicle_identifier),
        )
        known = {int(r[0]): (r[1], r[2]) for r in cur.fetchall()}
        foreign = sorted(set(answered) - set(known))
        if foreign:
            raise CheckError(422, "unknown_report",
                             "answers name reports that are not on this vehicle",
                             report_ids=foreign)
        not_askable = sorted(rid for rid, (_a, t) in known.items()
                             if t not in fleet_reports.CONDITION_CHECK_TYPES)
        if not_askable:
            raise CheckError(422, "not_a_condition",
                             "answers name reports that are not condition-check types",
                             report_ids=not_askable)
    else:
        known = {}

    if not asked and not found:
        raise CheckError(409, "nothing_to_check",
                         "this vehicle has no standing condition to check")

    if not test_ride:
        # DISCARD every answer. The minimal audit row only.
        cur.execute(
            """
            INSERT INTO device_condition_checks (
                vehicle_identifier, account_id, test_ride, proof, feature_report_id,
                feed_status, points_withheld
            ) VALUES (%s, %s, FALSE, %s, %s, %s, 'no_test_ride')
            RETURNING id, submitted_at
            """,
            (vehicle_identifier, account_id, proof, proof_feature_id,
             FEED_NOT_APPLICABLE),
        )
        check_id, submitted_at = cur.fetchone()
        return {
            "check_id": int(check_id),
            "vehicle_identifier": vehicle_identifier,
            "submitted_at": submitted_at.isoformat(),
            "test_ride": False,
            "discarded": True,
            "resolved": [], "reconfirmed": [], "found": [], "stale": [],
            "points_awarded": 0,
            "points_pending": 0,
            "points_withheld_reason": "no_test_ride",
            "feed_status": FEED_NOT_APPLICABLE,
            "feed_window_minutes": FEED_WINDOW_MINUTES,
        }

    missing = sorted(set(asked_by_id) - set(answered))
    if missing:
        raise CheckError(422, "unanswered",
                         "answer every listed condition (re-fetch the list if it changed)",
                         report_ids=missing)

    cur.execute(
        """
        INSERT INTO device_condition_checks (
            vehicle_identifier, account_id, test_ride, proof, feature_report_id,
            parked_since_at_check, rental_started_at_check, lat_at_check, lon_at_check,
            feed_status
        ) VALUES (%s, %s, TRUE, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, submitted_at
        """,
        (vehicle_identifier, account_id, proof, proof_feature_id, parked_since,
         rental_started, lat, lon, FEED_PENDING),
    )
    check_id, submitted_at = cur.fetchone()
    check_id = int(check_id)

    outcome: dict[str, list[int]] = {OUTCOME_RESOLVED: [], OUTCOME_RECONFIRMED: [],
                                     OUTCOME_FOUND: [], OUTCOME_STALE: []}
    acted_own: list[bool] = []

    def _answer_row(rid: int, still: bool | None, out: str, own: bool) -> None:
        cur.execute(
            """
            INSERT INTO device_condition_check_answers
                (check_id, report_id, still_a_problem, outcome, own_report)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (check_id, rid, still, out, own),
        )
        outcome[out].append(rid)

    for rid, still in sorted(answered.items()):
        own = known[rid][0] == account_id
        if rid not in asked_by_id:
            _answer_row(rid, still, OUTCOME_STALE, own)
            continue
        acted_own.append(own)
        if still:
            # Re-baseline: pending through the test ride, then the vehicle's
            # position and charge once it settles (settle_baselines).
            cur.execute(
                """
                UPDATE device_reports
                   SET last_reconfirmed_at = NOW(),
                       reconfirm_count = reconfirm_count + 1,
                       baseline_pending = TRUE,
                       baseline_check_id = %s
                 WHERE id = %s
                """,
                (check_id, rid),
            )
            _answer_row(rid, True, OUTCOME_RECONFIRMED, own)
        else:
            try:
                fleet_reports.resolve_report(
                    cur, rid, source=fleet_reports.RESOLUTION_SOURCE_RIDER_CHECK,
                    resolution=f"rider condition check #{check_id}: no longer a "
                               "problem (test ride)",
                    account_id=account_id, check_id=check_id)
            except fleet_reports.ReportAlreadyResolved:
                _answer_row(rid, False, OUTCOME_STALE, own)
                continue
            _answer_row(rid, False, OUTCOME_RESOLVED, own)

    for c in found:
        acted_own.append(c["own_report"])
        try:
            fleet_reports.resolve_report(
                cur, c["report_id"], source=fleet_reports.RESOLUTION_SOURCE_RIDER_CHECK,
                resolution=f"rider condition check #{check_id}: found and test-ridden",
                account_id=account_id, check_id=check_id)
        except fleet_reports.ReportAlreadyResolved:
            continue
        _answer_row(c["report_id"], None, OUTCOME_FOUND, c["own_report"])

    # Points: the 10 now, the +40 when the feed agrees.
    withheld: str | None = None
    if acted_own and all(acted_own):
        withheld = "own_reports_only"
    elif lat is None or lon is None:
        withheld = "no_location"
    else:
        withheld = points.condition_check_points_blocker(
            cur, account_id=account_id, vehicle_identifier=vehicle_identifier)
    awarded = 0
    if withheld is None:
        credited = points.credit_condition_check_points(
            cur, account_id=account_id, vehicle_identifier=vehicle_identifier,
            check_id=check_id, lat=float(lat), lng=float(lon))
        awarded = credited["points"] if credited else 0
    cur.execute(
        """
        UPDATE device_condition_checks
           SET reports_resolved = %s, reports_reconfirmed = %s,
               points_base = %s, points_withheld = %s
         WHERE id = %s
        """,
        (len(outcome[OUTCOME_RESOLVED]) + len(outcome[OUTCOME_FOUND]),
         len(outcome[OUTCOME_RECONFIRMED]), awarded, withheld, check_id),
    )
    return {
        "check_id": check_id,
        "vehicle_identifier": vehicle_identifier,
        "submitted_at": submitted_at.isoformat(),
        "test_ride": True,
        "discarded": False,
        "resolved": outcome[OUTCOME_RESOLVED],
        "reconfirmed": outcome[OUTCOME_RECONFIRMED],
        "found": outcome[OUTCOME_FOUND],
        "stale": outcome[OUTCOME_STALE],
        "points_awarded": awarded,
        "points_pending": (points.POINTS_CONDITION_CHECK_FEED_CONFIRMED
                           if awarded else 0),
        "points_withheld_reason": withheld,
        "feed_status": FEED_PENDING,
        "feed_window_minutes": FEED_WINDOW_MINUTES,
    }


# ---------------------------------------------------------------------------
# Feed confirmation, once per ingest cycle
# ---------------------------------------------------------------------------

def feed_signal(
    *, submitted_at: datetime, parked_since_at_check: datetime | None,
    rental_started_at_check: datetime | None, parked_since_now: datetime | None,
    rental_started_now: datetime | None, window: timedelta = FEED_WINDOW,
) -> str | None:
    """Which feed signal confirms a test ride, or None. Pure — the rules in
    the module docstring, with nothing to mock."""
    lo, hi = submitted_at - window, submitted_at + window
    if rental_started_now is not None and lo <= rental_started_now <= hi:
        return "reserved"
    if (parked_since_now is not None and parked_since_at_check is not None
            and parked_since_now > parked_since_at_check
            and parked_since_now <= hi):
        return "moved"
    if rental_started_at_check is not None and lo <= rental_started_at_check <= hi:
        return "rental_before_check"
    if parked_since_at_check is not None and lo <= parked_since_at_check <= submitted_at:
        return "moved_before_check"
    return None


#: A test ride still in a rental this long after the check settles anyway,
#: at the position the rental froze (where it was rented from).
MAX_SETTLE_WAIT = timedelta(hours=2)


def settle_baselines(cur, snapshot_time: datetime) -> int:
    """Re-baseline the reports a test-ridden check reconfirmed (owner,
    2026-10-09: "still a problem" re-baselines that report at the vehicle's
    state after the test ride settles).

    A check settles once its FEED_WINDOW has closed (by the feed's clock) and
    the vehicle is out of any rental — or MAX_SETTLE_WAIT has passed. Each of
    its still-pending reports gets `baseline_lat/lon` = the vehicle's current
    position (device_state, which a rental freezes and its release updates),
    `baseline_range_meters` = its latest charge, `baseline_at` = the
    snapshot, and stops being pending. Only reports this check owns
    (`baseline_check_id`) are touched. Returns how many checks settled."""
    cur.execute(
        """
        SELECT c.id, c.vehicle_identifier, c.submitted_at, ds.rental_started_at
          FROM device_condition_checks c
          LEFT JOIN device_state ds USING (vehicle_identifier)
         WHERE c.test_ride AND c.reports_reconfirmed > 0
           AND c.baseline_settled_at IS NULL
         ORDER BY c.submitted_at
         FOR UPDATE OF c SKIP LOCKED
        """
    )
    settled = 0
    for cid, vid, submitted_at, rental_started in cur.fetchall():
        if snapshot_time < submitted_at + FEED_WINDOW:
            continue
        if rental_started is not None and snapshot_time < submitted_at + MAX_SETTLE_WAIT:
            continue
        cur.execute(
            """
            UPDATE device_reports dr
               SET baseline_lat = ds.current_lat,
                   baseline_lon = ds.current_lon,
                   baseline_range_meters = (
                       SELECT r.current_range_meters FROM raw_telemetry_points r
                        WHERE r.vehicle_identifier = dr.vehicle_identifier
                          AND r.snapshot_time <= %(snap)s
                        ORDER BY r.snapshot_time DESC LIMIT 1),
                   baseline_at = %(snap)s,
                   baseline_pending = FALSE
              FROM device_state ds
             WHERE ds.vehicle_identifier = dr.vehicle_identifier
               AND dr.baseline_check_id = %(cid)s AND dr.baseline_pending
            """,
            {"snap": snapshot_time, "cid": cid},
        )
        cur.execute(
            "UPDATE device_condition_checks SET baseline_settled_at = %s WHERE id = %s",
            (snapshot_time, cid),
        )
        settled += 1
    return settled


def confirm_pending_checks(snapshot_time: datetime) -> dict[str, int]:
    """One cycle's pass: settle pending checks against device_state as the
    cycle just left it. Called from src/cycle.py after
    device_state.update_for_cycle, under the same isolation contract as the
    other derived layers (the caller catches everything).

    `snapshot_time` is the feed's clock, and the window closes against it:
    a cycle that ran late must not expire a check whose confirming rental it
    simply has not seen yet."""
    stats = {"pending": 0, "confirmed": 0, "unconfirmed": 0, "paid": 0,
             "baselines_settled": 0}
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.vehicle_identifier, c.account_id, c.submitted_at,
                       c.parked_since_at_check, c.rental_started_at_check,
                       c.lat_at_check, c.lon_at_check, c.points_base,
                       ds.first_observed_at_location, ds.rental_started_at
                  FROM device_condition_checks c
                  LEFT JOIN device_state ds USING (vehicle_identifier)
                 WHERE c.feed_status = 'pending'
                 ORDER BY c.submitted_at
                 FOR UPDATE OF c SKIP LOCKED
                """
            )
            rows = cur.fetchall()
            stats["pending"] = len(rows)
            for (cid, vid, account_id, submitted_at, parked_at_check,
                 rental_at_check, lat, lon, base, parked_now, rental_now) in rows:
                signal = feed_signal(
                    submitted_at=submitted_at, parked_since_at_check=parked_at_check,
                    rental_started_at_check=rental_at_check,
                    parked_since_now=parked_now, rental_started_now=rental_now)
                if signal is not None:
                    paid = 0
                    if base and account_id is not None and lat is not None and lon is not None:
                        credited = points.credit_condition_check_points(
                            cur, account_id=account_id, vehicle_identifier=vid,
                            check_id=cid, lat=float(lat), lng=float(lon), confirmed=True)
                        paid = credited["points"] if credited else 0
                    cur.execute(
                        """
                        UPDATE device_condition_checks
                           SET feed_status = 'confirmed', feed_signal = %s,
                               feed_checked_at = %s, points_confirmed = %s
                         WHERE id = %s
                        """,
                        (signal, snapshot_time, paid, cid),
                    )
                    stats["confirmed"] += 1
                    stats["paid"] += 1 if paid else 0
                elif snapshot_time > submitted_at + FEED_WINDOW:
                    cur.execute(
                        """
                        UPDATE device_condition_checks
                           SET feed_status = 'unconfirmed', feed_checked_at = %s
                         WHERE id = %s
                        """,
                        (snapshot_time, cid),
                    )
                    stats["unconfirmed"] += 1
            stats["baselines_settled"] = settle_baselines(cur, snapshot_time)
        conn.commit()
    if stats["pending"] or stats["baselines_settled"]:
        log.info("condition checks: %s", stats)
    return stats


# ---------------------------------------------------------------------------
# Reading checks back (dossier, reporter view)
# ---------------------------------------------------------------------------

def checks_for_vehicle(cur, vehicle_identifier: str, limit: int = 100) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT c.id, c.account_id, a.public_username, c.submitted_at, c.test_ride,
               c.proof, c.reports_resolved, c.reports_reconfirmed, c.feed_status,
               c.feed_signal, c.points_base, c.points_confirmed, c.points_withheld,
               COALESCE(json_agg(json_build_object(
                   'report_id', x.report_id, 'still_a_problem', x.still_a_problem,
                   'outcome', x.outcome, 'own_report', x.own_report)
                   ORDER BY x.report_id) FILTER (WHERE x.report_id IS NOT NULL), '[]')
          FROM device_condition_checks c
          LEFT JOIN accounts a ON a.id = c.account_id
          LEFT JOIN device_condition_check_answers x ON x.check_id = c.id
         WHERE c.vehicle_identifier = %s
         GROUP BY c.id, a.public_username
         ORDER BY c.submitted_at DESC
         LIMIT %s
        """,
        (vehicle_identifier, limit),
    )
    return [
        {
            "id": int(r[0]), "account_id": r[1], "public_username": r[2],
            "submitted_at": _iso(r[3]), "test_ride": bool(r[4]), "proof": r[5],
            "reports_resolved": int(r[6]), "reports_reconfirmed": int(r[7]),
            "feed_status": r[8], "feed_signal": r[9],
            "points": int(r[10]) + int(r[11]), "points_withheld": r[12],
            "answers": r[13],
        }
        for r in cur.fetchall()
    ]
