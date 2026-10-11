"""The reads behind the Fleet admin pages, in one place.

Why this module exists. Each of these queries was written inside a Jinja
route in `api_admin.py`, and each is now wanted twice: once by that page and
once by the JSON endpoint the in-app admin console reads
(`api_fleet_admin.py`). Two copies of the reports queue's five filters would
not stay the same shape for a week — the standing-report maths and the
region-scan paging are both subtle enough that a divergence would show up as
"the console says 3 open, the portal says 4" and nobody would know which was
lying.

So the HTML route and the JSON route call the same function here, and
`tests/test_fleet_admin_json_pg.py` holds them together with parity tests
that assert both surfaces answer from the same fixture.

Everything here takes a cursor and returns plain Python. No HTTP, no
rendering, no connection handling: the caller owns the transaction, because
the HTML routes already do and the JSON ones want the same cursor to also
read the census envelope.

PRIVACY. Reporters are identified by account id and public username only —
never by email. (An admin's own email appears where an admin ACTED, which is
a different thing and lives in the write paths.)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import fleet_reports, vehicle_identity
from .api_public import latest_complete_cycle

#: Rows per page in the reports queue, on both surfaces.
PAGE_SIZE = 50

REGION_LAYER = "neighborhood"

#: The region filter is applied in Python (a report's region is a
#: point-in-polygon on its own coordinates), over at most this many of the
#: newest matching rows. Reported alongside the rows so a caller can say
#: "older than this was not scanned" rather than implying completeness.
REGION_SCAN_LIMIT = 5000

#: Caps on the reporters view, carried over from the HTML route.
REPORTERS_LIMIT = 500
REPORTER_DETAIL_LIMIT = 300


def report_types() -> tuple[str, ...]:
    from .api_frontend_reports import _REPORT_TYPES

    return tuple(_REPORT_TYPES)


def reason_options() -> tuple[str, ...]:
    """The not-rideable reasons, plus the pseudo-reason the queue filters on
    for a not_rideable report that named none."""
    from .api_frontend_reports import NOT_RIDEABLE_REASONS

    return tuple(NOT_RIDEABLE_REASONS) + ("unspecified",)


def region_of(lat: float | None, lon: float | None) -> str | None:
    from . import geo

    if lat is None or lon is None:
        return None
    try:
        return geo.region_for_point(REGION_LAYER, float(lon), float(lat))
    except Exception:  # noqa: BLE001 — a missing layer must not break the page
        return None


def region_names() -> list[str]:
    from . import geo

    try:
        return sorted(geo.region_names(REGION_LAYER))
    except Exception:  # noqa: BLE001
        return []


def report_point(lat, lng, h3_10, ds_lat, ds_lon) -> tuple[float | None, float | None]:
    """A report's position, best source first: its own coordinates, then the
    cell it was filed in, then where the vehicle is now."""
    if lat is not None and lng is not None:
        return float(lat), float(lng)
    if h3_10 is not None:
        import h3

        try:
            c = h3.cell_to_latlng(h3.int_to_str(int(h3_10)))
            return float(c[0]), float(c[1])
        except Exception:  # noqa: BLE001 — a bad stored cell is just "no point"
            pass
    if ds_lat is not None and ds_lon is not None:
        return float(ds_lat), float(ds_lon)
    return None, None


def charge_pct(range_m) -> int | None:
    from .quality import full_charge_range_meters

    if range_m is None:
        return None
    return int(round(100 * float(range_m) / full_charge_range_meters()))


# ---------------------------------------------------------------------------
# 1. Reports queue
# ---------------------------------------------------------------------------

_QUEUE_SQL = """
    SELECT dr.id, dr.vehicle_identifier, ds.vehicle_plate, dr.report_type,
           dr.reason, dr.submitted_reason, dr.observed_at, dr.reported_at,
           dr.account_id, acc.public_username, dr.range_at_report_meters,
           dr.lat, dr.lng, dr.h3_10_index, ds.current_lat, ds.current_lon,
           ds.first_observed_at_location, dr.resolved_at,
           COALESCE(dr.resolution_source,
                    CASE WHEN dr.resolved_at IS NOT NULL THEN 'admin' END),
           dr.resolution, dr.reconfirm_count,
           COALESCE(dr.baseline_at, dr.reported_at), dr.baseline_pending,
           (SELECT COUNT(*) FROM device_reports d2
             WHERE d2.vehicle_identifier = dr.vehicle_identifier
               AND d2.report_type = dr.report_type AND d2.id <> dr.id
               AND d2.reported_at BETWEEN dr.reported_at - INTERVAL '30 minutes'
                                      AND dr.reported_at + INTERVAL '30 minutes'),
           (SELECT COUNT(DISTINCT d3.account_id) FROM device_reports d3
             WHERE d3.vehicle_identifier = dr.vehicle_identifier
               AND d3.id = ANY(%s))
      FROM device_reports dr
      LEFT JOIN device_state ds ON ds.vehicle_identifier = dr.vehicle_identifier
      LEFT JOIN accounts acc ON acc.id = dr.account_id
     WHERE {where}
     ORDER BY dr.reported_at DESC, dr.id DESC
     LIMIT %s OFFSET %s
"""


def reports_queue(
    cur,
    *,
    report_type: str | None = None,
    reason: str | None = None,
    region: str | None = None,
    standing: str | None = None,
    status: str | None = None,
    page: int = 0,
) -> dict[str, Any]:
    """One page of the reports queue.

    `standing` is "yes"/"no"/None and `status` is "open"/"resolved"/None, as
    the page's selects send them.

    The region filter is the awkward one and the reason this is not a single
    SQL statement: a report's region is a point-in-polygon over coordinates
    that may come from the report, its cell, or the vehicle's current
    position, so it cannot be pushed into the WHERE clause. When a region is
    asked for we take the newest REGION_SCAN_LIMIT matching rows, filter them
    in Python and page the result; `scanned` and `scan_limited` say whether
    that window was the whole set.

    Returns {rows, as_of, page, has_next, scanned, scan_limited}.
    """
    where = ["TRUE"]
    params: list[Any] = []
    if report_type:
        where.append("dr.report_type = %s")
        params.append(report_type)
    if reason:
        if reason == "unspecified":
            where.append("dr.report_type = 'not_rideable' AND dr.reason IS NULL")
        else:
            where.append("dr.reason = %s")
            params.append(reason)
    if status == "open":
        where.append("dr.resolved_at IS NULL")
    elif status == "resolved":
        where.append("dr.resolved_at IS NOT NULL")

    cycle_id, snap = latest_complete_cycle(cur)
    standing_ids = fleet_reports.standing_report_ids_all(cur, cycle_id)
    if standing == "yes":
        where.append("dr.id = ANY(%s)")
        params.append(list(standing_ids))
    elif standing == "no":
        where.append("NOT (dr.id = ANY(%s))")
        params.append(list(standing_ids))

    limit = REGION_SCAN_LIMIT if region else PAGE_SIZE + 1
    offset = 0 if region else page * PAGE_SIZE
    cur.execute(
        _QUEUE_SQL.format(where=" AND ".join(where)),
        [list(standing_ids), *params, limit, offset],
    )
    raw = cur.fetchall()

    rows = []
    for r in raw:
        lat, lon = report_point(r[11], r[12], r[13], r[14], r[15])
        reg = region_of(lat, lon)
        if region and reg != region:
            continue
        parked_since = r[16]
        anchor, _pending = r[21], r[22]
        r = r[:21] + r[23:]
        rows.append({
            "id": r[0], "vehicle_identifier": r[1],
            "display_name": vehicle_identity.display_name(r[1], r[2]),
            "report_type": r[3], "reason": r[4], "submitted_reason": r[5],
            "observed_at": r[6], "reported_at": r[7],
            "account_id": r[8], "public_username": r[9],
            "charge_pct_at_report": charge_pct(r[10]),
            "region": reg,
            "moved_since": bool(parked_since and parked_since > anchor),
            "resolved_at": r[17], "resolution_source": r[18], "resolution": r[19],
            "reconfirm_count": r[20],
            "near_duplicates": int(r[21] or 0),
            "standing_accounts": int(r[22] or 0),
            "standing": r[0] in standing_ids,
            "negative_type": r[3] in fleet_reports.NEGATIVE_REPORT_PRIORITY,
            "signed_in": r[8] is not None,
        })

    if region:
        start = page * PAGE_SIZE
        has_next = len(rows) > start + PAGE_SIZE
        rows = rows[start:start + PAGE_SIZE]
    else:
        has_next = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]

    return {
        "rows": rows,
        "as_of": snap,
        "page": page,
        "has_next": has_next,
        "scanned": len(raw),
        # True when the region scan hit its ceiling, so the caller can say
        # "older reports were not scanned" instead of implying none exist.
        "scan_limited": bool(region) and len(raw) >= REGION_SCAN_LIMIT,
    }


# ---------------------------------------------------------------------------
# 2. Reporters
# ---------------------------------------------------------------------------

_REPORTERS_SQL = """
    WITH rep AS (
        SELECT dr.account_id,
               COUNT(*) AS reports,
               COUNT(DISTINCT dr.vehicle_identifier) AS vehicles,
               COUNT(DISTINCT dr.h3_10_index) AS cells,
               COUNT(DISTINCT (dr.reported_at AT TIME ZONE 'America/Denver')::date) AS days_active,
               COUNT(DISTINCT EXTRACT(HOUR FROM dr.reported_at AT TIME ZONE 'America/Denver')) AS hours_of_day,
               MIN(dr.reported_at) AS first_at, MAX(dr.reported_at) AS last_at,
               COUNT(*) FILTER (WHERE dr.resolution_source = 'admin'
                                  OR (dr.resolved_at IS NOT NULL
                                      AND dr.resolution_source IS NULL)) AS voided,
               COUNT(*) FILTER (WHERE dr.resolution_source = 'rider_check') AS rider_resolved,
               {type_cols}
          FROM device_reports dr
         WHERE dr.account_id IS NOT NULL AND dr.reported_at >= %(since)s
         GROUP BY dr.account_id
    ), chk AS (
        SELECT c.account_id,
               COUNT(*) AS checks,
               COUNT(*) FILTER (WHERE NOT c.test_ride) AS no_ride_checks,
               SUM(c.reports_resolved) AS resolutions,
               SUM(c.reports_reconfirmed) AS reconfirmations,
               COUNT(*) FILTER (WHERE c.feed_status = 'confirmed') AS feed_confirmed,
               COUNT(*) FILTER (WHERE c.feed_status = 'unconfirmed') AS feed_unconfirmed
          FROM device_condition_checks c
         WHERE c.account_id IS NOT NULL AND c.submitted_at >= %(since)s
         GROUP BY c.account_id
    )
    SELECT COALESCE(rep.account_id, chk.account_id) AS aid,
           a.public_username, rep.*, chk.*
      FROM rep FULL OUTER JOIN chk ON chk.account_id = rep.account_id
      LEFT JOIN accounts a ON a.id = COALESCE(rep.account_id, chk.account_id)
     ORDER BY COALESCE(rep.reports, 0) + COALESCE(chk.resolutions, 0) DESC
     LIMIT {limit}
"""


def reporters(cur, *, days: int = 30, account_id: int | None = None) -> dict[str, Any]:
    """Per-account report volume and spread, for spotting griefing (§2.6(2)),
    with rider condition-check resolutions alongside: an account resolving
    reports nobody else's rides corroborate is the same signal as one filing
    them.

    With `account_id`, also returns that account's `detail` — its reports, its
    checks, its reports by hour of day, and its spread over H3 resolution 8
    (~0.7 km²: enough to see a cluster, too coarse to name an address).

    Returns {rows, days, since, detail}.
    """
    types = report_types()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    type_cols = ", ".join(
        f"COUNT(*) FILTER (WHERE dr.report_type = '{t}')" for t in types)
    cur.execute(
        _REPORTERS_SQL.format(type_cols=type_cols, limit=REPORTERS_LIMIT),
        {"since": since},
    )
    cols = [c.name for c in cur.description]
    rows = []
    idx = cols.index("rider_resolved") + 1  # rep.* puts the type counts here
    for r in cur.fetchall():
        rec = dict(zip(cols, r))
        rec["by_type"] = dict(zip(types, r[idx:idx + len(types)]))
        rows.append(rec)

    detail = _reporter_detail(cur, account_id, since) if account_id is not None else None
    return {"rows": rows, "days": days, "since": since, "detail": detail}


def _reporter_detail(cur, account_id: int, since: datetime) -> dict[str, Any]:
    cur.execute(
        f"""
        SELECT dr.id, dr.vehicle_identifier, dr.report_type, dr.reason,
               dr.reported_at, dr.resolved_at,
               COALESCE(dr.resolution_source,
                        CASE WHEN dr.resolved_at IS NOT NULL THEN 'admin' END),
               dr.h3_10_index
          FROM device_reports dr
         WHERE dr.account_id = %s AND dr.reported_at >= %s
         ORDER BY dr.reported_at DESC LIMIT {REPORTER_DETAIL_LIMIT}
        """,
        (account_id, since),
    )
    reps = cur.fetchall()
    cur.execute(
        """
        SELECT EXTRACT(HOUR FROM reported_at AT TIME ZONE 'America/Denver')::int,
               COUNT(*)
          FROM device_reports
         WHERE account_id = %s AND reported_at >= %s
         GROUP BY 1 ORDER BY 1
        """,
        (account_id, since),
    )
    by_hour = dict(cur.fetchall())
    cur.execute(
        f"""
        SELECT c.id, c.vehicle_identifier, c.submitted_at, c.test_ride,
               c.reports_resolved, c.reports_reconfirmed, c.feed_status,
               c.points_base + c.points_confirmed, c.points_withheld
          FROM device_condition_checks c
         WHERE c.account_id = %s AND c.submitted_at >= %s
         ORDER BY c.submitted_at DESC LIMIT {REPORTER_DETAIL_LIMIT}
        """,
        (account_id, since),
    )
    checks = cur.fetchall()
    cur.execute("SELECT public_username FROM accounts WHERE id = %s", (account_id,))
    urow = cur.fetchone()

    import h3

    cells: dict[str, int] = {}
    for rr in reps:
        if rr[7] is None:
            continue
        try:
            c8 = h3.cell_to_parent(h3.int_to_str(int(rr[7])), 8)
        except Exception:  # noqa: BLE001
            c8 = "invalid-cell"
        cells[c8] = cells.get(c8, 0) + 1

    return {
        "account_id": account_id,
        "public_username": urow[0] if urow else None,
        "reports": [
            {"id": x[0], "vehicle_identifier": x[1],
             "display_name": vehicle_identity.public_name(x[1]),
             "report_type": x[2], "reason": x[3], "reported_at": x[4],
             "resolved_at": x[5], "resolution_source": x[6]}
            for x in reps],
        "by_hour": [(h, by_hour.get(h, 0)) for h in range(24)],
        "cells": sorted(cells.items(), key=lambda kv: -kv[1]),
        "checks": [
            {"id": x[0], "vehicle_identifier": x[1],
             "display_name": vehicle_identity.public_name(x[1]),
             "submitted_at": x[2], "test_ride": x[3], "resolved": x[4],
             "reconfirmed": x[5], "feed_status": x[6], "points": x[7],
             "withheld": x[8]}
            for x in checks],
    }
