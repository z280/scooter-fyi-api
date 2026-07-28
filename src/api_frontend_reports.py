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
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import h3
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from . import geo
from .accounts import SessionUser, optional_session, require_session
from .client_ip import real_client_ip
from .pg import connection
from .points import credit_report_points
from .ratelimit import enforce
from .receipts import (
    MAX_RECEIPT_BYTES,
    ReceiptError,
    delete_receipt,
    receipts_bucket,
    store_model_photo,
    store_receipt,
)

log = logging.getLogger(__name__)

router = APIRouter()

# 'improperly_parked' is stored and counted in the reports summary/export
# (compliance signal), but is EXCLUDED from has_negative_report /
# reliability_tier — see NON_RELIABILITY_REPORT_TYPES and the exclusion in
# api_public.py / api_h3.py. A parking complaint says nothing about whether
# the scooter rides. 'not_found' (sql/029) is NOT excluded — see that
# migration's header for why a missing vehicle IS a reliability signal.
_REPORT_TYPES = ("not_rideable", "dead_battery", "damaged", "improperly_parked", "not_found")

# DEPRECATED input aliases — accepted on the wire, normalised to the
# canonical spelling before anything reads them. REMOVE once no client
# sends the old spelling.
#
# sql/037 renamed 'failed_unlock' -> 'not_rideable'. The button that sends
# it lives in a DIFFERENT repository, so backend and frontend cannot deploy
# atomically, and report_type is validated by a pydantic `pattern`: a
# mismatch is a 422, not a soft failure. Without this alias there is no
# safe merge order — ship the backend first and every rider on the old
# frontend gets "Couldn't send — please try again" forever; ship the
# frontend first and it breaks against the old backend the same way. Either
# way the single most important reliability signal we collect stops
# flowing, and nothing in the response tells anyone why.
#
# REMOVAL: delete this dict (and the `_ACCEPTED_REPORT_TYPES` seam) once
# the frontend rename has been live long enough that no client sends the
# old spelling. The database can't answer that question — the alias is
# normalised away before storage, by design — so the signal is the
# WARNING logged by _normalise_report_type below. When 30 days pass with
# none of those lines, delete this and the alias becomes a 422 again.
_DEPRECATED_REPORT_TYPE_ALIASES = {"failed_unlock": "not_rideable"}

# What the endpoint ACCEPTS, as opposed to what it stores. Storage only
# ever sees _REPORT_TYPES — sql/037's CHECK constraint would reject
# anything else, which is exactly the safety net we want behind the
# normalisation.
_ACCEPTED_REPORT_TYPES = _REPORT_TYPES + tuple(_DEPRECATED_REPORT_TYPE_ALIASES)

# Report types that must NOT drive has_negative_report / reliability_tier.
# Single source of truth for the exclusion applied in the /devices/current
# and /h3 aggregate queries. A scooter blocking a sidewalk can still be a
# great ride, so parking complaints stay out of the "worth the walk?" signal.
NON_RELIABILITY_REPORT_TYPES = ("improperly_parked",)


def reliability_report_type_sql(alias: str = "dr") -> str:
    """SQL predicate limiting a device_reports row (table alias `alias`) to
    the report types that count toward has_negative_report — i.e. excluding
    NON_RELIABILITY_REPORT_TYPES. Interpolated into the /devices/current and
    /h3 aggregate queries so the exclusion has one source of truth. The
    values are code-controlled literals (never user input), so inlining them
    is injection-safe; returns TRUE when nothing is excluded."""
    if not NON_RELIABILITY_REPORT_TYPES:
        return "TRUE"
    excluded = ", ".join("'{}'".format(t.replace("'", "''")) for t in NON_RELIABILITY_REPORT_TYPES)
    return f"{alias}.report_type NOT IN ({excluded})"


_DEDUPE_WINDOW_MINUTES = 30

_LIMIT_DEVICE_ANON_PER_IP = (3, 3600)        # 3/hour per IP (anonymous)
_LIMIT_DEVICE_AUTH_PER_ACCOUNT = (10, 3600)  # 10/hour per authenticated account
_LIMIT_DISCOUNT_PER_ACCOUNT = (20, 86400)
_LIMIT_EXPORT_PER_IP = (10, 3600)
_LIMIT_MODEL_ANON_PER_IP = (5, 3600)
_LIMIT_MODEL_AUTH_PER_ACCOUNT = (20, 3600)
_MAX_MODEL_DESCRIPTION = 2000
# Ceiling on the whole request body an ANONYMOUS model report may declare.
# A text-only report is a device_id, a <=2000 char description, an optional
# vehicle_identifier and two coordinates — kilobytes. 64 KB leaves room for
# multipart framing and a generous UTF-8 description while being far too
# small to smuggle a photo through, which is the point: the endpoint's rule
# is "a photo requires a session", and this is that rule enforced BEFORE we
# buffer the body rather than after.
_MAX_ANON_MODEL_REPORT_BYTES = 64 * 1024
_VEHICLE_IDENTIFIER_RE = re.compile(r"^[0-9a-f]{16}$")

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
    report_type: str = Field(..., pattern=f"^({'|'.join(_ACCEPTED_REPORT_TYPES)})$")
    observed_at: datetime | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("report_type")
    @classmethod
    def _normalise_report_type(cls, value: str) -> str:
        """Fold a deprecated spelling onto its canonical one at the edge, so
        exactly one value reaches dedupe, storage and points — see
        _DEPRECATED_REPORT_TYPE_ALIASES. Doing it here rather than in the
        handler means every future reader of a DeviceReportIn inherits it
        instead of having to remember."""
        canonical = _DEPRECATED_REPORT_TYPE_ALIASES.get(value)
        if canonical is None:
            return value
        log.warning(
            "deprecated report_type %r accepted and stored as %r — a client "
            "is still on the pre-sql/037 spelling", value, canonical,
        )
        return canonical


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
            # Dedupe FIRST, rate-limit second: a deduped resubmission is a
            # no-op (no new row, no new evidence) and must not consume
            # rate-limit quota. With the tight anon bucket (3/hour per IP),
            # metering before dedup would let one impatient rider triple-
            # tapping "report" on a single scooter exhaust their whole
            # hourly budget and get 429'd reporting a DIFFERENT broken
            # scooter minutes later. The dedup probe is a cheap indexed
            # SELECT, so leaving it unmetered is not an abuse vector.
            # Reporter = account when signed in, else IP.
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
                return {"id": int(dup[0]), "reported_at": dup[1].isoformat(),
                        "deduped": True, "points_awarded": 0}

            if user is None:
                enforce(cur, bucket="device_report_ip", key=ip or "?",
                        limit=_LIMIT_DEVICE_ANON_PER_IP[0],
                        window_seconds=_LIMIT_DEVICE_ANON_PER_IP[1])
            else:
                enforce(cur, bucket="device_report_account", key=str(user.account_id),
                        limit=_LIMIT_DEVICE_AUTH_PER_ACCOUNT[0],
                        window_seconds=_LIMIT_DEVICE_AUTH_PER_ACCOUNT[1])

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

            # Points (requirement #10): only for an authenticated, freshly-
            # inserted report of a points-eligible type. Reuses the same
            # h3_10 anchor already resolved above (reporter coords when
            # given, else the scooter's current cell) — cell_to_latlng
            # recovers a real lat/lng from that cell when the reporter
            # didn't supply coordinates directly.
            points_awarded = 0
            if user is not None:
                points_lat, points_lng = payload.lat, payload.lng
                if (points_lat is None or points_lng is None) and h3_10 is not None:
                    points_lat, points_lng = h3.cell_to_latlng(h3.int_to_str(h3_10))
                if points_lat is not None and points_lng is not None:
                    credited = credit_report_points(
                        cur, account_id=user.account_id,
                        report_type=payload.report_type,
                        lat=points_lat, lng=points_lng,
                        vehicle_identifier=payload.vehicle_identifier,
                        report_id=int(new_id),
                    )
                    points_awarded = credited["points"] if credited else 0
        conn.commit()

    log.info(
        "device report id=%d vehicle=%s type=%s auth=%s points=%d",
        new_id, payload.vehicle_identifier, payload.report_type, user is not None, points_awarded,
    )
    return {"id": int(new_id), "reported_at": reported_at.isoformat(),
            "deduped": False, "points_awarded": points_awarded}


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

    # Rate limit BEFORE storing the receipt, in its own committed
    # transaction — same reasoning as POST /api/v1/reports/model above.
    # store_receipt is an EXIF strip + re-encode plus an R2 PUT (and an R2
    # DELETE on the rollback path); metering after it left all of that
    # unpriced, and sharing the insert's transaction would refund the quota
    # of every attempt that failed on the expensive path.
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="discount_report_account", key=str(user.account_id),
                    limit=_LIMIT_DISCOUNT_PER_ACCOUNT[0],
                    window_seconds=_LIMIT_DISCOUNT_PER_ACCOUNT[1])
        conn.commit()

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
# ---------------------------------------------------------------------------
# POST /api/v1/reports/model
# ---------------------------------------------------------------------------
@router.post("/api/v1/reports/model")
async def submit_model_report(
    request: Request,
    user: SessionUser | None = Depends(optional_session),
) -> dict[str, Any]:
    """"We're showing this as an unrecognized model — tell us what it is."

    Feeds a review queue (sql/038), NOT the reliability signals. A model
    report is a catalog correction; a scooter whose name we got wrong still
    rides fine, so this must never touch has_negative_report /
    reliability_tier the way /api/v1/reports/device does.

    Anonymous TEXT is allowed and rate-limited per IP — naming a scooter
    model isn't evidence about a rider, and requiring sign-in would lose
    most of the corrections.

    A PHOTO requires a session, always. Accepting binaries from
    unauthenticated callers means anyone on the internet can push arbitrary
    files into our R2 bucket; no per-IP limit fixes that, because IPs are
    free and the liability of hosting whatever they upload is not. This is
    the only endpoint in the project that takes an upload alongside an
    optional session, so it is the only one where the rule has to be stated
    rather than inherited from require_session. An anonymous request is
    additionally capped at _MAX_ANON_MODEL_REPORT_BYTES of declared body
    BEFORE the form is parsed, so rejecting one costs a header read rather
    than a 10 MB spool.

    multipart/form-data: `device_id` and `description` required;
    `vehicle_identifier`, `lat`, `lng`, and a `photo` part optional.
    """
    # Either form encoding is fine — request.form() parses both, and a
    # text-only report has no reason to be multipart. A JSON body is
    # refused rather than half-accepted: it can't carry the photo part, so
    # silently taking it would drop an attachment the caller thought they
    # sent.
    ctype = (request.headers.get("content-type") or "").lower()
    if not (ctype.startswith("multipart/form-data")
            or ctype.startswith("application/x-www-form-urlencoded")):
        raise HTTPException(415, "send multipart/form-data or application/x-www-form-urlencoded")

    # BODY SIZE GATE FOR ANONYMOUS CALLERS — must happen here, before
    # request.form().
    #
    # request.form() parses and spools the ENTIRE body, file parts included,
    # before returning. Any check written after it (including the 401 below)
    # has already cost us the buffering it was supposed to prevent: an
    # anonymous caller could make us take 10 MB per request and pay for it
    # only in a rejection. The only thing available before parsing is the
    # declared length, so that is what the gate uses.
    #
    # Content-Length is trustworthy here in the way that matters. For a
    # non-chunked HTTP/1.1 request the ASGI server reads exactly that many
    # body bytes and no more, so a client cannot under-declare its way past
    # this and then stream more. A chunked request declares no length at
    # all, which is why anonymous chunked uploads are refused outright
    # rather than parsed and hoped about; every real client of this endpoint
    # (browser form post, the mobile app) sends a length.
    #
    # A signed-in caller is past the gate because they are already bounded
    # by the per-account rate limit below and by an identity we can revoke.
    if user is None:
        declared = request.headers.get("content-length")
        if declared is None:
            raise HTTPException(
                411, "Content-Length required — anonymous model reports must "
                     "declare their size (sign in to attach a photo)")
        try:
            declared_bytes = int(declared)
        except ValueError:
            raise HTTPException(400, "malformed Content-Length")
        if declared_bytes > _MAX_ANON_MODEL_REPORT_BYTES:
            raise HTTPException(
                413, "sign in to attach a photo — anonymous model reports are "
                     f"text-only (max {_MAX_ANON_MODEL_REPORT_BYTES // 1024} KB)")

    form = await request.form()

    def _text(name: str) -> str | None:
        v = form.get(name)
        return v.strip() if isinstance(v, str) and v.strip() else None

    device_id = _text("device_id")
    description = _text("description")
    if not device_id:
        raise HTTPException(422, "device_id is required")
    if not description:
        raise HTTPException(422, "description is required")
    if len(description) > _MAX_MODEL_DESCRIPTION:
        raise HTTPException(422, f"description too long (max {_MAX_MODEL_DESCRIPTION})")

    vehicle_identifier = _text("vehicle_identifier")
    if vehicle_identifier and not _VEHICLE_IDENTIFIER_RE.match(vehicle_identifier):
        raise HTTPException(422, "vehicle_identifier must be 16 lowercase hex chars")

    def _coord(name: str, lo: float, hi: float) -> float | None:
        raw = _text(name)
        if raw is None:
            return None
        try:
            val = float(raw)
        except ValueError:
            raise HTTPException(422, f"{name} must be a number")
        if not lo <= val <= hi:
            raise HTTPException(422, f"{name} out of range")
        return val

    lat = _coord("lat", -90, 90)
    lng = _coord("lng", -180, 180)
    # Half a coordinate pair locates nothing; storing it would just be a
    # column that lies about being usable.
    if (lat is None) != (lng is None):
        raise HTTPException(422, "lat and lng must be sent together")

    photo = form.get("photo")
    photo_bytes: bytes | None = None
    if photo is not None and not isinstance(photo, str):
        # Belt to the size gate's braces. An anonymous caller can no longer
        # get a photo-sized body this far (see _MAX_ANON_MODEL_REPORT_BYTES
        # above), so this now rejects the small-but-present photo part
        # rather than being the only thing standing between an anonymous
        # stranger and a 10 MB buffer.
        if user is None:
            raise HTTPException(401, "sign in to attach a photo — "
                                     "text-only model reports are accepted anonymously")
        photo_bytes = await photo.read() or None
        if photo_bytes and len(photo_bytes) > MAX_RECEIPT_BYTES:
            raise HTTPException(413, "photo too large (max 10 MB)")

    ip = real_client_ip(request)

    # RATE LIMIT BEFORE THE EXPENSIVE WORK, in its own committed
    # transaction.
    #
    # store_model_photo is a Pillow decode + re-encode of up to 10 MB
    # followed by an R2 PUT, and the failure path adds an R2 DELETE. Running
    # the limiter after that made the 20/hour cap protect only the INSERT —
    # the cheapest thing in the handler — while the CPU and the paid object
    # storage round-trips stayed unmetered. Metering first is the whole
    # point of having a cap here.
    #
    # The separate commit is deliberate. Sharing the insert's transaction
    # would roll the consumed quota back whenever the upload or the insert
    # failed, so a caller whose uploads keep failing would get unlimited
    # free attempts at exactly the expensive path. Quota is spent on the
    # attempt, not on the success.
    with connection() as conn:
        with conn.cursor() as cur:
            if user is not None:
                enforce(cur, bucket="model_report_account", key=str(user.account_id),
                        limit=_LIMIT_MODEL_AUTH_PER_ACCOUNT[0],
                        window_seconds=_LIMIT_MODEL_AUTH_PER_ACCOUNT[1])
            else:
                enforce(cur, bucket="model_report_ip", key=ip or "unknown",
                        limit=_LIMIT_MODEL_ANON_PER_IP[0],
                        window_seconds=_LIMIT_MODEL_ANON_PER_IP[1])
        conn.commit()

    photo_key: str | None = None
    if photo_bytes:
        if not receipts_bucket():
            raise HTTPException(503, "photo storage not configured — submit without the image")
        try:
            photo_key = store_model_photo(user.account_id if user else None, photo_bytes)
        except ReceiptError as e:
            raise HTTPException(400, str(e))

    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO model_reports (
                        account_id, device_id, vehicle_identifier, description,
                        lat, lng, photo_r2_key, reporter_ip, reporter_user_agent
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, created_at
                    """,
                    (user.account_id if user else None, device_id, vehicle_identifier,
                     description, lat, lng, photo_key, ip,
                     request.headers.get("user-agent")),
                )
                new_id, created_at = cur.fetchone()
            conn.commit()
    except Exception:
        # Don't leave the uploaded object orphaned in R2 if the row never
        # landed — mirrors the discount-report path.
        if photo_key:
            try:
                delete_receipt(photo_key)
            except ReceiptError:
                log.exception("orphaned model report photo cleanup failed for %s", photo_key)
        raise

    return {"id": int(new_id), "created_at": created_at.isoformat(),
            "photo_stored": photo_key is not None}


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
