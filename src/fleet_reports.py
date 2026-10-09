"""Fleet reports: negative reports, how they clear, and the census
(docs/FLEET_REPORTS_PLAN.md; rules rewritten by the owner on 2026-10-09).

THE OWNER'S RULES (2026-10-09), which replace Phase 1's suppression:

  "The logic should be move + battery increase, or going off the map and
  appearing in a new location with a full battery = improved/reset. A move
  of <100m should not reset any negative reported device, ever. Nor should
  simply 24h time. Reports of non-rideability should persist until they are
  verified as resolved by a successful movement and an increase in battery
  (otherwise indicating servicing)."
  "Persist and flag as high risk should be the only result of any report.
  24h fade can apply for anonymous reports, but they should fade into
  unknown risk, not likely ridable. But all scooters with a negative report
  should be labeled as 'high risk', not hidden from the map."
  Location reports: "A 100 m+ move clears them."

NO REPORT HIDES A SCOOTER. The only effect of a negative report is the
vehicle's reliability label. This deliberately overrides the plan's §2.5
("do not overload reliability_tier"): the owner wants the tier to BE the
label. There is no `suppressed` flag any more; /devices/current carries
`negative_report_risk` / `_reason` / `_since` so a card can say WHY it is
high risk, and clients must not hide on any of it.

NEGATIVE REPORT TYPES (NEGATIVE_REPORT_PRIORITY): not_rideable (any reason),
damaged, dead_battery — RIDEABILITY reports — and inaccessible, not_found —
LOCATION reports. improperly_parked is not negative: it changes no label and
is a report to Veo. Map-pin `negative_reports` rows (anonymous, no type) are
treated as anonymous rideability reports.

WHAT A REPORT DOES while it is uncleared:
  * signed in  -> high_risk, with no time limit at all;
  * anonymous  -> high_risk for ANONYMOUS_HIGH_RISK_HOURS (24 h), then
                  "unknown" — never back to "ok" until it is cleared.

HOW A REPORT CLEARS (`uncleared_negative_sql`):
  * rideability: the vehicle is >= CLEAR_MOVE_METERS (100 m, straight line)
    from where it was when reported AND its charge rose by at least
    charge_rise_meters() (5% of a full charge) over the charge then; OR it
    went OFF THE MAP (a device_history stop closed as 'absent', i.e. out of
    the feed longer than device_state.ABSENT_STOP_AFTER) after the report
    and reappeared >= 100 m from where it was last seen with a FULL battery
    (>= FULL_BATTERY_PERCENT, 95%);
  * location: >= 100 m from where it was reported, or reappeared >= 100 m
    from its last-seen spot after going off the map. No battery condition;
  * a move under 100 m never clears anything, and time never clears;
  * verification clears: an admin resolve, or a rider condition check with a
    test ride answering "no longer a problem" (resolution_source);
  * a reconfirmation ("still a problem", test ride) RE-BASELINES the report
    at the vehicle's position and charge once the test ride settles, so a
    later clear needs a NEW 100 m move (plus a rise) from there. While the
    test ride is settling (`baseline_pending`) the report cannot clear.

The baseline is `COALESCE(baseline_*, *_at_report)`: the vehicle's position
and charge when the report was filed (vehicle_lat/lon_at_report,
range_at_report_meters), overridden by a reconfirmation. A report with no
known charge at report time (filed before sql/100, or while the vehicle was
out of the feed) counts a "rise" only when the vehicle now reads full. A
report with no known position cannot clear by moving — only by the off-map
path or verification.

ONE IMPLEMENTATION. The SQL is built here, once, and /devices/current
(api_public), the /h3 aggregate (api_h3), the identify path, the condition
checks and the admin pages all use it; tests/test_reliability_sql_mirrored.py
holds every consumer to the builder.

BROKEN PARTS (feature confirmation's poor_condition) are not reports: they
change no label (§2.9); they count in the export and the dossier.

THE CENSUS (§2.8). Newest arrivals sort on `first_ever_observed_at` ("never
reset"), never `first_observed_at_location` ("reset on movement"). Missing is
`last_observed_at` older than N hours (default 72). Permanently gone is an
admin acknowledgement in `device_census_ack`, which an ingest cycle never
touches; a gone vehicle seen again is surfaced, never silently relisted.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .device_features import FEATURE_PRESENCE_COLUMNS, STATUS_NEEDS_REVIEW
from .quality import full_charge_range_meters

#: Every negative report type, in the order `negative_report_reason` names
#: them when several stand: location reports first (they change what a rider
#: should DO — "don't go in", "it isn't there"), then rideability.
NEGATIVE_REPORT_PRIORITY: tuple[str, ...] = (
    "inaccessible",
    "not_found",
    "not_rideable",
    "damaged",
    "dead_battery",
)
RIDEABILITY_REPORT_TYPES: tuple[str, ...] = ("not_rideable", "damaged", "dead_battery")
LOCATION_REPORT_TYPES: tuple[str, ...] = ("inaccessible", "not_found")

#: Not negative: changes no label (owner, 2026-10-09: "the improperly parked
#: is a report to veo"). Every type is in exactly one of these two tuples.
NON_NEGATIVE_REPORT_TYPES: tuple[str, ...] = ("improperly_parked",)

#: The standing reports a rider at the scooter is asked "Still a problem?"
#: about (Phase 1b): the negative types minus `not_found` — a rider standing
#: at the scooter has found it; FOUND_ON_CHECK_TYPES are resolved by a
#: test-ridden check without asking.
CONDITION_CHECK_TYPES: tuple[str, ...] = (
    "inaccessible",
    "not_rideable",
    "damaged",
    "dead_battery",
)
FOUND_ON_CHECK_TYPES: tuple[str, ...] = ("not_found",)

RESOLUTION_SOURCE_ADMIN = "admin"
RESOLUTION_SOURCE_RIDER_CHECK = "rider_check"

RISK_HIGH = "high_risk"
RISK_UNKNOWN = "unknown"

#: The straight-line move that can clear a report. Under it, nothing clears
#: (owner: "A move of <100m should not reset any negative reported device,
#: ever"). It is also well past the feed's jitter and device_state's 50 m
#: in-place radius.
CLEAR_MOVE_METERS = 100

#: An anonymous report is high risk this long, then fades to unknown.
ANONYMOUS_HIGH_RISK_HOURS = 24

#: How much the charge must RISE over the baseline to count as servicing:
#: 5% of a full charge (~2.3 km of range). The feed's range is frozen while a
#: vehicle sits (battery_model: 99.4% of parked 2-minute steps show no
#: change); a swap or a charge is tens of percent.
CHARGE_RISE_FRACTION = 0.05

#: "Full" for the off-the-map path. The feed's range is an integer percent
#: mapped through a 100-step table (quality.compute_battery_percent), so this
#: is the table's 95% entry, not a guess at a range.
FULL_BATTERY_PERCENT = 95

#: The default missing threshold, in hours (§2.8).
DEFAULT_MISSING_HOURS = 72

#: Broken-part export order.
BROKEN_PART_ORDER: tuple[str, ...] = ("bell", "cup_holder", "basket", "phone_holder")

CENSUS_STATUS_GONE = "gone"
CENSUS_STATUS_NOT_GONE = "not_gone"


def charge_rise_meters() -> int:
    """Metres of range a vehicle must gain over its baseline for the gain to
    count as servicing. Derived from the one definition of a full charge."""
    return int(round(full_charge_range_meters() * CHARGE_RISE_FRACTION))


def full_battery_meters() -> int:
    """The current_range_meters at which the battery readout says
    FULL_BATTERY_PERCENT — read from the same lookup table the percentage
    comes from, so "full" means what the readout means."""
    from .quality import _soc_lut

    lut = _soc_lut()
    return int(lut[round(FULL_BATTERY_PERCENT / 100 * (len(lut) - 1))])


# ---------------------------------------------------------------------------
# Uncleared negative reports — THE one SQL implementation
# ---------------------------------------------------------------------------

def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def uncleared_negative_sql(*, vid: str, current_range: str, now: str,
                           dr_filter: str = "AND dr.resolved_at IS NULL",
                           nr_filter: str = "",
                           include_pins: bool = True) -> str:
    """A SELECT of the uncleared negative reports on the vehicle `vid`.

    Columns: src ('device_reports' | 'negative_reports'), id, report_type,
    reason, reported_at, signed_in, high (true while it makes the vehicle
    high_risk; false once an anonymous report has faded to unknown),
    observed_at (when the rider saw it; reported_at when not given).

    `vid`, `current_range` and `now` are SQL expressions from the caller
    (e.g. `r.vehicle_identifier`, `r.current_range_meters`, `NOW()` or a
    snapshot placeholder); the device_state row must be in scope as `ds`.
    `dr_filter` / `nr_filter` restrict which rows count — the /h3 aggregate
    bounds them by its snapshot. Constants are inlined (code-controlled, never
    user input) so the fragment works under either psycopg placeholder style.
    The rules are the module docstring's.
    """
    m = CLEAR_MOVE_METERS
    rise = charge_rise_meters()
    full = full_battery_meters()
    moved = (f"COALESCE(geo_distance_m(n.base_lat, n.base_lon, "
             f"ds.current_lat, ds.current_lon) >= {m}, FALSE)")
    rose = (f"COALESCE({current_range} >= n.base_range + {rise} "
            f"OR (n.base_range IS NULL AND {current_range} >= {full}), FALSE)")
    is_full = f"COALESCE({current_range} >= {full}, FALSE)"
    off_map = f"""EXISTS (
                SELECT 1 FROM device_history h
                 WHERE h.vehicle_identifier = {vid}
                   AND h.departure_reason = 'absent'
                   AND h.departed_at >= n.base_at
                   AND ds.last_observed_at > h.departed_at
                   AND geo_distance_m(h.lat, h.lon, ds.current_lat, ds.current_lon) >= {m})"""
    pins = f"""
          UNION ALL
          SELECT 'negative_reports', nr.id, 'not_rideable', NULL::text, nr.reported_at,
                 nr.reported_at, FALSE, nr.report_lat, nr.report_lon, NULL::integer, nr.reported_at, FALSE
            FROM negative_reports nr
           WHERE nr.vehicle_identifier = {vid}
             {nr_filter}""" if include_pins else ""
    return f"""
        SELECT n.src, n.id, n.report_type, n.reason, n.reported_at, n.signed_in,
               (n.signed_in OR n.reported_at >= {now}
                    - INTERVAL '{ANONYMOUS_HIGH_RISK_HOURS} hours') AS high,
               n.observed_at
          FROM (
          SELECT 'device_reports' AS src, dr.id, dr.report_type, dr.reason, dr.reported_at,
                 COALESCE(dr.observed_at, dr.reported_at) AS observed_at,
                 dr.account_id IS NOT NULL AS signed_in,
                 COALESCE(dr.baseline_lat, dr.vehicle_lat_at_report) AS base_lat,
                 COALESCE(dr.baseline_lon, dr.vehicle_lon_at_report) AS base_lon,
                 COALESCE(dr.baseline_range_meters, dr.range_at_report_meters) AS base_range,
                 COALESCE(dr.baseline_at, dr.reported_at) AS base_at,
                 dr.baseline_pending AS pending
            FROM device_reports dr
           WHERE dr.vehicle_identifier = {vid}
             AND dr.report_type IN {_in(NEGATIVE_REPORT_PRIORITY)}
             {dr_filter}{pins}
          ) n
         WHERE n.pending OR NOT (
               CASE WHEN n.report_type IN {_in(RIDEABILITY_REPORT_TYPES)}
                    THEN ({moved} AND {rose}) OR ({is_full} AND {off_map})
                    ELSE {moved} OR {off_map}
               END)"""


def negative_state_sql(**kw: Any) -> str:
    """One vehicle's negative-report state as a scalar subquery: 'high'
    (high_risk), 'unknown' (only faded anonymous reports), or NULL (none).
    Embedded in the /devices/current and /h3 SELECTs."""
    return (f"(SELECT CASE WHEN COUNT(*) = 0 THEN NULL "
            f"WHEN bool_or(x.high) THEN 'high' ELSE 'unknown' END "
            f"FROM ({uncleared_negative_sql(**kw)}) x)")


def _fleet_rows_sql() -> str:
    return f"""
        SELECT r.vehicle_identifier, x.src, x.id, x.report_type, x.reason,
               x.reported_at, x.signed_in, x.high, x.observed_at
          FROM raw_telemetry_points r
          LEFT JOIN device_state ds ON ds.vehicle_identifier = r.vehicle_identifier
          CROSS JOIN LATERAL ({uncleared_negative_sql(
              vid="r.vehicle_identifier", current_range="r.current_range_meters",
              now="NOW()")}) x
         WHERE r.cycle_id = %(cycle)s
    """


def _vehicle_rows_sql() -> str:
    """One vehicle, in the feed or not: with no telemetry row its current
    charge is NULL, which proves no rise and no full battery."""
    return f"""
        SELECT v.vid, x.src, x.id, x.report_type, x.reason,
               x.reported_at, x.signed_in, x.high, x.observed_at
          FROM (SELECT %(vid)s::text AS vid) v
          LEFT JOIN device_state ds ON ds.vehicle_identifier = v.vid
          LEFT JOIN raw_telemetry_points r
                 ON r.cycle_id = %(cycle)s AND r.vehicle_identifier = v.vid
          CROSS JOIN LATERAL ({uncleared_negative_sql(
              vid="v.vid", current_range="r.current_range_meters", now="NOW()")}) x
    """


def _priority(t: str) -> int:
    return NEGATIVE_REPORT_PRIORITY.index(t)


def _summarise(rows: list[tuple]) -> dict[str, Any]:
    """Rows for ONE vehicle → its label. The reason is the strongest type
    among the reports that set the risk (the high ones when any are high);
    `since` is the oldest of that type."""
    high = [r for r in rows if r[7]]
    basis = high or rows
    top = min(basis, key=lambda r: (_priority(r[3]), r[5], r[2]))
    # The NEWEST uncleared report, whatever its priority (owner, 2026-10-09:
    # "the most recent report should be displayed on the scooter details
    # tile"): by when it was seen, then filed, then id.
    newest = max(rows, key=lambda r: (r[8] or r[5], r[5], r[2]))
    return {
        "risk": RISK_HIGH if high else RISK_UNKNOWN,
        "latest_report": {
            "report_type": newest[3],
            "reason": newest[4],
            "observed_at": (newest[8] or newest[5]).isoformat(),
            "reported_at": newest[5].isoformat(),
            "anonymous": not newest[6],
        },
        "reason": top[3],
        "reason_detail": top[4],
        "since": min(r[5] for r in basis if r[3] == top[3]),
        "signed_in": any(r[6] for r in basis),
        "needs_condition_check": any(
            r[1] == "device_reports" and r[3] in CONDITION_CHECK_TYPES for r in rows),
    }


def negative_states(cur, cycle_id: Any) -> dict[str, dict[str, Any]]:
    """{vehicle_identifier: {risk, reason, reason_detail, since, signed_in,
    needs_condition_check}} for every vehicle in the cycle with an uncleared
    negative report."""
    cur.execute(_fleet_rows_sql(), {"cycle": cycle_id})
    by: dict[str, list[tuple]] = {}
    for row in cur.fetchall():
        by.setdefault(row[0], []).append(row)
    return {vid: _summarise(rows) for vid, rows in by.items()}


def uncleared_reports_for(cur, cycle_id: Any, vehicle_identifier: str,
                          *, device_reports_only: bool = False) -> list[dict[str, Any]]:
    """The uncleared negative reports on one vehicle, strongest first."""
    cur.execute(_vehicle_rows_sql(), {"cycle": cycle_id, "vid": vehicle_identifier})
    rows = [r for r in cur.fetchall()
            if not device_reports_only or r[1] == "device_reports"]
    rows.sort(key=lambda r: (_priority(r[3]), r[5], r[2]))
    return [
        {"src": r[1], "id": int(r[2]), "report_type": r[3], "reason": r[4],
         "reported_at": r[5], "signed_in": bool(r[6]),
         "risk": RISK_HIGH if r[7] else RISK_UNKNOWN}
        for r in rows
    ]


def reports_stamp(cur) -> str:
    """A cheap fingerprint of every input that changes a vehicle's
    negative-report fields between cycles — a new report or pin, a
    resolution, a reinstatement, a reconfirmation or a settled baseline — so
    the /devices/current ETag changes with them rather than serving a stale
    304 for the rest of the cycle. (An anonymous report crossing its 24 h
    mark mid-cycle still waits for the next cycle; that is at most minutes.)"""
    cur.execute(
        """
        SELECT (SELECT COALESCE(MAX(id), 0) FROM device_reports),
               (SELECT COALESCE(MAX(id), 0) FROM negative_reports),
               (SELECT MAX(GREATEST(resolved_at, reinstated_at, baseline_at,
                                    last_reconfirmed_at)) FROM device_reports)
        """
    )
    row = cur.fetchone() or ()
    return "-".join(str(v) for v in row)


def negative_state_for(cur, cycle_id: Any, vehicle_identifier: str) -> dict[str, Any] | None:
    cur.execute(_vehicle_rows_sql(), {"cycle": cycle_id, "vid": vehicle_identifier})
    rows = cur.fetchall()
    return _summarise(rows) if rows else None


def standing_report_ids(cur, cycle_id: Any, vehicle_identifier: str) -> list[int]:
    """Ids of the uncleared device reports on one vehicle, strongest first."""
    return [r["id"] for r in uncleared_reports_for(
        cur, cycle_id, vehicle_identifier, device_reports_only=True)]


def standing_report_ids_all(cur, cycle_id: Any) -> set[int]:
    """Ids of every uncleared device report on a vehicle in `cycle_id`."""
    cur.execute(_fleet_rows_sql(), {"cycle": cycle_id})
    return {int(r[2]) for r in cur.fetchall() if r[1] == "device_reports"}


def open_reports_for(cur, cycle_id: Any, vehicle_identifier: str) -> list[dict[str, Any]]:
    """The uncleared reports on one vehicle, strongest first — what the
    identify answer shows. Never carries the reporter."""
    return [
        {"report_type": r["report_type"], "reason": r["reason"],
         "reported_at": r["reported_at"].isoformat() if r["reported_at"] else None,
         "risk": r["risk"]}
        for r in uncleared_reports_for(cur, cycle_id, vehicle_identifier)
    ]


# ---------------------------------------------------------------------------
# Resolving a report — one implementation for every resolver
# ---------------------------------------------------------------------------

class ReportNotFound(LookupError):
    """No device report with that id."""


class ReportAlreadyResolved(Exception):
    """The report is resolved already; resolutions are final."""


def resolve_report(
    cur, report_id: int, *, source: str, resolution: str,
    account_id: int | None = None, login: str | None = None,
    check_id: int | None = None,
) -> tuple:
    """Resolve one report, attributed. THE one write path for a resolution:
    the Phase 1 admin endpoint (source 'admin', the admin's account), the
    /admin pages (source 'admin', the GitHub login — that session has no
    rider account) and a rider's condition check (source 'rider_check',
    the rider's account and the check id) all come through here, so the
    audit columns cannot be filled differently by different callers.

    Returns (id, vehicle_identifier, report_type, reported_at, resolved_at).
    Raises ReportNotFound / ReportAlreadyResolved. Final: there is no
    un-resolve, except an admin reinstating a RIDER resolution
    (`reinstate_report`)."""
    if source not in (RESOLUTION_SOURCE_ADMIN, RESOLUTION_SOURCE_RIDER_CHECK):
        raise ValueError(f"unknown resolution source {source!r}")
    cur.execute(
        """
        UPDATE device_reports
           SET resolved_at = NOW(), resolved_by = %s, resolution = %s,
               resolution_source = %s, resolved_by_login = %s,
               resolved_by_check_id = %s
         WHERE id = %s AND resolved_at IS NULL
        RETURNING id, vehicle_identifier, report_type, reported_at, resolved_at
        """,
        (account_id, resolution, source, login, check_id, report_id),
    )
    row = cur.fetchone()
    if row is not None:
        return row
    cur.execute("SELECT 1 FROM device_reports WHERE id = %s", (report_id,))
    if cur.fetchone() is None:
        raise ReportNotFound(report_id)
    raise ReportAlreadyResolved(report_id)


class NotRiderResolved(Exception):
    """Only a rider-check resolution can be reinstated."""


def reinstate_report(cur, report_id: int, *, login: str, reason: str) -> None:
    """Undo a RIDER's resolution (plan §4.4, "Griefing controls"): a false
    "no longer a problem" un-hides a vehicle, and an admin can put the
    report back. An ADMIN's resolution is final and is refused. The rider's
    answer stays in device_condition_check_answers, and who reinstated it
    and why is stamped on the report, so the history is not erased.
    Points already paid for the check are not clawed back (the ledger is
    append-only)."""
    cur.execute(
        """
        UPDATE device_reports
           SET resolved_at = NULL, resolved_by = NULL, resolution = NULL,
               resolution_source = NULL, resolved_by_login = NULL,
               resolved_by_check_id = NULL,
               reinstated_at = NOW(), reinstated_by_login = %s,
               reinstate_reason = %s
         WHERE id = %s AND resolution_source = %s
        RETURNING id
        """,
        (login, reason, report_id, RESOLUTION_SOURCE_RIDER_CHECK),
    )
    if cur.fetchone() is not None:
        return
    cur.execute("SELECT 1 FROM device_reports WHERE id = %s", (report_id,))
    if cur.fetchone() is None:
        raise ReportNotFound(report_id)
    raise NotRiderResolved(report_id)


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
