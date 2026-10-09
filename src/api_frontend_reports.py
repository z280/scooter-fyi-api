"""Frontend report ingestion + aggregates (docs/API_REQUIREMENTS.md §3).

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
import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

import h3
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from . import geo
from .accounts import SessionUser, optional_session, require_session
from .client_ip import real_client_ip
from .identity import hash_plate
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
#
# 'inaccessible' (sql/100, docs/FLEET_REPORTS_PLAN.md §2.1): "the vehicle may
# be perfectly fine; you cannot lawfully or reasonably reach it". Since the
# owner's 2026-10-09 rules it is a negative report like the rest — the
# vehicle reads high risk until it moves 100 m (src/fleet_reports.py) — and
# it still earns no points (src/points.py): paying for a report that marks a
# vehicle high risk would pay for griefing.
_REPORT_TYPES = (
    "not_rideable", "dead_battery", "damaged", "improperly_parked", "not_found",
    "inaccessible",
)

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

# HOW LONG A REPORT COUNTS FOR (owner, 2026-10-09). The rule and its one SQL
# implementation live in src/fleet_reports.py (uncleared_negative_sql); in
# short:
#
#   * a SIGNED-IN negative report makes the vehicle high risk until it is
#     CLEARED — no time limit;
#   * an ANONYMOUS one (and every map-pin `negative_reports` row) is high risk
#     for 24 hours, then fades to UNKNOWN — never back to "ok" — until cleared;
#   * a rideability report clears on a >= 100 m move AND a charge rise, or on
#     reappearing >= 100 m away with a full battery after going off the map; a
#     location report (inaccessible, not_found) on a >= 100 m move or an
#     off-the-map reappearance >= 100 m away; a move under 100 m never clears,
#     and neither does time;
#   * an admin resolve or a rider condition check ("no longer a problem" after
#     a test ride) clears it; a rider's "still a problem" re-baselines it.
#
# Nothing hides a scooter: the only effect is the reliability label.

# Owner, 2026-10-09: every negative report — inaccessible and not_found
# included — makes the vehicle high risk; improperly_parked alone changes no
# label (it is a report to Veo). The clearing rules live in
# src/fleet_reports.py, which is now the one implementation.
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


# WHY NOT RIDEABLE (owner, 2026-10-09; sql/100). A not_rideable report may
# say why. NULL — an old client, or a rider who skipped the question — is
# "unspecified" and is accepted exactly as before.
NOT_RIDEABLE_REASONS = ("acceleration", "flat_tire", "wheel", "lighting", "seat", "handlebar")

# The picker also offers two DECOYS: choices a rider reaches for under "why
# won't it ride?" that are really different reports. The server re-files
# them, so every client gets it right whatever it sends, and keeps the
# original choice in `submitted_reason` so the remap is visible:
#   "cannot find"  -> not_found (owner, 2026-10-09: it is not where the map
#                     says; `inaccessible` stays its own type, for a scooter
#                     you can SEE but cannot reach)
#   "dead battery" -> dead_battery
NOT_RIDEABLE_DECOYS = {"cannot_find": "not_found", "dead_battery": "dead_battery"}

# observed_at — when the rider saw the problem. Optional; defaults to the
# submission time. A date in the future, or older than this, is refused: a
# month-old sighting is not evidence about the vehicle as it is now, and the
# report's hold rule (until it moves) is measured from reported_at anyway.
OBSERVED_AT_MAX_AGE = timedelta(days=30)
# Clock skew between a phone and the server is not "the future".
_OBSERVED_AT_SKEW = timedelta(minutes=10)

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
# of what was charged. Documented in docs/reference/API.md; tune here when DOTI confirms
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
    # A date ("2026-10-08", read as that day in Denver) or a timestamp. A
    # timestamp without an offset is read as UTC.
    observed_at: datetime | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    # not_rideable only: one of NOT_RIDEABLE_REASONS, or a decoy in
    # NOT_RIDEABLE_DECOYS (which re-files the report). Absent = unspecified.
    reason: str | None = Field(
        default=None,
        pattern=f"^({'|'.join(NOT_RIDEABLE_REASONS + tuple(NOT_RIDEABLE_DECOYS))})$",
    )
    # Set by the decoy remap, never by the client: the decoy that re-filed
    # this report. A private attribute, so no request body can set it.
    _submitted_reason: str | None = PrivateAttr(default=None)

    @property
    def submitted_reason(self) -> str | None:
        return self._submitted_reason

    @field_validator("observed_at", mode="before")
    @classmethod
    def _observed_date(cls, value: Any) -> Any:
        if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value.strip()):
            try:
                d = date.fromisoformat(value.strip())
            except ValueError:
                raise ValueError("observed_at is not a real date")
            return datetime(d.year, d.month, d.day, tzinfo=_DENVER)
        return value

    @field_validator("observed_at")
    @classmethod
    def _observed_window(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if value > now + _OBSERVED_AT_SKEW:
            raise ValueError("observed_at is in the future")
        if value < now - OBSERVED_AT_MAX_AGE:
            raise ValueError(
                f"observed_at is more than {OBSERVED_AT_MAX_AGE.days} days ago")
        return value

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

    @model_validator(mode="after")
    def _reason_belongs_to_not_rideable(self) -> "DeviceReportIn":
        """A reason is an answer to "why won't it ride?", so only a
        not_rideable report carries one. A decoy re-files the report under
        the type it really is and drops the reason; the choice is kept in
        submitted_reason. Runs after report_type's alias normalisation, so a
        pre-sql/037 'failed_unlock' client gets the same treatment."""
        if self.reason is None:
            return self
        if self.report_type != "not_rideable":
            raise ValueError("reason is only accepted on a not_rideable report")
        remapped = NOT_RIDEABLE_DECOYS.get(self.reason)
        if remapped is not None:
            self._submitted_reason = self.reason
            self.report_type = remapped
            self.reason = None
        return self


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

            # range_at_report_meters (sql/100): the charge as the feed last
            # published it, so the report clears on a RISE rather than on a
            # level (§2.4). Only a reading from the last hour counts as "at
            # report time"; anything older, or a vehicle not in the feed, is
            # NULL, and a NULL clears nothing. A subquery rather than a
            # separate SELECT so the handler's round trips stay as they were.
            cur.execute(
                """
                INSERT INTO device_reports (
                    vehicle_identifier, report_type, observed_at, lat, lng,
                    h3_10_index, account_id, reporter_ip, reporter_user_agent,
                    reason, submitted_reason, range_at_report_meters,
                    vehicle_lat_at_report, vehicle_lon_at_report
                ) VALUES (%s, %s, COALESCE(%s, NOW()), %s, %s, %s, %s, %s, %s, %s, %s, (
                    SELECT r.current_range_meters
                      FROM raw_telemetry_points r
                     WHERE r.vehicle_identifier = %s
                       AND r.snapshot_time >= NOW() - INTERVAL '1 hour'
                     ORDER BY r.snapshot_time DESC
                     LIMIT 1
                ),
                -- Where the VEHICLE was (sql/102): the baseline a 100 m
                -- clearing move is measured from (src/fleet_reports.py).
                (SELECT ds.current_lat FROM device_state ds
                  WHERE ds.vehicle_identifier = %s),
                (SELECT ds.current_lon FROM device_state ds
                  WHERE ds.vehicle_identifier = %s))
                RETURNING id, reported_at
                """,
                (payload.vehicle_identifier, payload.report_type, payload.observed_at,
                 payload.lat, payload.lng, h3_10,
                 user.account_id if user else None, ip, ua,
                 payload.reason, payload.submitted_reason,
                 payload.vehicle_identifier, payload.vehicle_identifier,
                 payload.vehicle_identifier),
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
    out = {"id": int(new_id), "reported_at": reported_at.isoformat(),
           "deduped": False, "points_awarded": points_awarded}
    if payload.submitted_reason is not None:
        # Say what it was filed as, so a client can word its thank-you right.
        out["report_type"] = payload.report_type
        out["remapped_from_reason"] = payload.submitted_reason
    return out


# ---------------------------------------------------------------------------
# POST /api/v1/reports/discount
# ---------------------------------------------------------------------------
class DiscountReportIn(BaseModel):
    ride_ended_at: datetime
    # 'equity' = the city's official Equity Area map (sql/091); v1/v2 are the
    # retired estimate layers, still accepted from old clients.
    zone_version: str = Field(..., pattern="^(v1|v2|equity)$")
    # `[0-9]`, NOT `\d`. Both Python's `re` and the Rust engine Pydantic uses
    # read `\d` as any Unicode decimal digit, so `EQ_\u0660\u0661\u0664`
    # (Arabic-Indic) passed this field — and sql/091's CHECK is
    # `~ '^EQ_[0-9]{3}$'`, which does not. The insert then raised a
    # CheckViolation that the handler below re-raises, so a malformed field
    # came back as a 500 instead of a 422. #117 made the receipt-claim path
    # ASCII-only for exactly this reason and this legacy field was missed.
    region_name: str | None = Field(default=None, pattern=r"^EQ_[0-9]{3}$")
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
            ("ride_ended_at", "zone_version", "region_name", "end_lat", "end_lng",
             "amount_charged_cents")
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


# ---------------------------------------------------------------------------
# Equity receipt claims (claim_version 2) — docs/PLAN_EQUITY_RECEIPTS.md
# Phase 1. A Veo receipt has the plate, minutes, costs and a charge date, but
# no location and no time of day; where the ride was comes from matching the
# feed later (Phase 2). The gate runs BEFORE any image is stored, so a claim
# that cannot show a rate error leaves nothing behind.
# ---------------------------------------------------------------------------
_CLAIM_MARKERS = ("vehicle_plate", "trip_minutes", "subtotal_cents", "total_cents", "charge_date")
_DENVER = ZoneInfo("America/Denver")


def _claim_error(status: int, error: str, **extra: Any) -> HTTPException:
    return HTTPException(status, {"error": error, **extra})


async def _submit_receipt_claim(request: Request, form: Any, user: SessionUser) -> dict[str, Any]:
    from .api_meta import _configured_pricing, _tax_rate
    from . import receipt_claims as rc

    invalid: list[str] = []

    def text(name: str) -> str | None:
        v = form.get(name)
        return v.strip() if isinstance(v, str) and v.strip() else None

    def integer(name: str, lo: int, hi: int) -> int | None:
        raw = text(name)
        if raw is None:
            return None
        # ASCII digits only: int() also takes "1_6" and other scripts' digits.
        if not re.fullmatch(r"[0-9]{1,7}", raw):
            invalid.append(name)
            return None
        v = int(raw)
        if not lo <= v <= hi:
            invalid.append(name)
            return None
        return v

    def coord(name: str, limit: float) -> float | None:
        raw = text(name)
        if raw is None:
            return None
        try:
            v = float(raw)
        except ValueError:
            invalid.append(name)
            return None
        if not -limit <= v <= limit:
            invalid.append(name)
            return None
        return v

    raw_plate = text("vehicle_plate")
    plate = rc.normalize_plate(raw_plate)
    if raw_plate is not None and plate is None:
        invalid.append("vehicle_plate")
    minutes = integer("trip_minutes", 1, rc.MAX_TRIP_MINUTES)
    subtotal = integer("subtotal_cents", 0, 100_000)
    total = integer("total_cents", 0, 100_000)
    if subtotal is not None and total is not None and total < subtotal:
        invalid.append("total_cents")

    charge_date: date | None = None
    if (raw := text("charge_date")) is not None:
        try:
            charge_date = date.fromisoformat(raw)
        except ValueError:
            invalid.append("charge_date")
        else:
            today = datetime.now(_DENVER).date()
            if not rc.EARLIEST_CHARGE_DATE <= charge_date <= today + timedelta(days=1):
                invalid.append("charge_date")
                charge_date = None

    approx_started_at: datetime | None = None
    if (raw := text("approx_started_at")) is not None:
        try:
            approx_started_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if approx_started_at.tzinfo is None:
                approx_started_at = approx_started_at.replace(tzinfo=_DENVER)
        except ValueError:
            invalid.append("approx_started_at")

    pins = {k: coord(k, 90 if k.endswith("lat") else 180)
            for k in ("pin_start_lat", "pin_start_lng", "pin_end_lat", "pin_end_lng")}
    for a, b in (("pin_start_lat", "pin_start_lng"), ("pin_end_lat", "pin_end_lng")):
        if (pins[a] is None) != (pins[b] is None):
            invalid.append(a.rsplit("_", 1)[0])

    plan = text("declared_rate_plan") or "unknown"
    if plan not in rc.DECLARED_RATE_PLANS:
        invalid.append("declared_rate_plan")

    # A tie-breaker for a ride charged on charge_date: within a day of it.
    if approx_started_at is not None and charge_date is not None:
        local_day = approx_started_at.astimezone(_DENVER).date()
        if abs((local_day - charge_date).days) > 1:
            invalid.append("approx_started_at")

    if invalid:
        raise _claim_error(422, "invalid_field", fields=sorted(set(invalid)))

    claim = rc.Claim(plate, minutes, subtotal, total, charge_date)
    missing = rc.missing_for_rate_check(claim)
    if missing:
        # Nothing is kept: no row, and the images were never uploaded.
        raise _claim_error(422, "not_rate_checkable", missing=missing)

    async def image(name: str) -> bytes | None:
        part = form.get(name)
        if part is None or isinstance(part, str):
            return None
        # Refuse on the declared size before reading the spooled part into
        # memory (this runs before the rate limit).
        if (part.size or 0) > MAX_RECEIPT_BYTES:
            raise _claim_error(413, "image_too_large", field=name, max_bytes=MAX_RECEIPT_BYTES)
        data = await part.read()
        if len(data) > MAX_RECEIPT_BYTES:
            raise _claim_error(413, "image_too_large", field=name, max_bytes=MAX_RECEIPT_BYTES)
        return data or None

    receipt_bytes = await image("receipt")
    if receipt_bytes is None:
        raise _claim_error(422, "receipt_required")
    # NO PLAN SCREENSHOT. It was required (owner, 2026-10-06) because the Equity
    # Area rate applies whatever tier you are on, so the tier is what makes a
    # claim stand — but nothing automated ever read it. The claim is checked
    # against the FEED: the arithmetic below prices the minutes at the Equity
    # Area rate, and Phases 2-3 corroborate against our own trip observations.
    # `declared_rate_plan` carries what the rider says, and we trust it (owner,
    # 2026-10-07). A `plan_evidence` part on the request is IGNORED rather than
    # rejected, so an older client keeps working; its bytes are never read and
    # never stored. See sql/095.

    ip = real_client_ip(request)
    ua = request.headers.get("user-agent")
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="discount_report_account", key=str(user.account_id),
                    limit=_LIMIT_DISCOUNT_PER_ACCOUNT[0],
                    window_seconds=_LIMIT_DISCOUNT_PER_ACCOUNT[1])
        conn.commit()

    if not receipts_bucket():
        raise _claim_error(503, "storage_unavailable")
    stored: list[str] = []
    try:
        receipt_key = store_receipt(user.account_id, receipt_bytes)
        stored.append(receipt_key)
    except Exception as e:
        # A failed PUT must leave nothing behind: cleanup_receipts only finds
        # images through table rows, so an orphan would outlive the 18 months.
        # One image now, but the loop stays — the insert below can still fail
        # after this succeeds, and that path shares it.
        for k in stored:
            try:
                delete_receipt(k)
            except Exception:  # noqa: BLE001
                log.exception("failed to clean up %s", k)
        if isinstance(e, ReceiptError):
            raise _claim_error(400, "unreadable_image", field="receipt")
        log.exception("receipt claim upload failed")
        raise _claim_error(502, "storage_unavailable")

    math = rc.arithmetic(claim, _tax_rate(_configured_pricing().get("tax_rate")))
    tax = math["tax"] or {}
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO discount_reports (
                        account_id, zone_version, claim_version,
                        vehicle_plate, vehicle_identifier, trip_minutes,
                        subtotal_cents, total_cents, charge_date, approx_started_at,
                        pin_start_lat, pin_start_lng, pin_end_lat, pin_end_lng,
                        declared_rate_plan, receipt_r2_key,
                        expected_cents, rate_error_cents, rate_signature,
                        tax_cents, tax_finding, analysis,
                        reporter_ip, reporter_user_agent
                    ) VALUES (%s, 'equity', 2, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                    RETURNING id, created_at
                    """,
                    (user.account_id, plate, hash_plate(plate), minutes,
                     subtotal, total, charge_date, approx_started_at,
                     pins["pin_start_lat"], pins["pin_start_lng"],
                     pins["pin_end_lat"], pins["pin_end_lng"],
                     plan, receipt_key,
                     math["expected_cents"], math["rate_error_cents"], math["rate_signature"],
                     tax.get("tax_cents"), tax.get("finding"), json.dumps(math),
                     ip, ua),
                )
                new_id, created_at = cur.fetchone()
            conn.commit()
    except Exception:
        for k in stored:
            try:
                delete_receipt(k)
            except ReceiptError:
                log.exception("failed to clean up orphaned %s", k)
        raise

    log.info("receipt claim id=%d account=%d minutes=%d", new_id, user.account_id, minutes)
    return {
        "id": int(new_id),
        "created_at": created_at.isoformat(),
        "status": "received",
        "receipt_stored": True,
    }


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
    ctype = (request.headers.get("content-type") or "").lower()
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        if any(form.get(k) not in (None, "") for k in _CLAIM_MARKERS):
            return await _submit_receipt_claim(request, form, user)

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
                        account_id, ride_ended_at, zone_version, region_name,
                        end_lat, end_lng, amount_charged_cents, receipt_r2_key,
                        reporter_ip, reporter_user_agent
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, created_at
                    """,
                    (user.account_id, payload.ride_ended_at, payload.zone_version,
                     payload.region_name,
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


#: Report types whose coordinates never appear in the public monthly CSV: the
#: two that steer riders away (owner, 2026-10-09), because either can point
#: at private property — the yard a scooter is fenced into, or the driveway
#: it was last reported in and is not in any more.
_UNLOCATED_IN_PUBLIC_EXPORT = frozenset({"inaccessible", "not_found"})


def _round3(v: float | None) -> float | str:
    return "" if v is None else round(float(v), 3)


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
                SELECT created_at, ride_ended_at, zone_version,
                       COALESCE(end_lat, pin_end_lat), COALESCE(end_lng, pin_end_lng),
                       COALESCE(amount_charged_cents, total_cents, subtotal_cents),
                       receipt_r2_key IS NOT NULL, region_name, vehicle_identifier
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
        # An inaccessible (or not_found) report's point can be somebody's
        # yard, garage or building. The report is about a spot being unreachable, never about
        # who lives there (docs/FLEET_REPORTS_PLAN.md §6), so its coordinates
        # stay out of the public file even at ~100 m — repeated rows at one
        # rounded point would be exactly the map of addresses the plan
        # refuses to build. The row itself stays: it is Veo's retrieval
        # obligation, and the count is the evidence.
        if rtype in _UNLOCATED_IN_PUBLIC_EXPORT:
            lat = lng = None
        w.writerow(["device", reported_at.isoformat(), vid, rtype,
                    _round3(lat), _round3(lng), "", str(bool(authed)).lower()])
    for created_at, _ride_ended, zone, lat, lng, amount, has_receipt, region, vid in discount_rows:
        # "equity:EQ_014" when the area is known (sql/091); same column, so
        # the CSV's shape is unchanged for anyone already parsing it.
        zone_cell = f"{zone}:{region}" if region else zone
        # A discount report's point is where a rider says they were (sql/093
        # pins, or the legacy end point): rounded to 3 decimals (~100 m) in
        # public, never exact. The vehicle appears only as its HMAC
        # identifier; the raw plate a receipt carries stays admin-only.
        w.writerow(["discount", created_at.isoformat(), vid or "", zone_cell,
                    _round3(lat), _round3(lng), amount if amount is not None else "",
                    str(bool(has_receipt)).lower()])

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="veo-audit-reports-{month}.csv"',
            "Cache-Control": "public, max-age=600",
        },
    )
