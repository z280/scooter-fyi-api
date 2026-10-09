"""Fleet reports, admin side (docs/FLEET_REPORTS_PLAN.md Phase 1, §2.6, §2.8).

    GET    /api/v1/private/census/arrivals              newest vehicles
    GET    /api/v1/private/census/missing?hours=72      vehicles the feed lost
    GET    /api/v1/private/census/gone                  acknowledged gone
    PUT    /api/v1/private/census/{vid}/ack             acknowledge gone
    DELETE /api/v1/private/census/{vid}/ack             withdraw it
    PUT    /api/v1/private/census/{vid}/note            write the note
    POST   /api/v1/private/reports/{id}/resolve         void / resolve a report
    GET    /api/v1/private/devices/{vid}/reports        one vehicle's dossier data
    GET    /api/v1/private/reports/export               advocacy numbers (JSON/CSV)

Every route is admin-only through `require_admin` — the same rider-session
allowlist gate as the rest of /api/v1/private/* (src/api_private.py), checked
live on every request. These are the JSON endpoints Phase 2's admin pages are
built on; nothing here renders HTML.

"NOW" IS THE SNAPSHOT. Missing hours, days unmoved and the export window are
measured from the newest complete cycle's snapshot_time, not the wall clock:
if ingest stalls for three days, the wall clock would list the whole fleet as
missing, and the list's only job is to be unusual.

Writes record the acting admin's ACCOUNT (never an email copy), ON DELETE SET
NULL, so the audit trail follows the account and leaves with it.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field

from . import fleet_reports, vehicle_identity
from .accounts import SessionUser, require_admin
from .api_public import latest_complete_cycle
from .pg import connection

log = logging.getLogger(__name__)

router = APIRouter()

_VID = Annotated[str, Path(pattern=r"^[0-9a-f]{16}$",
                           description="16-hex vehicle_identifier")]
_MAX_NOTE = 2000
_MAX_RESOLUTION = 500


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _hours(snap: datetime, then: datetime | None) -> float | None:
    return round((snap - then).total_seconds() / 3600.0, 1) if then else None


def _vehicle(row_vid: str, plate: str | None) -> dict[str, Any]:
    """Admin view: the raw plate rides along, as on every /private route."""
    return {
        "vehicle_identifier": row_vid,
        "vehicle_plate": plate,
        "display_name": vehicle_identity.display_name(row_vid, plate),
    }


# ---------------------------------------------------------------------------
# Census reads
# ---------------------------------------------------------------------------

_ACK_COLUMNS = """
    a.status, a.acknowledged_at, a.last_observed_at_ack, a.note, a.note_at,
    acc_ack.email, acc_note.email
"""
_ACK_JOINS = """
    LEFT JOIN device_census_ack a ON a.vehicle_identifier = ds.vehicle_identifier
    LEFT JOIN accounts acc_ack ON acc_ack.id = a.acknowledged_by
    LEFT JOIN accounts acc_note ON acc_note.id = a.note_by
"""


def _ack(row: tuple, last_observed_at: datetime | None) -> dict[str, Any] | None:
    status, ack_at, seen_at_ack, note, note_at, ack_by, note_by = row
    if status is None:
        return None
    gone = status == fleet_reports.CENSUS_STATUS_GONE
    return {
        "status": status,
        "acknowledged_at": _iso(ack_at) if gone else None,
        "acknowledged_by": ack_by if gone else None,
        "last_observed_at_ack": _iso(seen_at_ack) if gone else None,
        # Seen since the admin called it gone. Surfaced, never relisted.
        "reappeared": bool(gone and last_observed_at and seen_at_ack
                           and last_observed_at > seen_at_ack),
        "note": note,
        "note_at": _iso(note_at),
        "note_by": note_by,
    }


def _envelope(cur, snap: datetime, **meta: Any) -> dict[str, Any]:
    return {
        "as_of": _iso(snap),
        # On every census response, so a gone vehicle that came back is
        # never nowhere (§4.1(7)).
        "gone_reappeared_count": fleet_reports.reappeared_count(cur),
        **meta,
    }


@router.get("/api/v1/private/census/arrivals")
def census_arrivals(
    user: SessionUser = Depends(require_admin),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Newest vehicles first, by `first_ever_observed_at` — "never reset".
    NOT first_observed_at_location, which resets on every move and would
    list this morning's relocations as new scooters."""
    with connection() as conn:
        with conn.cursor() as cur:
            _cycle, snap = latest_complete_cycle(cur)
            cur.execute(
                """
                SELECT ds.vehicle_identifier, ds.vehicle_plate,
                       ds.first_ever_observed_at, ds.last_observed_at,
                       ds.current_vehicle_model_name, ds.current_form_factor
                  FROM device_state ds
                 ORDER BY ds.first_ever_observed_at DESC, ds.vehicle_identifier
                 LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cur.fetchall()
            out = _envelope(cur, snap, limit=limit, offset=offset)
    out["devices"] = [
        {
            **_vehicle(vid, plate),
            "first_ever_observed_at": _iso(first),
            "last_observed_at": _iso(last),
            "vehicle_model_name": model,
            "form_factor": ff,
        }
        for vid, plate, first, last, model, ff in rows
    ]
    return out


@router.get("/api/v1/private/census/missing")
def census_missing(
    user: SessionUser = Depends(require_admin),
    hours: float = Query(fleet_reports.DEFAULT_MISSING_HOURS, gt=0, le=24 * 3650,
                         description="Absent at least this long (default 72)"),
    order: Literal["asc", "desc"] = Query(
        "asc", description="asc = longest-missing first (the plan's order)"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Vehicles whose `last_observed_at` is at least `hours` before the
    current snapshot, spanning back indefinitely. Acknowledged-gone vehicles
    are on /gone instead; a note without an acknowledgement rides along."""
    direction = "ASC" if order == "asc" else "DESC"
    with connection() as conn:
        with conn.cursor() as cur:
            _cycle, snap = latest_complete_cycle(cur)
            cutoff = snap - timedelta(hours=hours)
            where = """
                 WHERE ds.last_observed_at <= %(cutoff)s
                   AND (a.status IS NULL OR a.status <> 'gone')
            """
            cur.execute(
                f"SELECT COUNT(*) FROM device_state ds {_ACK_JOINS} {where}",
                {"cutoff": cutoff},
            )
            total = int(cur.fetchone()[0])
            cur.execute(
                f"""
                SELECT ds.vehicle_identifier, ds.vehicle_plate,
                       ds.last_observed_at, ds.first_ever_observed_at,
                       ds.current_lat, ds.current_lon,
                       ds.current_vehicle_model_name, ds.current_form_factor,
                       {_ACK_COLUMNS}
                  FROM device_state ds
                  {_ACK_JOINS}
                  {where}
                 ORDER BY ds.last_observed_at {direction}, ds.vehicle_identifier
                 LIMIT %(limit)s OFFSET %(offset)s
                """,
                {"cutoff": cutoff, "limit": limit, "offset": offset},
            )
            rows = cur.fetchall()
            out = _envelope(cur, snap, hours=hours, order=order, total=total,
                            limit=limit, offset=offset)
    out["devices"] = [
        {
            **_vehicle(r[0], r[1]),
            "last_observed_at": _iso(r[2]),
            "hours_missing": _hours(snap, r[2]),
            "first_ever_observed_at": _iso(r[3]),
            "last_lat": float(r[4]) if r[4] is not None else None,
            "last_lon": float(r[5]) if r[5] is not None else None,
            "vehicle_model_name": r[6],
            "form_factor": r[7],
            "ack": _ack(r[8:15], r[2]),
        }
        for r in rows
    ]
    return out


@router.get("/api/v1/private/census/gone")
def census_gone(
    user: SessionUser = Depends(require_admin),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Vehicles an admin acknowledged permanently gone — the join against
    device_census_ack, so the list is auditable rather than a filter.
    Reappeared vehicles sort FIRST: a van emptied, or a vehicle recovered,
    is the most interesting row in the system."""
    with connection() as conn:
        with conn.cursor() as cur:
            _cycle, snap = latest_complete_cycle(cur)
            cur.execute(
                f"""
                SELECT a.vehicle_identifier, ds.vehicle_plate,
                       ds.last_observed_at, ds.first_ever_observed_at,
                       ds.current_vehicle_model_name, ds.current_form_factor,
                       {_ACK_COLUMNS}
                  FROM device_census_ack a
                  LEFT JOIN device_state ds ON ds.vehicle_identifier = a.vehicle_identifier
                  LEFT JOIN accounts acc_ack ON acc_ack.id = a.acknowledged_by
                  LEFT JOIN accounts acc_note ON acc_note.id = a.note_by
                 WHERE a.status = 'gone'
                 ORDER BY (ds.last_observed_at > a.last_observed_at_ack) IS TRUE DESC,
                          a.acknowledged_at DESC, a.vehicle_identifier
                 LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cur.fetchall()
            cur.execute("SELECT COUNT(*) FROM device_census_ack WHERE status = 'gone'")
            total = int(cur.fetchone()[0])
            out = _envelope(cur, snap, total=total, limit=limit, offset=offset)
    out["devices"] = [
        {
            **_vehicle(r[0], r[1]),
            "last_observed_at": _iso(r[2]),
            "hours_missing": _hours(snap, r[2]),
            "first_ever_observed_at": _iso(r[3]),
            "vehicle_model_name": r[4],
            "form_factor": r[5],
            "ack": _ack(r[6:13], r[2]),
        }
        for r in rows
    ]
    return out


# ---------------------------------------------------------------------------
# Census writes
# ---------------------------------------------------------------------------

class AckIn(BaseModel):
    note: str | None = Field(default=None, max_length=_MAX_NOTE)


class NoteIn(BaseModel):
    # null (or "") clears the note.
    note: str | None = Field(default=None, max_length=_MAX_NOTE)


def _require_known(cur, vid: str) -> datetime:
    cur.execute("SELECT last_observed_at FROM device_state WHERE vehicle_identifier = %s",
                (vid,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "no vehicle with that identifier")
    return row[0]


def _ack_state(cur, vid: str) -> dict[str, Any]:
    cur.execute(
        f"""
        SELECT ds.last_observed_at, {_ACK_COLUMNS}
          FROM device_state ds
          {_ACK_JOINS}
         WHERE ds.vehicle_identifier = %s
        """,
        (vid,),
    )
    row = cur.fetchone()
    return {"vehicle_identifier": vid, "ack": _ack(row[1:8], row[0]) if row else None}


@router.put("/api/v1/private/census/{vehicle_identifier}/ack")
def census_acknowledge(
    vehicle_identifier: _VID,
    payload: AckIn = Body(default_factory=AckIn),
    user: SessionUser = Depends(require_admin),
) -> dict[str, Any]:
    """Acknowledge a vehicle permanently gone. Records the admin, the time,
    and the vehicle's last_observed_at as of now — the baseline a later
    sighting is a reappearance against. Re-acknowledging a reappeared vehicle
    moves that baseline, which is the admin saying "gone again"."""
    note = (payload.note or "").strip() or None
    with connection() as conn:
        with conn.cursor() as cur:
            last_seen = _require_known(cur, vehicle_identifier)
            cur.execute(
                """
                INSERT INTO device_census_ack (
                    vehicle_identifier, status, acknowledged_by, acknowledged_at,
                    last_observed_at_ack, note, note_by, note_at, updated_at
                ) VALUES (%(vid)s, 'gone', %(by)s, NOW(), %(seen)s,
                          %(note)s, %(note_by)s,
                          CASE WHEN %(has_note)s THEN NOW() END, NOW())
                ON CONFLICT (vehicle_identifier) DO UPDATE SET
                    status = 'gone',
                    acknowledged_by = EXCLUDED.acknowledged_by,
                    acknowledged_at = EXCLUDED.acknowledged_at,
                    last_observed_at_ack = EXCLUDED.last_observed_at_ack,
                    withdrawn_by = NULL,
                    withdrawn_at = NULL,
                    note = COALESCE(EXCLUDED.note, device_census_ack.note),
                    note_by = COALESCE(EXCLUDED.note_by, device_census_ack.note_by),
                    note_at = COALESCE(EXCLUDED.note_at, device_census_ack.note_at),
                    updated_at = NOW()
                """,
                {"vid": vehicle_identifier, "by": user.account_id,
                 "seen": last_seen, "note": note,
                 "note_by": user.account_id if note else None,
                 "has_note": note is not None},
            )
            out = _ack_state(cur, vehicle_identifier)
        conn.commit()
    log.info("census ack gone vehicle=%s admin_account=%d", vehicle_identifier, user.account_id)
    return out


@router.delete("/api/v1/private/census/{vehicle_identifier}/ack")
def census_unacknowledge(
    vehicle_identifier: _VID,
    user: SessionUser = Depends(require_admin),
) -> dict[str, Any]:
    """Withdraw a gone acknowledgement. The row is KEPT (status not_gone,
    with who withdrew it and when, and the note) — reversing a judgement
    does not erase that it was made. 404 when there is nothing to withdraw."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE device_census_ack
                   SET status = 'not_gone', withdrawn_by = %s, withdrawn_at = NOW(),
                       updated_at = NOW()
                 WHERE vehicle_identifier = %s AND status = 'gone'
                """,
                (user.account_id, vehicle_identifier),
            )
            changed = cur.rowcount
            out = _ack_state(cur, vehicle_identifier) if changed else None
        conn.commit()
    if not changed:
        raise HTTPException(404, "that vehicle is not acknowledged gone")
    log.info("census ack withdrawn vehicle=%s admin_account=%d",
             vehicle_identifier, user.account_id)
    return out


@router.put("/api/v1/private/census/{vehicle_identifier}/note")
def census_note(
    vehicle_identifier: _VID,
    payload: NoteIn = Body(...),
    user: SessionUser = Depends(require_admin),
) -> dict[str, Any]:
    """Set (or, with null / "", clear) the admin note on any known vehicle,
    acknowledged or not — a note on a missing vehicle nobody has called gone
    yet is how an admin records "Veo says it is in the shop"."""
    note = (payload.note or "").strip() or None
    with connection() as conn:
        with conn.cursor() as cur:
            _require_known(cur, vehicle_identifier)
            cur.execute(
                """
                INSERT INTO device_census_ack (
                    vehicle_identifier, status, note, note_by, note_at, updated_at
                ) VALUES (%(vid)s, 'not_gone', %(note)s, %(by)s, NOW(), NOW())
                ON CONFLICT (vehicle_identifier) DO UPDATE SET
                    note = EXCLUDED.note, note_by = EXCLUDED.note_by,
                    note_at = EXCLUDED.note_at, updated_at = NOW()
                """,
                {"vid": vehicle_identifier, "note": note, "by": user.account_id},
            )
            out = _ack_state(cur, vehicle_identifier)
        conn.commit()
    return out


# ---------------------------------------------------------------------------
# Resolving a report (§2.6(1) — the safety valve the 24 hours used to be)
# ---------------------------------------------------------------------------

class ResolveIn(BaseModel):
    resolution: str = Field(..., min_length=1, max_length=_MAX_RESOLUTION,
                            description="Why — e.g. 'void: duplicate of a griefing run'")


@router.post("/api/v1/private/reports/{report_id}/resolve")
def resolve_report(
    report_id: int = Path(..., ge=1),
    payload: ResolveIn = Body(...),
    user: SessionUser = Depends(require_admin),
) -> dict[str, Any]:
    """Void or resolve one device report. From the next request it counts
    for nothing — not toward has_negative_report, not toward suppression,
    not in the export. Attributed (resolved_by = the admin's account) and
    final: there is no un-resolve, so the audit trail cannot be rewritten;
    a mistaken void is undone by the next rider's report. 409 when already
    resolved, 404 when there is no such report."""
    resolution = payload.resolution.strip()
    if not resolution:
        raise HTTPException(422, "resolution must not be blank")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE device_reports
                   SET resolved_at = NOW(), resolved_by = %s, resolution = %s
                 WHERE id = %s AND resolved_at IS NULL
                RETURNING id, vehicle_identifier, report_type, reported_at, resolved_at
                """,
                (user.account_id, resolution, report_id),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT resolved_at FROM device_reports WHERE id = %s",
                            (report_id,))
                existing = cur.fetchone()
        conn.commit()
    if row is None:
        if existing is None:
            raise HTTPException(404, "no such report")
        raise HTTPException(409, "report already resolved")
    log.info("device report id=%d resolved by admin_account=%d", report_id, user.account_id)
    return {
        "id": int(row[0]),
        "vehicle_identifier": row[1],
        "report_type": row[2],
        "reported_at": _iso(row[3]),
        "resolved_at": _iso(row[4]),
        "resolved_by": user.email,
        "resolution": resolution,
    }


# ---------------------------------------------------------------------------
# Export for advocacy (the data Phase 2's page renders)
# ---------------------------------------------------------------------------

def _headline(inacc: dict[str, Any]) -> str:
    return (f"{inacc['vehicles_reported']} vehicles reported inaccessible, "
            f"{inacc['still_unmoved']} still unmoved after {inacc['unmoved_days']} days")


def _summary_rows(data: dict[str, Any]) -> list[tuple[str, int, str]]:
    inacc = data["inaccessible"]
    rows = [
        ("inaccessible_vehicles_reported", inacc["vehicles_reported"], ""),
        ("inaccessible_vehicles_reported_signed_in", inacc["vehicles_reported_signed_in"], ""),
        ("inaccessible_reports", inacc["reports"], ""),
        ("inaccessible_still_unmoved", inacc["still_unmoved"], ""),
    ]
    sample = data["broken_parts"]["sample"]
    rows.append(("vehicles_observed", sample["vehicles_observed"], ""))
    rows.append(("vehicles_with_feature_answers", sample["vehicles_with_feature_answers"], ""))
    for p in data["broken_parts"]["parts"]:
        rows.append((f"broken_{p['part']}", p["broken"], p["part"]))
        rows.append((f"broken_{p['part']}_under_review", p["under_review"], p["part"]))
        rows.append((f"vehicles_with_{p['part']}", p["vehicles_with_part"], p["part"]))
    problems = data["problem_reports"]
    for t in problems["by_type"]:
        rows.append((f"reports_{t['report_type']}", t["reports"], ""))
    for r in problems["not_rideable"]["reasons"]:
        rows.append((f"not_rideable_reason_{r['reason']}", r["reports"], ""))
    for decoy, n in problems["remapped"].items():
        rows.append((f"remapped_from_{decoy}", n, ""))
    return rows


@router.get("/api/v1/private/reports/export")
def reports_export(
    user: SessionUser = Depends(require_admin),
    window_days: int = Query(30, ge=1, le=3650),
    unmoved_days: int = Query(7, ge=0, le=3650),
    format: Literal["json", "csv"] = Query("json"),
    table: Literal["summary", "inaccessible"] = Query(
        "summary", description="CSV only: the summary metrics, or one row per "
                               "inaccessible-reported vehicle"),
) -> Any:
    """The advocacy numbers: "N vehicles reported inaccessible, M still
    unmoved after X days", plus vehicles with an unresolved broken bell, cup
    holder, basket or phone holder from feature confirmation. Read-only.
    Every block carries its definition, window and sample, because a number
    handed to a regulator without them is an argument waiting to happen.

    Never exports a location (plan §6)."""
    with connection() as conn:
        with conn.cursor() as cur:
            cycle_id, snap = latest_complete_cycle(cur)
            since = snap - timedelta(days=window_days)
            inacc = fleet_reports.inaccessible_summary(
                cur, cycle_id=cycle_id, since=since, until=snap,
                unmoved_days=unmoved_days)
            parts = fleet_reports.broken_parts_summary(cur, since=since, until=snap)
            problems = fleet_reports.reasons_summary(cur, since=since, until=snap)
    data = {
        "as_of": _iso(snap),
        "window": {"days": window_days, "from": _iso(since), "to": _iso(snap)},
        "headline": _headline(inacc),
        "inaccessible": inacc,
        "broken_parts": parts,
        "problem_reports": problems,
    }
    if format == "json":
        return data

    buf = io.StringIO()
    w = csv.writer(buf)
    if table == "summary":
        w.writerow(["metric", "value", "part", "window_from", "window_to",
                    "unmoved_days"])
        for metric, value, part in _summary_rows(data):
            w.writerow([metric, value, part, data["window"]["from"],
                        data["window"]["to"], unmoved_days])
    else:
        cols = ["vehicle_identifier", "public_name", "first_reported_at",
                "last_reported_at", "reports", "signed_in_reports",
                "distinct_reporting_accounts", "in_feed",
                "unmoved_since_first_report", "days_since_first_report",
                "still_unmoved"]
        w.writerow(cols)
        for v in inacc["vehicles"]:
            w.writerow([str(v[c]).lower() if isinstance(v[c], bool) else v[c]
                        for c in cols])
    stamp = snap.strftime("%Y-%m-%d")
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="fleet-reports-{table}-{stamp}.csv"',
            "Cache-Control": "private, no-store",
        },
    )


# ---------------------------------------------------------------------------
# One vehicle's dossier data (Phase 2's per-device page reads this)
# ---------------------------------------------------------------------------

_MAX_DOSSIER_REPORTS = 500


@router.get("/api/v1/private/devices/{vehicle_identifier}/reports")
def device_dossier(
    vehicle_identifier: _VID,
    user: SessionUser = Depends(require_admin),
    limit: int = Query(200, ge=1, le=_MAX_DOSSIER_REPORTS),
) -> dict[str, Any]:
    """Everything the reports side knows about one vehicle: every device
    report (type, reason, decoy remap, observed and reported times, the
    reporter's account when signed in, the charge at report time, whether it
    still stands, and its resolution), the suppression it produces now, its
    feature consensus with any broken parts, and its census acknowledgement.
    Movement and battery HISTORY are /api/v1/private/devices/{vid}/history.
    404 when neither device_state nor any report knows the vehicle."""
    from .device_features import FEATURE_KEYS, FEATURE_PRESENCE_COLUMNS

    presence = ", ".join(f"ds.{FEATURE_PRESENCE_COLUMNS[k]}" for k in FEATURE_KEYS)
    with connection() as conn:
        with conn.cursor() as cur:
            cycle_id, snap = latest_complete_cycle(cur)
            cur.execute(
                f"""
                SELECT ds.vehicle_plate, ds.first_ever_observed_at, ds.last_observed_at,
                       ds.first_observed_at_location, ds.current_vehicle_model_name,
                       ds.current_form_factor, ds.feature_status,
                       ds.features_poor_condition, ds.features_confirmed_at,
                       {presence},
                       (SELECT r.current_range_meters FROM raw_telemetry_points r
                         WHERE r.cycle_id = %(cycle)s
                           AND r.vehicle_identifier = ds.vehicle_identifier
                         LIMIT 1),
                       EXISTS (SELECT 1 FROM raw_telemetry_points r
                                WHERE r.cycle_id = %(cycle)s
                                  AND r.vehicle_identifier = ds.vehicle_identifier)
                  FROM device_state ds
                 WHERE ds.vehicle_identifier = %(vid)s
                """,
                {"cycle": cycle_id, "vid": vehicle_identifier},
            )
            state = cur.fetchone()
            cur.execute(
                """
                SELECT dr.id, dr.report_type, dr.reason, dr.submitted_reason,
                       dr.observed_at, dr.reported_at, dr.account_id, acc.email,
                       dr.range_at_report_meters, dr.resolved_at, res.email,
                       dr.resolution
                  FROM device_reports dr
                  LEFT JOIN accounts acc ON acc.id = dr.account_id
                  LEFT JOIN accounts res ON res.id = dr.resolved_by
                 WHERE dr.vehicle_identifier = %s
                 ORDER BY dr.reported_at DESC, dr.id DESC
                 LIMIT %s
                """,
                (vehicle_identifier, limit),
            )
            reports = cur.fetchall()
            if state is None and not reports:
                raise HTTPException(404, "no vehicle with that identifier")
            standing = fleet_reports.open_reports_for(cur, cycle_id, vehicle_identifier)
            ack = _ack_state(cur, vehicle_identifier)["ack"] if state else None
            standing_ids = _standing_ids(cur, cycle_id, vehicle_identifier)

    parked_since = state[3] if state else None
    first = standing[0] if standing else None
    out: dict[str, Any] = {
        **_vehicle(vehicle_identifier, state[0] if state else None),
        "as_of": _iso(snap),
        "state": None if state is None else {
            "first_ever_observed_at": _iso(state[1]),
            "last_observed_at": _iso(state[2]),
            "parked_since": _iso(state[3]),
            "vehicle_model_name": state[4],
            "form_factor": state[5],
            "in_feed": bool(state[14]),
            "current_range_meters": state[13],
        },
        "suppression": {
            "suppressed": bool(first) and bool(state and state[14]),
            "suppressed_reason": first["report_type"] if first else None,
            "suppressed_since": first["reported_at"] if first else None,
        },
        "reports": [
            {
                "id": int(r[0]),
                "report_type": r[1],
                "reason": r[2],
                "remapped_from_reason": r[3],
                "observed_at": _iso(r[4]),
                "reported_at": _iso(r[5]),
                "signed_in": r[6] is not None,
                "reporter_account_id": r[6],
                "reporter_email": r[7],
                "range_at_report_meters": r[8],
                "moved_since": bool(parked_since and parked_since > r[5]),
                # Counts toward suppression right now (signed in, unresolved,
                # not moved, charge not risen).
                "standing": int(r[0]) in standing_ids,
                "resolved_at": _iso(r[9]),
                "resolved_by": r[10],
                "resolution": r[11],
            }
            for r in reports
        ],
        "features": None if state is None else {
            "feature_status": state[6],
            "present": {k: state[9 + i] for i, k in enumerate(FEATURE_KEYS)},
            "poor_condition": list(state[7] or []),
            "confirmed_at": _iso(state[8]),
            # Broken parts NEVER suppress (plan §2.9); they are shown here
            # and counted in the export.
            "broken_parts": [k for k in fleet_reports.BROKEN_PART_ORDER
                             if state[9 + FEATURE_KEYS.index(k)]
                             and k in (state[7] or [])],
        },
        "census": ack,
    }
    return out


def _standing_ids(cur, cycle_id: Any, vid: str) -> set[int]:
    cur.execute(fleet_reports._open_reports_sql(single_vehicle=True),
                fleet_reports._params(cycle_id, vid=vid))
    return {int(row[3]) for row in cur.fetchall()}
