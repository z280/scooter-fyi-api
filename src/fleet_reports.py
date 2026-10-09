"""Fleet reports: suppression, report holding, and the census
(docs/FLEET_REPORTS_PLAN.md, Phase 1).

TWO AXES, KEPT APART ON PURPOSE (§2.5). `reliability_tier` answers "will it
ride?". `suppressed` answers "should a rider be sent to it at all?". A
scooter behind a fence at 100% rides perfectly; rating it `high_risk` to keep
it off the map would tell the rider something false about the hardware, and
it would re-merge exactly the two questions the plan exists to separate. So
suppression is its own flag with its own reason, computed here, and nothing in
src/quality.py reads it. DO NOT "simplify" the two together.

WHAT SUPPRESSES. A SIGNED-IN, UNRESOLVED device report of a type in
SUPPRESSION_REASON_PRIORITY (every type except improperly_parked), on a vehicle that has not moved since the report
and whose charge has not RISEN since it (`report holds`, below). Anonymous
reports never suppress: they still feed has_negative_report for 24 hours in
their cell, as they always have, but hiding a vehicle from every rider is the
strongest thing this system does and it needs somebody accountable behind it
— that is the griefing control (§2.6(2), risk 2), and it is why the reporter
view an admin needs can be per-account at all.

BROKEN PARTS DO NOT SUPPRESS. A broken bell, cup holder, basket or phone
holder is recorded through feature confirmation (`device_feature_reports.
poor_condition`, folded into `device_state.features_poor_condition` by
src/device_features.py), not through device reports. The scooter still rides
and is still reachable, so it stays on the map; it counts in the advocacy
export (`broken_parts_summary`) and the Phase 2 dossier instead. See the
plan's §2.9.

WHEN A REPORT HOLDS (§2.2-§2.4). Until the vehicle MOVES
(`device_state.first_observed_at_location` is reset by the ingest on any move
past the stationary threshold, so `<= reported_at` means "not moved since"),
or its charge RISES by at least `charge_rise_meters()` over what it read when
the report was filed (`device_reports.range_at_report_meters`). A LEVEL test
("it reads 100%, so somebody charged it") is what this replaced: it cleared a
report on a fully charged scooter the instant it was filed. NULL on either
side of the comparison clears nothing — no evidence of a service visit is not
evidence of one.

THE CENSUS (§2.8). Newest arrivals sort on `first_ever_observed_at` ("never
reset"), never `first_observed_at_location` ("reset on movement"), which would
report every scooter that moved this morning as new. Missing is
`last_observed_at` older than N hours (default 72, from device_state.py's
fleet measurement: 2-12 h absences are the overnight van). Permanently gone is
an admin acknowledgement in `device_census_ack`, which an ingest cycle never
touches; a gone vehicle that is seen again is surfaced, never silently
relisted, and its acknowledgement is not deleted.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .device_features import FEATURE_PRESENCE_COLUMNS, STATUS_NEEDS_REVIEW
from .quality import full_charge_range_meters

#: The report types that suppress, and — when several hold at once — the
#: order `suppressed_reason` reports them in: the reason that most changes
#: what a rider should do comes first. `inaccessible` heads it because it is
#: the one whose copy has to stop somebody climbing a fence (§2.1); `not_found`
#: next, because the rider would walk to a spot with nothing there.
#:
#: `improperly_parked` is deliberately ABSENT (owner, 2026-10-09, overriding
#: the plan's §2.2(2)/§5): "Improperly Parked is not the same as
#: Inaccessible/Can't Find it. The latter should avoid especially if on
#: private property, the improperly parked is a report to veo." A badly
#: parked scooter is reachable and rideable — riding it away even fixes the
#: complaint — so it stays on the map. It is still stored, counted in the
#: admin export and the dossier, and is Veo's to act on.
SUPPRESSION_REASON_PRIORITY: tuple[str, ...] = (
    "inaccessible",
    "not_found",
    "not_rideable",
    "damaged",
    "dead_battery",
)

#: Report types that never suppress. Every type is in exactly one of these
#: two tuples; tests/test_fleet_reports.py holds that.
NON_SUPPRESSING_REPORT_TYPES: tuple[str, ...] = ("improperly_parked",)

#: How much the charge must RISE over the reading at report time before the
#: rise counts as somebody servicing the vehicle. A battery swap or a charge
#: is a jump of tens of percent; the feed's range is otherwise frozen while a
#: vehicle sits (battery_model: 99.4% of parked 2-minute steps show no change)
#: but is not perfectly still, so a bare `>` would let a few metres of jitter
#: clear a report. 5% of a full charge (~2.3 km) is far below any real
#: service visit and far above that noise.
CHARGE_RISE_FRACTION = 0.05

#: The default missing threshold, in hours (§2.8). Not ABSENT_STOP_AFTER (one
#: hour): device_state.py's header measures 8.4% of the fleet absent for under
#: 12 h at any moment — the overnight pulls — and a list that shows them every
#: night is a list nobody reads.
DEFAULT_MISSING_HOURS = 72

#: Broken-part export order: the three the owner named first, then the
#: fourth the feature data also carries.
BROKEN_PART_ORDER: tuple[str, ...] = ("bell", "cup_holder", "basket", "phone_holder")

CENSUS_STATUS_GONE = "gone"
CENSUS_STATUS_NOT_GONE = "not_gone"


def charge_rise_meters() -> int:
    """Metres of range a vehicle must gain over its reading at report time
    for the gain to clear the report. Derived from the one definition of a
    full charge, so it cannot drift from the battery readout."""
    return int(round(full_charge_range_meters() * CHARGE_RISE_FRACTION))


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------

def _open_reports_sql(*, single_vehicle: bool) -> str:
    """Signed-in, unresolved reports that still HOLD, with the telemetry row
    of the given cycle supplying the current range.

    The hold clauses are the accountable has_negative_report branch's, with
    the suppressing types in place of the reliability-type filter, and they are
    mirrored in api_public.py and api_h3.py —
    tests/test_reliability_sql_mirrored.py keeps the copies honest.

    For the whole fleet the telemetry join is INNER: only vehicles in the
    cycle are on /devices/current, so only they need a flag. For one vehicle
    it is LEFT, because the identify path asks about vehicles that have left
    the feed; with no telemetry row the current range is NULL, and a NULL
    clears nothing.
    """
    join = "LEFT JOIN" if single_vehicle else "JOIN"
    vehicle_filter = "AND dr.vehicle_identifier = %(vid)s" if single_vehicle else ""
    return f"""
        SELECT dr.vehicle_identifier, dr.report_type, dr.reported_at, dr.id
          FROM device_reports dr
          {join} raw_telemetry_points r
                 ON r.cycle_id = %(cycle)s
                AND r.vehicle_identifier = dr.vehicle_identifier
          LEFT JOIN device_state ds
                 ON ds.vehicle_identifier = dr.vehicle_identifier
         WHERE dr.account_id IS NOT NULL
           AND dr.resolved_at IS NULL
           AND dr.report_type = ANY(%(types)s::text[])
           {vehicle_filter}
           AND (ds.first_observed_at_location IS NULL
                OR ds.first_observed_at_location <= dr.reported_at)
           AND (dr.range_at_report_meters IS NULL
                OR r.current_range_meters IS NULL
                OR r.current_range_meters < dr.range_at_report_meters + %(rise)s)
         ORDER BY dr.vehicle_identifier,
                  array_position(%(types)s::text[], dr.report_type),
                  dr.reported_at, dr.id
    """


def _params(cycle_id: Any, **extra: Any) -> dict[str, Any]:
    return {
        "cycle": cycle_id,
        "types": list(SUPPRESSION_REASON_PRIORITY),
        "rise": charge_rise_meters(),
        **extra,
    }


def suppressions(cur, cycle_id: Any) -> dict[str, tuple[str, datetime]]:
    """{vehicle_identifier: (suppressed_reason, suppressed_since)} for every
    vehicle in `cycle_id` that a standing report suppresses.

    The reason is the highest-priority type among its standing reports
    (SUPPRESSION_REASON_PRIORITY); `since` is the OLDEST standing report of
    that type — "how long has this been hidden for this reason".
    """
    cur.execute(_open_reports_sql(single_vehicle=False), _params(cycle_id))
    out: dict[str, tuple[str, datetime]] = {}
    for vid, report_type, reported_at, _id in cur.fetchall():
        # Rows arrive ordered by (vehicle, priority, age): the first row per
        # vehicle is the answer. setdefault keeps it.
        out.setdefault(vid, (report_type, reported_at))
    return out


def open_reports_for(cur, cycle_id: Any, vehicle_identifier: str) -> list[dict[str, Any]]:
    """The standing reports on one vehicle, priority first — what the
    identify answer shows a rider standing in front of a hidden scooter.
    Never carries the reporter."""
    cur.execute(_open_reports_sql(single_vehicle=True),
                _params(cycle_id, vid=vehicle_identifier))
    return [
        {"report_type": t, "reported_at": at.isoformat() if at else None}
        for _vid, t, at, _id in cur.fetchall()
    ]


# ---------------------------------------------------------------------------
# Broken parts (feature confirmation's poor_condition)
# ---------------------------------------------------------------------------

BROKEN_PARTS_DEFINITION = (
    "A vehicle counts as having a broken <part> when its feature consensus "
    "(device_state, folded from rider feature confirmations by the "
    "device-features processor: first valid report authoritative, a "
    "disagreeing report opens needs_review, a 2-of-3 vote resolves it) says "
    "the part is present AND lists it in poor_condition, and the vehicle is "
    "not in needs_review. A later report saying the part is fine disagrees "
    "with that consensus and moves the vehicle to needs_review, so those are "
    "counted separately as under_review rather than as broken. The sample is "
    "vehicles the feed carried within the window; vehicles_with_part is the "
    "denominator (consensus says the part is present). The consensus itself "
    "may have been written before the window opened."
)


def broken_parts_summary(cur, *, since: datetime, until: datetime) -> dict[str, Any]:
    """Counts of vehicles with an unresolved broken part, over vehicles the
    feed carried in (since, until]. See BROKEN_PARTS_DEFINITION.

    Column names are interpolated from FEATURE_PRESENCE_COLUMNS, which is
    code-controlled, never user input."""
    selects = []
    for part in BROKEN_PART_ORDER:
        col = FEATURE_PRESENCE_COLUMNS[part]
        poor = f"(ds.{col} IS TRUE AND '{part}' = ANY(ds.features_poor_condition))"
        selects.append(f"COUNT(*) FILTER (WHERE ds.{col} IS TRUE)")
        selects.append(
            f"COUNT(*) FILTER (WHERE {poor} AND ds.feature_status <> %(review)s)")
        selects.append(
            f"COUNT(*) FILTER (WHERE {poor} AND ds.feature_status = %(review)s)")
    answered = " OR ".join(
        f"ds.{FEATURE_PRESENCE_COLUMNS[p]} IS NOT NULL" for p in BROKEN_PART_ORDER)
    cur.execute(
        f"""
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE {answered}),
               {", ".join(selects)}
          FROM device_state ds
         WHERE ds.last_observed_at > %(since)s
           AND ds.last_observed_at <= %(until)s
        """,
        {"since": since, "until": until, "review": STATUS_NEEDS_REVIEW},
    )
    row = cur.fetchone() or (0,) * (2 + 3 * len(BROKEN_PART_ORDER))
    parts = []
    for i, part in enumerate(BROKEN_PART_ORDER):
        with_part, broken, review = row[2 + 3 * i: 5 + 3 * i]
        parts.append({
            "part": part,
            "broken": int(broken or 0),
            "under_review": int(review or 0),
            "vehicles_with_part": int(with_part or 0),
        })
    return {
        "definition": BROKEN_PARTS_DEFINITION,
        "sample": {
            "vehicles_observed": int(row[0] or 0),
            "vehicles_with_feature_answers": int(row[1] or 0),
        },
        "parts": parts,
    }


# ---------------------------------------------------------------------------
# Inaccessible (the advocacy number)
# ---------------------------------------------------------------------------

INACCESSIBLE_DEFINITION = (
    "vehicles_reported: distinct vehicles with at least one inaccessible "
    "report filed in the window that no admin has voided, signed in or not. "
    "still_unmoved: of those, vehicles the current feed still carries, that "
    "have not moved since their FIRST report in the window "
    "(first_observed_at_location <= that report), and whose first report is "
    "at least unmoved_days old. A vehicle that left the feed is not counted "
    "as unmoved: its position is no longer observed. Locations are never "
    "exported (plan §6: the report is about a spot being unreachable, never "
    "about who lives there)."
)


def inaccessible_summary(
    cur, *, cycle_id: Any, since: datetime, until: datetime, unmoved_days: int,
) -> dict[str, Any]:
    cur.execute(
        """
        WITH rep AS (
            SELECT dr.vehicle_identifier,
                   MIN(dr.reported_at) AS first_reported_at,
                   MAX(dr.reported_at) AS last_reported_at,
                   COUNT(*) AS reports,
                   COUNT(*) FILTER (WHERE dr.account_id IS NOT NULL) AS signed_in_reports,
                   COUNT(DISTINCT dr.account_id) AS distinct_accounts
              FROM device_reports dr
             WHERE dr.report_type = 'inaccessible'
               AND dr.resolved_at IS NULL
               AND dr.reported_at > %(since)s
               AND dr.reported_at <= %(until)s
             GROUP BY dr.vehicle_identifier
        )
        SELECT rep.vehicle_identifier, rep.first_reported_at, rep.last_reported_at,
               rep.reports, rep.signed_in_reports, rep.distinct_accounts,
               ds.first_observed_at_location,
               EXISTS (SELECT 1 FROM raw_telemetry_points r
                        WHERE r.cycle_id = %(cycle)s
                          AND r.vehicle_identifier = rep.vehicle_identifier) AS in_feed
          FROM rep
          LEFT JOIN device_state ds USING (vehicle_identifier)
         ORDER BY rep.first_reported_at, rep.vehicle_identifier
        """,
        {"since": since, "until": until, "cycle": cycle_id},
    )
    from . import vehicle_identity

    threshold = timedelta(days=unmoved_days)
    vehicles = []
    for (vid, first_at, last_at, reports, signed_in, accounts,
         parked_since, in_feed) in cur.fetchall():
        unmoved = bool(in_feed) and parked_since is not None and parked_since <= first_at
        vehicles.append({
            "vehicle_identifier": vid,
            "public_name": vehicle_identity.public_name(vid),
            "first_reported_at": first_at.isoformat(),
            "last_reported_at": last_at.isoformat(),
            "reports": int(reports),
            "signed_in_reports": int(signed_in),
            "distinct_reporting_accounts": int(accounts),
            "in_feed": bool(in_feed),
            "unmoved_since_first_report": unmoved,
            "days_since_first_report": round((until - first_at).total_seconds() / 86400, 1),
            "still_unmoved": unmoved and (until - first_at) >= threshold,
        })
    return {
        "definition": INACCESSIBLE_DEFINITION,
        "unmoved_days": unmoved_days,
        "vehicles_reported": len(vehicles),
        "vehicles_reported_signed_in": sum(1 for v in vehicles if v["signed_in_reports"]),
        "reports": sum(v["reports"] for v in vehicles),
        "still_unmoved": sum(1 for v in vehicles if v["still_unmoved"]),
        "vehicles": vehicles,
    }


# ---------------------------------------------------------------------------
# Why not rideable, and when it was seen (owner, 2026-10-09)
# ---------------------------------------------------------------------------

REASONS_DEFINITION = (
    "Device reports filed in the window that no admin has voided, signed in "
    "or not. not_rideable.reasons counts not_rideable reports by the reason "
    "the rider gave; 'unspecified' is a report with none (older clients, or "
    "a skipped question). remapped counts picks of the not_rideable picker's "
    "decoys, which the server re-files: 'cannot_find' as not_found, "
    "'dead_battery' as dead_battery — those reports are counted under their "
    "stored type, not under not_rideable. observed_at is when the rider says "
    "they saw the problem (defaults to the submission time); "
    "median_report_lag_hours is the median of reported_at - observed_at."
)


def _grouped(cur, key_sql: str, where_extra: str, since: datetime,
             until: datetime) -> dict[str, dict[str, Any]]:
    """Reports in (since, until], not voided, grouped by `key_sql` (a
    code-controlled expression), with their observed-date summary."""
    cur.execute(
        f"""
        SELECT {key_sql} AS k,
               COUNT(*),
               COUNT(DISTINCT dr.vehicle_identifier),
               MIN(dr.observed_at), MAX(dr.observed_at),
               percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY EXTRACT(EPOCH FROM (dr.reported_at - dr.observed_at)) / 3600.0
               )
          FROM device_reports dr
         WHERE dr.resolved_at IS NULL
           AND dr.reported_at > %(since)s
           AND dr.reported_at <= %(until)s
           {where_extra}
         GROUP BY 1
        """,
        {"since": since, "until": until},
    )
    out = {}
    for k, reports, vehicles, first, last, lag in cur.fetchall():
        out[k] = {
            "reports": int(reports),
            "vehicles": int(vehicles),
            "oldest_observed_at": first.isoformat() if first else None,
            "newest_observed_at": last.isoformat() if last else None,
            "median_report_lag_hours": round(float(lag), 1) if lag is not None else None,
        }
    return out


_EMPTY_GROUP = {"reports": 0, "vehicles": 0, "oldest_observed_at": None,
                "newest_observed_at": None, "median_report_lag_hours": None}


def reasons_summary(cur, *, since: datetime, until: datetime) -> dict[str, Any]:
    """Counts by type and, for not_rideable, by reason, with the observed
    dates — the export's problem-report block. See REASONS_DEFINITION."""
    from .api_frontend_reports import NOT_RIDEABLE_DECOYS, NOT_RIDEABLE_REASONS, _REPORT_TYPES

    by_type = _grouped(cur, "dr.report_type", "", since, until)
    by_reason = _grouped(cur, "COALESCE(dr.reason, 'unspecified')",
                         "AND dr.report_type = 'not_rideable'", since, until)
    remapped = _grouped(cur, "dr.submitted_reason",
                        "AND dr.submitted_reason IS NOT NULL", since, until)
    return {
        "definition": REASONS_DEFINITION,
        "by_type": [{"report_type": t, **by_type.get(t, _EMPTY_GROUP)}
                    for t in _REPORT_TYPES],
        "not_rideable": {
            "reasons": [{"reason": r, **by_reason.get(r, _EMPTY_GROUP)}
                        for r in NOT_RIDEABLE_REASONS + ("unspecified",)],
        },
        "remapped": {k: remapped.get(k, _EMPTY_GROUP)["reports"]
                     for k in NOT_RIDEABLE_DECOYS},
    }


# ---------------------------------------------------------------------------
# Census
# ---------------------------------------------------------------------------

def reappeared_count(cur) -> int:
    """Vehicles acknowledged gone that the feed has carried since. The count
    every census response carries, so a contradiction is never nowhere."""
    cur.execute(
        """
        SELECT COUNT(*)
          FROM device_census_ack a
          JOIN device_state ds USING (vehicle_identifier)
         WHERE a.status = 'gone'
           AND ds.last_observed_at > a.last_observed_at_ack
        """
    )
    row = cur.fetchone()
    return int(row[0] or 0) if row else 0
