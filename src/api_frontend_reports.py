"""Frontend report ingestion + aggregates (API_REQUIREMENTS.md §3).

    POST /api/v1/reports/device               rider failure report
    POST /api/v1/reports/discount             missed-discount evidence
    GET  /api/v1/reports/summary?layer=...    per-region aggregate (public)
    GET  /api/v1/reports/export/monthly.csv   public CSV for DOTI/journalists

Distinct from src/api_reports.py (the original map-pin negative_reports +
quality feedback flow) — these are the account-aware rider flows. Device
reports feed the same has_negative_report signal on /devices/current.
"""

from __future__ import annotations

import csv
import io
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import h3
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from . import geo
from .accounts import SessionUser, optional_session, require_session
from .client_ip import real_client_ip
from .pg import connection
from .ratelimit import enforce
from .receipts import (
    MAX_RECEIPT_BYTES,
    ReceiptError,
    delete_receipt,
    receipts_bucket,
    store_receipt,
)

log = logging.getLogger(__name__)

router = APIRouter()

_REPORT_TYPES = ("failed_unlock", "dead_battery", "damaged")
_DEDUPE_WINDOW_MINUTES = 30

_LIMIT_DEVICE_ANON_PER_IP = (5, 86400)       # §3.1: 5/day anonymous
_LIMIT_DEVICE_AUTH_PER_ACCOUNT = (30, 86400)
_LIMIT_DISCOUNT_PER_ACCOUNT = (20, 86400)
_LIMIT_EXPORT_PER_IP = (10, 3600)

# §3.3 est_overcharge_cents: without Veo's rate card we can't compute the
# exact delta, so the estimate assumes the missed equity discount is half
# of what was charged. Documented in API.md; tune here when DOTI confirms
# the actual discount schedule.
OVERCHARGE_FRACTION = 0.5

_SUMMARY_TTL_S = 600  # matches the CDN Cache-Control below


class _SummaryCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, layer: str) -> dict[str, Any] | None:
        with self._lock:
            hit = self._entries.get(layer)
            if hit and time.monotonic() - hit[0] < _SUMMARY_TTL_S:
                return hit[1]
            return None

    def put(self, layer: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._entries[layer] = (time.monotonic(), payload)


_summary_cache = _SummaryCache()


# ---------------------------------------------------------------------------
# POST /api/v1/reports/device
# ---------------------------------------------------------------------------
class DeviceReportIn(BaseModel):
    vehicle_identifier: str = Field(..., min_length=16, max_length=16, pattern=r"^[0-9a-f]{16}$")
    report_type: str = Field(..., pattern=f"^({'|'.join(_REPORT_TYPES)})$")
    observed_at: datetime | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)


@router.post("/api/v1/reports/device")
def submit_device_report(
    request: Request,
    payload: DeviceReportIn = Body(...),
    user: SessionUser | None = Depends(optional_session),
) -> dict[str, Any]:
    """Rider failure report. Anonymous allowed (tight limits); a presented
    session links the report to the account (weighted higher in the
    summary aggregate).

    Idempotency: an identical (vehicle, type, reporter) within 30 minutes
    returns the existing report with `deduped: true` instead of a new row.
    """
    ip = real_client_ip(request)
    ua = request.headers.get("user-agent")

    with connection() as conn:
        with conn.cursor() as cur:
            if user is None:
                enforce(cur, bucket="device_report_ip", key=ip or "?",
                        limit=_LIMIT_DEVICE_ANON_PER_IP[0],
                        window_seconds=_LIMIT_DEVICE_ANON_PER_IP[1])
            else:
                enforce(cur, bucket="device_report_account", key=str(user.account_id),
                        limit=_LIMIT_DEVICE_AUTH_PER_ACCOUNT[0],
                        window_seconds=_LIMIT_DEVICE_AUTH_PER_ACCOUNT[1])

            # Dedupe: reporter = account when signed in, else IP.
            if user is not None:
                reporter_clause, reporter_val = "account_id = %s", user.account_id
            else:
                reporter_clause, reporter_val = "account_id IS NULL AND reporter_ip = %s", ip
            cur.execute(
                f"""
                SELECT id, reported_at FROM device_reports
                WHERE vehicle_identifier = %s AND report_type = %s
                  AND {reporter_clause}
                  AND reported_at >= NOW() - INTERVAL '{_DEDUPE_WINDOW_MINUTES} minutes'
                ORDER BY reported_at DESC LIMIT 1
                """,
                (payload.vehicle_identifier, payload.report_type, reporter_val),
            )
            dup = cur.fetchone()
            if dup:
                conn.commit()  # keep the rate-limit event
                return {"id": int(dup[0]), "reported_at": dup[1].isoformat(), "deduped": True}

            # h3 anchor: reporter coords when given, else the scooter's
            # current cell (same anchoring rationale as sql/008).
            if payload.lat is not None and payload.lng is not None:
                h3_10 = int(h3.latlng_to_cell(payload.lat, payload.lng, 10), 16)
            else:
                cur.execute(
                    "SELECT current_h3_10_index FROM device_state WHERE vehicle_identifier = %s",
                    (payload.vehicle_identifier,),
                )
                row = cur.fetchone()
                h3_10 = int(row[0]) if row and row[0] is not None else None

            cur.execute(
                """
                INSERT INTO device_reports (
                    vehicle_identifier, report_type, observed_at, lat, lng,
                    h3_10_index, account_id, reporter_ip, reporter_user_agent
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, reported_at
                """,
                (payload.vehicle_identifier, payload.report_type, payload.observed_at,
                 payload.lat, payload.lng, h3_10,
                 user.account_id if user else None, ip, ua),
            )
            new_id, reported_at = cur.fetchone()
        conn.commit()

    log.info(
        "device report id=%d vehicle=%s type=%s auth=%s",
        new_id, payload.vehicle_identifier, payload.report_type, user is not None,
    )
    return {"id": int(new_id), "reported_at": reported_at.isoformat(), "deduped": False}


# ---------------------------------------------------------------------------
# POST /api/v1/reports/discount
# ---------------------------------------------------------------------------
class DiscountReportIn(BaseModel):
    ride_ended_at: datetime
    zone_version: str = Field(..., pattern="^(v1|v2)$")
    end_lat: float | None = Field(default=None, ge=-90, le=90)
    end_lng: float | None = Field(default=None, ge=-180, le=180)
    amount_charged_cents: int | None = Field(default=None, ge=0, le=100_000)


async def _parse_discount_body(request: Request) -> tuple[DiscountReportIn, bytes | None]:
    """JSON body, or multipart/form-data with the same field names plus an
    optional `receipt` file part."""
    ctype = (request.headers.get("content-type") or "").lower()
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        fields = {
            k: v for k in
            ("ride_ended_at", "zone_version", "end_lat", "end_lng", "amount_charged_cents")
            if (v := form.get(k)) not in (None, "")
        }
        try:
            payload = DiscountReportIn(**fields)
        except ValueError as e:
            raise HTTPException(422, f"bad form fields: {e}")
        receipt = form.get("receipt")
        if receipt is None or isinstance(receipt, str):
            return payload, None
        data = await receipt.read()
        if len(data) > MAX_RECEIPT_BYTES:
            raise HTTPException(413, "receipt too large (max 10 MB)")
        return payload, data or None
    try:
        payload = DiscountReportIn(**(await request.json()))
    except ValueError as e:
        raise HTTPException(422, f"bad JSON body: {e}")
    return payload, None


@router.post("/api/v1/reports/discount")
async def submit_discount_report(
    request: Request,
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    """Missed-discount evidence. Signed-in only — evidence needs provenance.

    Accepts JSON, or multipart/form-data when attaching a `receipt` image.
    The receipt is EXIF-stripped and stored in a private R2 bucket with an
    18-month retention (see /api/v1/meta/privacy).
    """
    payload, receipt_bytes = await _parse_discount_body(request)
    ip = real_client_ip(request)
    ua = request.headers.get("user-agent")

    receipt_key: str | None = None
    if receipt_bytes:
        if not receipts_bucket():
            raise HTTPException(503, "receipt storage not configured — submit without the image")
        try:
            receipt_key = store_receipt(user.account_id, receipt_bytes)
        except ReceiptError as e:
            raise HTTPException(400, str(e))

    try:
        with connection() as conn:
            with conn.cursor() as cur:
                enforce(cur, bucket="discount_report_account", key=str(user.account_id),
                        limit=_LIMIT_DISCOUNT_PER_ACCOUNT[0],
                        window_seconds=_LIMIT_DISCOUNT_PER_ACCOUNT[1])
                cur.execute(
                    """
                    INSERT INTO discount_reports (
                        account_id, ride_ended_at, zone_version, end_lat, end_lng,
                        amount_charged_cents, receipt_r2_key,
                        reporter_ip, reporter_user_agent
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, created_at
                    """,
                    (user.account_id, payload.ride_ended_at, payload.zone_version,
                     payload.end_lat, payload.end_lng, payload.amount_charged_cents,
                     receipt_key, ip, ua),
                )
                new_id, created_at = cur.fetchone()
            conn.commit()
    except Exception:
        # The DB write is what makes the receipt reachable via cleanup_receipts
        # (it only scans discount_reports). If that write never lands, delete
        # the orphaned R2 object now rather than retaining it past 18 months.
        if receipt_key is not None:
            try:
                delete_receipt(receipt_key)
            except ReceiptError:
                log.exception("failed to clean up orphaned receipt %s", receipt_key)
        raise

    log.info("discount report id=%d account=%d receipt=%s",
             new_id, user.account_id, bool(receipt_key))
    return {
        "id": int(new_id),
        "created_at": created_at.isoformat(),
        "receipt_stored": receipt_key is not None,
    }


# ---------------------------------------------------------------------------
# GET /api/v1/reports/summary
# ---------------------------------------------------------------------------
@router.get("/api/v1/reports/summary")
def reports_summary(
    response: Response,
    layer: str = Query(..., description="Boundary layer, e.g. neighborhood, v1"),
) -> dict[str, Any]:
    """Per-region report aggregate — powers the 'Contract violations'
    choropleth and the ticker. Public, cached ~10 min (in-process + CDN).

    device_reports is a weighted count: authenticated reports count 2,
    anonymous count 1 (§3.1 — attributed evidence weighs more).
    Reports without coordinates can't be regionalized and are excluded
    here (they still appear in the CSV export and internal signals).
    """
    try:
        names = geo.region_names(layer)
    except KeyError:
        raise HTTPException(404, f"unknown layer '{layer}'")

    cached = _summary_cache.get(layer)
    if cached is None:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT lat, lng, account_id FROM device_reports "
                    "WHERE lat IS NOT NULL AND lng IS NOT NULL"
                )
                device_rows = cur.fetchall()
                cur.execute(
                    "SELECT end_lat, end_lng, amount_charged_cents FROM discount_reports "
                    "WHERE end_lat IS NOT NULL AND end_lng IS NOT NULL"
                )
                discount_rows = cur.fetchall()

        regions: dict[str, dict[str, int]] = {
            n: {"device_reports": 0, "discount_reports": 0, "est_overcharge_cents": 0}
            for n in names
        }
        for lat, lng, account_id in device_rows:
            name = geo.region_for_point(layer, float(lng), float(lat))
            if name:
                regions[name]["device_reports"] += 2 if account_id is not None else 1
        for lat, lng, amount in discount_rows:
            name = geo.region_for_point(layer, float(lng), float(lat))
            if name:
                regions[name]["discount_reports"] += 1
                if amount:
                    regions[name]["est_overcharge_cents"] += int(amount * OVERCHARGE_FRACTION)

        cached = {
            "layer": layer,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "regions": regions,
        }
        _summary_cache.put(layer, cached)

    response.headers["Cache-Control"] = f"public, max-age={_SUMMARY_TTL_S}"
    return cached


# ---------------------------------------------------------------------------
# GET /api/v1/reports/export/monthly.csv
# ---------------------------------------------------------------------------
@router.get("/api/v1/reports/export/monthly.csv")
def reports_export_monthly(
    request: Request,
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$", description="YYYY-MM (UTC)"),
) -> Response:
    """Public CSV of the month's reports for DOTI/journalists. No auth;
    rate-limited. Columns exclude reporter identity (no IPs, no emails —
    just an `authenticated` boolean for evidentiary weight)."""
    ip = real_client_ip(request)
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(400, "month must be YYYY-MM")
    end = (start.replace(year=start.year + 1, month=1)
           if start.month == 12 else start.replace(month=start.month + 1))

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="reports_export_ip", key=ip or "?",
                    limit=_LIMIT_EXPORT_PER_IP[0], window_seconds=_LIMIT_EXPORT_PER_IP[1])
            conn.commit()
            cur.execute(
                """
                SELECT reported_at, vehicle_identifier, report_type, lat, lng,
                       account_id IS NOT NULL
                FROM device_reports
                WHERE reported_at >= %s AND reported_at < %s
                ORDER BY reported_at
                """,
                (start, end),
            )
            device_rows = cur.fetchall()
            cur.execute(
                """
                SELECT created_at, ride_ended_at, zone_version, end_lat, end_lng,
                       amount_charged_cents, receipt_r2_key IS NOT NULL
                FROM discount_reports
                WHERE created_at >= %s AND created_at < %s
                ORDER BY created_at
                """,
                (start, end),
            )
            discount_rows = cur.fetchall()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "kind", "reported_at", "vehicle_identifier", "report_type_or_zone",
        "lat", "lng", "amount_charged_cents", "authenticated_or_has_receipt",
    ])
    for reported_at, vid, rtype, lat, lng, authed in device_rows:
        w.writerow(["device", reported_at.isoformat(), vid, rtype,
                    lat, lng, "", str(bool(authed)).lower()])
    for created_at, _ride_ended, zone, lat, lng, amount, has_receipt in discount_rows:
        w.writerow(["discount", created_at.isoformat(), "", zone,
                    lat, lng, amount if amount is not None else "",
                    str(bool(has_receipt)).lower()])

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="veo-audit-reports-{month}.csv"',
            "Cache-Control": "public, max-age=600",
        },
    )
