"""Device feature confirmations — the write side of sql/055.

    POST /api/v1/reports/device-features    rider confirms what's bolted on
    GET  /api/v1/devices/{vid}/features     current consensus for one vehicle

Veo's feed says nothing about baskets, bells or cup holders, so there is no
way to filter the map for them. Riders standing next to a scooter can see
the answer in a second, so the "☑️ Confirm Features" modal asks them, and
the fleet becomes filterable on equipment for the first time.

WHAT THIS MODULE DOES AND DOES NOT DO --------------------------------------
It writes ONE row to `device_feature_reports` and, for an authenticated
reporter who typed the right plate, ONE row to `user_points`. That is all.
It never grades a report against anything, never touches the feature columns
on `device_state`, and never changes a `feature_status` — every one of those
belongs to the ten-minute processor (`src/device_features.py`), which is the
single writer of the consensus. See that module's header for the state
machine and why the split exists.

THE PLATE, AND WHY A WRONG ONE IS A 200 -----------------------------------
The last question in the modal is "enter the plate number under the QR code
on the device", and it is the whole anti-abuse story: you cannot confirm a
scooter's features from your sofa, because you cannot read its plate from
there. The owner's rule is "we will accept but give no points for wrong
entered plate numbers", so a mismatch is NOT a 4xx — the report is stored
with `plate_valid = false`, the response says `plate_valid: false` and
`points_awarded: 0`, and the processor ignores the row forever.

Storing rather than rejecting matters: a rash of near-miss plates on
adjacent vehicles is a rider mixing up two scooters parked side by side,
which is a real data-quality signal, and rejecting the request would throw
it away. It also means the endpoint gives an attacker no oracle worth
having — every submission looks the same from the outside except for one
boolean the honest client already knew the answer to.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import h3
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field, model_validator

from .accounts import SessionUser, optional_session
from .client_ip import real_client_ip
from .device_features import (
    FEATURE_KEYS,
    FEATURE_PRESENCE_COLUMNS,
    STATUS_NEEDS_CONFIRMED,
    canonical_poor,
)
from .identity import hash_plate
from .pg import connection
from .points import credit_device_feature_points
from .qr import extract_plate
from .ratelimit import enforce

log = logging.getLogger(__name__)

router = APIRouter()

_VEHICLE_IDENTIFIER_RE = r"^[0-9a-f]{16}$"

# Anonymous reports are allowed (they still carry data) but earn nothing and
# are metered hard. The authenticated bucket is deliberately generous: a
# rider working a block of scooters can legitimately confirm a dozen in an
# hour, and that is exactly the behaviour the leaderboard is trying to buy.
_LIMIT_FEATURES_ANON_PER_IP = (5, 3600)
_LIMIT_FEATURES_AUTH_PER_ACCOUNT = (40, 3600)

# An identical resubmission from the same reporter inside this window is a
# no-op — same shape as the device-report dedupe, and for the same reason: a
# double-tapped Send must not burn rate-limit quota or write a second vote.
_DEDUPE_WINDOW_MINUTES = 30


def normalise_plate(raw: str) -> str:
    """Fold a typed plate to the form we compare on.

    Veo plates are printed as bare digits ("1025543"), but riders type them
    with whatever they see and whatever their keyboard does: spaces, a
    stray hyphen, a leading '#'. Comparing on alphanumerics-only,
    case-folded, means a rider who typed "#1025543" is right — because they
    ARE right, they read the correct plate off the correct scooter, which
    is the only thing this check is actually asking.
    """
    return re.sub(r"[^0-9a-z]", "", raw.strip().lower())


class DeviceFeatureReportIn(BaseModel):
    """The presence toggles, the condition follow-up, and the plate.

    Every presence toggle is REQUIRED except `has_basket`. "Neither pressed
    by default" is a rule about the modal's initial state, not permission to
    send a half-answered survey — a missing answer is a 422 here, and the
    client keeps its Send button disabled until every toggle is pressed.

    `has_basket` is optional ONLY because the question is newer than the
    clients (sql/058). Making it required would 422 every report from the
    frontend already in the wild, which asks three questions and knows
    nothing about a fourth. Omitting it is an ABSTENTION, not a "no": the
    row stores NULL, and `src/device_features.py` excludes the field from
    that report's agreement check and from the consensus vote. Once no
    deployed client omits it, this can become required like the others.

    THE QR ALTERNATIVE (sql/067). `qr_raw_value` is a scanned sticker
    payload and stands in for the typed plate as proof-of-presence — so
    with a scan attached, `submitted_plate` becomes optional, and so does
    `vehicle_identifier` itself: the tools-drawer flow scans a scooter the
    rider never tapped on the map, and the QR is the only identity it has.
    A report must carry at least one of the two identities, and at least
    one of the two proofs; either gap is a 422 here, not a guess later.
    """
    vehicle_identifier: str | None = Field(
        default=None, pattern=_VEHICLE_IDENTIFIER_RE,
    )
    #: The rotating GBFS bike_id the client had on screen. Audit only.
    device_id: str | None = Field(default=None, max_length=128)
    submitted_plate: str | None = Field(default=None, min_length=1, max_length=64)
    #: Decoded QR payload, verbatim — same bounds as api_qr.py's QrScanIn.
    qr_raw_value: str | None = Field(default=None, min_length=1, max_length=2000)
    has_bell: bool
    has_cup_holder: bool
    has_phone_holder: bool
    has_basket: bool | None = None
    all_good_condition: bool
    poor_condition: list[str] = Field(default_factory=list, max_length=len(FEATURE_KEYS))
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def _check_condition(self) -> "DeviceFeatureReportIn":
        if self.vehicle_identifier is None and self.qr_raw_value is None:
            raise ValueError(
                "send vehicle_identifier, qr_raw_value, or both — a report "
                "with neither has no scooter to attach to"
            )
        if self.submitted_plate is None and self.qr_raw_value is None:
            raise ValueError(
                "send submitted_plate or qr_raw_value — one of the two is "
                "the proof you were standing at the scooter"
            )
        unknown = [k for k in self.poor_condition if k not in FEATURE_KEYS]
        if unknown:
            raise ValueError(
                f"unknown feature key(s): {', '.join(sorted(unknown))}. "
                f"Valid: {', '.join(FEATURE_KEYS)}"
            )
        present = {
            k: getattr(self, FEATURE_PRESENCE_COLUMNS[k]) for k in FEATURE_KEYS
        }
        # A feature the report abstained on (`None`) is not present for this
        # purpose: a client that never asked about baskets cannot coherently
        # report a broken one, and `FeatureAnswers.normalise()` would strip
        # the claim downstream anyway.
        absent = [k for k in FEATURE_KEYS if k in set(self.poor_condition) and not present[k]]
        if absent:
            raise ValueError(
                "poor_condition may only name features this report says are "
                f"present; got: {', '.join(absent)}"
            )
        # `all_good_condition` and an empty `poor_condition` must agree.
        # src/device_features.py's FeatureAnswers.normalise() derives one
        # from the other (rule 2 there — device_state stores only the list,
        # so a disagreement would round-trip lossily and ping-pong the
        # vehicle into needs_review forever). Rejecting the contradiction
        # here rather than silently normalising it means a client with a bug
        # is told about it instead of having its blanket answer quietly
        # overridden.
        # Canonicalised in FEATURE_KEYS order, NOT `sorted()`, by the same
        # `canonical_poor` the processor uses.
        #
        # The two USED to agree by accident, because the original vocabulary
        # was alphabetical. sql/058's "basket" is the key that broke the
        # coincidence — exactly the case the previous note here anticipated —
        # and `FeatureAnswers.normalise()` was ordering lexicographically at
        # the time, so both sides moved to the shared helper rather than one
        # of them being trusted to stay in step by hand. The dedupe probe
        # below compares `poor_condition = %s` against a stored array
        # literally, so two orderings for one answer would mean a
        # double-tapped Send writes a second vote.
        deduped = list(canonical_poor(self.poor_condition))
        if self.all_good_condition and deduped:
            raise ValueError(
                "all_good_condition is true but poor_condition names "
                f"{', '.join(deduped)} — send one or the other"
            )
        if not self.all_good_condition and not deduped:
            raise ValueError(
                "all_good_condition is false but poor_condition is empty — "
                "name which present feature is not in good condition (if "
                "none are, the answer is all_good_condition: true)"
            )
        self.poor_condition = deduped
        return self


@router.post("/api/v1/reports/device-features")
def submit_device_feature_report(
    request: Request,
    payload: DeviceFeatureReportIn = Body(...),
    user: SessionUser | None = Depends(optional_session),
) -> dict[str, Any]:
    """Log one feature confirmation and award its points.

    Returns the award tier that was paid and the status the vehicle carried
    when the report landed, so the modal can say "+14 — thanks for clearing
    a review!" rather than a generic thank-you. `feature_status` in the
    response is the status BEFORE this report, which is deliberate: the
    status after is not knowable until the processor runs, up to ten minutes
    later, and promising the rider a status we have not computed yet would
    be the one thing worse than a stale one.

    THE QR RULE (sql/067). A scanned sticker is the plate with the typo
    removed, so a scan that resolves to a known vehicle validates the
    report outright (`plate_valid = true`) — and it also OUTVOTES the
    client's claim about which vehicle this is: if the rider tapped one
    scooter and scanned its neighbour, the answers describe the scooter
    they were standing at, so the report attaches to the vehicle the QR
    names, with the tapped one kept in `claimed_vehicle_identifier` for
    audit. A scan that resolves to nothing falls back to the claimed
    vehicle and the typed-plate rule (the sticker may be damaged, or Veo
    may have changed the payload shape); with no claimed vehicle to fall
    back to — the tools-drawer flow — that is a 404, because there is
    nothing for the report to attach to. The response's
    `vehicle_identifier` is always the vehicle the report actually
    attached to, so the client can tell the rider when a re-target
    happened.
    """
    ip = real_client_ip(request)
    ua = request.headers.get("user-agent")
    typed = normalise_plate(payload.submitted_plate or "")

    # Resolve the scan before touching the database: both halves are pure.
    # extract_plate's whole-payload fallback means qr_plate is only None for
    # an effectively empty scan; an unrecognized payload shape simply hashes
    # to an identifier no vehicle has, which the lookup below treats the
    # same as any other non-match.
    qr_plate = extract_plate(payload.qr_raw_value) if payload.qr_raw_value else None
    qr_vid = hash_plate(qr_plate) if qr_plate else None

    state_sql = """
        SELECT vehicle_plate, feature_status, current_h3_10_index,
               current_lat, current_lon
          FROM device_state
         WHERE vehicle_identifier = %s
    """

    with connection() as conn:
        with conn.cursor() as cur:
            # Which vehicle is this report about? The QR's answer wins when
            # it resolves; the client's claim is the fallback. Decided first
            # because everything downstream — the dedupe probe, the status
            # that picks the award, the row itself — is keyed on the target.
            state = None
            target_vid = payload.vehicle_identifier
            qr_matched: bool | None = None
            claimed_vid_for_row: str | None = None
            if payload.qr_raw_value is not None:
                qr_matched = False
                if qr_vid is not None:
                    cur.execute(state_sql, (qr_vid,))
                    state = cur.fetchone()
                    if state is not None:
                        qr_matched = True
                        if (
                            payload.vehicle_identifier is not None
                            and payload.vehicle_identifier != qr_vid
                        ):
                            claimed_vid_for_row = payload.vehicle_identifier
                        target_vid = qr_vid
                if state is None and payload.vehicle_identifier is None:
                    raise HTTPException(
                        404,
                        detail="scanned QR does not match any known scooter",
                    )
            assert target_vid is not None  # the model validator guarantees it
            # Dedupe BEFORE metering — a double-tapped Send is not evidence
            # and must not spend an anonymous reporter's 5/hour budget. Same
            # ordering and rationale as submit_device_report.
            if user is not None:
                reporter_clause, reporter_val = "account_id = %s", user.account_id
            else:
                reporter_clause, reporter_val = (
                    "account_id IS NULL AND reporter_ip = %s", ip,
                )
            cur.execute(
                f"""
                SELECT id, reported_at, plate_valid, points_awarded, status_at_report
                  FROM device_feature_reports
                 WHERE vehicle_identifier = %s
                   AND {reporter_clause}
                   AND reported_at >= NOW() - INTERVAL '{_DEDUPE_WINDOW_MINUTES} minutes'
                   AND has_bell = %s AND has_cup_holder = %s
                   AND has_phone_holder = %s AND poor_condition = %s
                   -- IS NOT DISTINCT FROM, not `=`: has_basket is NULL for
                   -- a report from a client that never asked (sql/058), and
                   -- `NULL = NULL` is NULL, so `=` would never match those
                   -- rows and every retry from an older client would write a
                   -- fresh vote instead of deduping.
                   AND has_basket IS NOT DISTINCT FROM %s
                 ORDER BY reported_at DESC LIMIT 1
                """,
                (
                    target_vid, reporter_val,
                    payload.has_bell, payload.has_cup_holder,
                    payload.has_phone_holder, payload.poor_condition,
                    payload.has_basket,
                ),
            )
            dup = cur.fetchone()
            if dup:
                return {
                    "id": int(dup[0]),
                    "reported_at": dup[1].isoformat(),
                    "deduped": True,
                    "plate_valid": bool(dup[2]),
                    "points_awarded": int(dup[3]),
                    "feature_status": dup[4],
                    "vehicle_identifier": target_vid,
                    "qr_matched": qr_matched,
                }

            if user is None:
                enforce(cur, bucket="device_features_ip", key=ip or "?",
                        limit=_LIMIT_FEATURES_ANON_PER_IP[0],
                        window_seconds=_LIMIT_FEATURES_ANON_PER_IP[1])
            else:
                enforce(cur, bucket="device_features_account",
                        key=str(user.account_id),
                        limit=_LIMIT_FEATURES_AUTH_PER_ACCOUNT[0],
                        window_seconds=_LIMIT_FEATURES_AUTH_PER_ACCOUNT[1])

            # Already fetched when the QR resolved; otherwise look the
            # target up now (the claimed vehicle, whether or not a
            # non-resolving scan rode along).
            if state is None:
                cur.execute(state_sql, (target_vid,))
                state = cur.fetchone()
            if state is None:
                # Unlike the plate check, this IS a hard error: we have no
                # record of the vehicle at all, so there is nothing for the
                # report to ever attach to and no status to award against.
                raise HTTPException(404, detail="unknown vehicle_identifier")

            stored_plate, status, h3_10, state_lat, state_lon = state
            status = status or STATUS_NEEDS_CONFIRMED
            # A resolved scan IS the plate check, passed by construction:
            # the target was found by hashing the plate the sticker encodes.
            plate_valid = qr_matched is True or (
                bool(stored_plate) and bool(typed)
                and normalise_plate(stored_plate) == typed
            )

            # Location for the points row: the reporter's own fix when the
            # client sent one (they are standing at the scooter, so it is the
            # better anchor), else the vehicle's last known position.
            lat = payload.lat if payload.lat is not None else state_lat
            lng = payload.lng if payload.lng is not None else state_lon
            if (lat is None or lng is None) and h3_10 is not None:
                lat, lng = h3.cell_to_latlng(h3.int_to_str(int(h3_10)))

            # submitted_plate stays NOT NULL and verbatim-as-typed; a
            # QR-only report stores the plate the sticker encoded, which is
            # what the rider "submitted" by pointing a camera at it — but
            # only when the scan actually RESOLVED. extract_plate falls back
            # to the whole trimmed payload for unrecognized shapes, and an
            # unresolved scan's "plate" is therefore likely a full URL;
            # qr_raw_value already stores that verbatim, so copying it here
            # would just make this column noisier. Empty string instead:
            # nothing plate-shaped was submitted.
            plate_for_row = (
                payload.submitted_plate.strip()
                if payload.submitted_plate is not None
                else (qr_plate or "") if qr_matched is True else ""
            )
            cur.execute(
                """
                INSERT INTO device_feature_reports (
                    vehicle_identifier, device_id, account_id, reporter_ip,
                    reporter_user_agent, submitted_plate, plate_valid,
                    has_bell, has_cup_holder, has_phone_holder, has_basket,
                    all_good_condition, poor_condition, status_at_report,
                    qr_raw_value, claimed_vehicle_identifier
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s)
                RETURNING id, reported_at
                """,
                (
                    target_vid, payload.device_id,
                    user.account_id if user else None, ip, ua,
                    plate_for_row, plate_valid,
                    payload.has_bell, payload.has_cup_holder,
                    payload.has_phone_holder, payload.has_basket,
                    payload.all_good_condition,
                    payload.poor_condition, status,
                    payload.qr_raw_value, claimed_vid_for_row,
                ),
            )
            new_id, reported_at = cur.fetchone()

            # A resolved scan also refreshes the per-device QR registry
            # (sql/032) — same upsert api_qr.py performs, minus the RETURNING
            # (nothing here reads the counters) and minus the sign-in
            # requirement: the registry's account columns are nullable, and
            # an anonymous scan of a real sticker is still a real sticker.
            if qr_matched is True:
                cur.execute(
                    """
                    INSERT INTO device_qr_codes (
                        vehicle_identifier, qr_raw_value,
                        first_scanned_by, last_scanned_by
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (vehicle_identifier) DO UPDATE SET
                        qr_raw_value = EXCLUDED.qr_raw_value,
                        -- COALESCE, unlike api_qr.py's plain assignment:
                        -- that endpoint requires a session, this one
                        -- doesn't, and an anonymous scan overwriting a
                        -- real account id with NULL would erase
                        -- attribution rather than add to it.
                        last_scanned_by = COALESCE(
                            EXCLUDED.last_scanned_by,
                            device_qr_codes.last_scanned_by
                        ),
                        last_scanned_at = NOW(),
                        scan_count = device_qr_codes.scan_count + 1
                    """,
                    (
                        target_vid, payload.qr_raw_value,
                        user.account_id if user else None,
                        user.account_id if user else None,
                    ),
                )

            # Points: authenticated AND right plate AND a resolvable
            # location. Anonymous reports and wrong-plate reports are stored
            # and earn nothing — the first because points are never
            # anonymous (sql/028), the second because that is the rule the
            # plate question exists to enforce.
            points_awarded = 0
            if user is not None and plate_valid and lat is not None and lng is not None:
                credited = credit_device_feature_points(
                    cur, account_id=user.account_id, feature_status=status,
                    lat=lat, lng=lng,
                    vehicle_identifier=target_vid,
                    report_id=int(new_id),
                )
                points_awarded = credited["points"] if credited else 0
                if points_awarded:
                    cur.execute(
                        "UPDATE device_feature_reports SET points_awarded = %s "
                        "WHERE id = %s",
                        (points_awarded, new_id),
                    )
        conn.commit()

    log.info(
        "device feature report id=%d vehicle=%s status=%s plate_valid=%s "
        "auth=%s points=%d qr=%s%s",
        new_id, target_vid, status, plate_valid,
        user is not None, points_awarded,
        "none" if qr_matched is None else ("matched" if qr_matched else "miss"),
        f" claimed={claimed_vid_for_row}" if claimed_vid_for_row else "",
    )
    return {
        "id": int(new_id),
        "reported_at": reported_at.isoformat(),
        "deduped": False,
        "plate_valid": plate_valid,
        "points_awarded": points_awarded,
        "feature_status": status,
        # The vehicle the report actually attached to — differs from the
        # request's claim exactly when the QR re-targeted it. qr_matched is
        # None (no scan) / True (resolved, validates the report) / False
        # (scan sent but unresolvable; typed-plate rules applied).
        "vehicle_identifier": target_vid,
        "qr_matched": qr_matched,
    }


@router.get("/api/v1/devices/{vehicle_identifier}/features")
def device_features(
    vehicle_identifier: str = Path(..., pattern=_VEHICLE_IDENTIFIER_RE),
) -> dict[str, Any]:
    """Current consensus for one vehicle — the same fields
    `/api/v1/devices/current` carries, fetched for a single device.

    Exists so the Confirm Features modal can render an up-to-the-second
    status when it opens, rather than whatever the map's 90-second poll last
    saw. Public: it is the same data the map payload already publishes to
    everyone, and no plate appears in it.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT feature_status, has_bell, has_cup_holder,
                       has_phone_holder, features_poor_condition,
                       features_confirmed_at, features_report_count,
                       has_basket
                  FROM device_state
                 WHERE vehicle_identifier = %s
                """,
                (vehicle_identifier,),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(404, detail="unknown vehicle_identifier")
    return {
        "vehicle_identifier": vehicle_identifier,
        "feature_status": row[0] or STATUS_NEEDS_CONFIRMED,
        "features": feature_payload(row[1], row[2], row[3], row[4], row[7]),
        "confirmed_at": row[5].isoformat() if row[5] else None,
        "report_count": int(row[6] or 0),
    }


def feature_payload(
    has_bell: Any, has_cup_holder: Any, has_phone_holder: Any,
    poor_condition: Any, has_basket: Any = None,
) -> dict[str, Any] | None:
    """The `device_features` object for a map feature / detail response, or
    None when nobody has confirmed anything about this vehicle yet.

    Returning None rather than an all-false object is the point: false would
    claim we know a scooter has no bell, when the truth is that nobody has
    looked. `feature_status: 'needs_features_confirmed'` says exactly that,
    and this returning None is the same statement in the shape a client
    checks with one `if`.

    The same honesty applies PER FEATURE inside the object: a field the
    stored consensus has no answer for serializes as null — unknown — never
    false. This used to be all-or-nothing (any object implied every field
    was answered, and a pre-058 consensus's unknown basket rode along as
    `false`), which was tolerable while the only possible gap was one
    feature on vehicles confirmed before the basket question existed. It
    stopped being tolerable when partial knowledge became a first-class
    state: a ride-survey basket answer (sql/065) or the Rover catalog seed
    (sql/066) knows ONLY the basket, and an all-or-nothing object would
    publish confident "no bell / no cup holder / no phone holder" claims
    for an entire model cohort nobody has looked at. Clients that check
    `=== true` (the equipment filter's semantics) read null and false
    identically, which is the correct reading for a filter: an unknown
    feature doesn't match a "must have it" requirement.

    Shared with src/api_public.py's payload builder so the two cannot drift
    on the object's shape.
    """
    if (
        has_bell is None and has_cup_holder is None
        and has_phone_holder is None and has_basket is None
    ):
        return None

    def tri(v: Any) -> bool | None:
        return None if v is None else bool(v)

    return {
        "bell": tri(has_bell),
        "cup_holder": tri(has_cup_holder),
        "phone_holder": tri(has_phone_holder),
        "basket": tri(has_basket),
        "poor_condition": sorted(poor_condition or []),
    }
