"""Server-side track chain verifier (PLAN_RIDE_MODE_API.md phase A2,
"Verification"; format spec RIDE_MODE_OVERHAUL_PLAN.md Part 2).

    verify_track_chain(cur, ride_row, batches) -> VerificationResult

A PURE function: no I/O, no mutation, no exceptions escape it. `cur` is
accepted (and currently unused) only to match the call shape the donation
endpoint uses for every other verification-adjacent helper in this
codebase and to leave room for a future check that legitimately needs a
read — nothing in checks 1-6 as specified needs the database, because
every fact they need already lives on `ride_row`.

Runs the six checks IN ORDER, exactly as PLAN_RIDE_MODE_API.md's A2
"Verification" section numbers them:

    1. signature   -- HMAC-SHA256 per batch + the triple ride binding
    2. chain       -- seq contiguity, prev-hash chaining, the rolling H_n
    3. monotonic   -- t0<=t1 per batch, strictly increasing across the
                      flattened track, and the server-stamped ride window
    4. speed       -- accuracy-adjusted per-segment plausibility
    5. gbfs        -- start/end correlation against the GBFS anchors
    6. volume      -- waypoint/distance/duration minimums

Checks 1 and 2 share ONE outward-facing key, "chain" (there is no
separate "signature" key in the per-check dict returned to callers —
see the golden vectors' own note on this: "The response's `verification`
dict has no separate signature key, so the observable field is
`chain`."). A failure in either is unrecoverable for everything after it
("don't run later checks against garbage" -- PLAN_RIDE_MODE_API.md): the
function stops immediately, per_check keys from "monotonic" onward stay
"skipped", chain_root_hash is None (never computed over an unverified/
misordered chain), and distance_meters/waypoint_count are 0.

From "monotonic" onward the function still stops at the FIRST failing
check (mirrors the shared golden-vector file's own contract: "expected.
failing_check is the FIRST check in pipeline_order that fails. Later
checks are unspecified.") -- EXCEPT "gbfs_end" reading "pending_feed",
which is deliberately NOT a stop condition: check 6 (volume) still runs
so a pending-feed donation's per_check dict is still fully populated (see
the golden vectors' own worked example: {"chain":"ok","monotonic":"ok",
"speed":"ok","gbfs_start":"ok","gbfs_end":"pending_feed","volume":"ok"}).
Likewise "gbfs_start" and "gbfs_end" are independent of each other and
both always run once check 4 passes, and "volume" always runs too --
none of the three depends on the others' outcome for its own arithmetic,
so populating all three gives the caller (and Screen 10's copy, which can
render multiple "because" clauses) the fullest picture rather than an
artificially truncated one.

distance_meters/waypoint_count are computed once, right after chain
integrity (check 2) succeeds -- not gated behind checks 3-6 -- because
they are metrics over trusted (chain-verified) points, not checks
themselves, and every later return carries them for free. This makes a
monotonic/speed rejection's response more informative without violating
the "don't run checks against garbage" rule, which is specifically about
not trusting UNVERIFIED points, not about withholding a plain reduction
(distance/count) over points whose signatures and chain-hashes already
checked out.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from .geo import distance_meters
from .ride_limits import clamp_distance, measure_path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Chain format constants (RIDE_MODE_OVERHAUL_PLAN.md Part 2 / the golden
# vectors' "contract" block). Kept local to this module rather than
# imported from src/api_tracked_rides.py (which owns TRACK_SIGNING_ALG for
# the *issuing* side) so this verifier has zero import-time coupling to the
# module that wires it in -- these two constants are the same values, and
# tests/test_track_verify.py pins that they match the shared fixture's own
# "contract" block, which is the actual cross-repo source of truth.
# ---------------------------------------------------------------------------
JWS_ALG = "HS256"
JWS_TYP = "sfyi-track+jws"
PAYLOAD_VERSION = 1

# --- check 3: monotonicity + bounds -----------------------------------
# t0(first) >= track_key_issued_at - this; t1(last) <= user_reported_ended_at + this.
BOUNDS_SLACK_MS = 120_000

# --- check 4: physical plausibility -------------------------------------
# Hard-reject: any accuracy-adjusted segment implying more than this.
MAX_SEGMENT_SPEED_MPS = 20.0
# Points-status flag (not a rejection): more than SUSTAINED_FAST_FRACTION
# of segments (by count) exceed this.
SUSTAINED_FAST_SEGMENT_MPS = 11.0
SUSTAINED_FAST_SEGMENT_FRACTION = 0.10
# Anti-abuse clamp (named fix from A1's review): a claimed accuracy above
# this contributes no more than this to the adjustment, so a rider cannot
# claim a huge accuracy value to erase an implausible segment's speed.
MAX_ACCURACY_ADJUSTMENT_M = 50.0

# --- check 5: GBFS correlation ------------------------------------------
GBFS_CORRELATION_RADIUS_M = 150.0
GBFS_TIME_WINDOW_MS = 10 * 60 * 1000  # +/- 10 minutes

# --- check 6: volume ------------------------------------------------------
MIN_WAYPOINTS = 10
MIN_DISTANCE_METERS = 500.0
MIN_DURATION_MS = 180_000  # 3 minutes

# The six outward-facing per-check keys, in pipeline order. "chain" covers
# both check 1 (signature) and check 2 (chain integrity) -- see module
# docstring.
CHECK_KEYS: tuple[str, ...] = (
    "chain", "monotonic", "speed", "gbfs_start", "gbfs_end", "volume",
)

# A per_check value meaning "this check never ran because an earlier one
# already stopped the pipeline" -- distinct from "ok" (ran and passed) and
# from any failure code (ran and failed), so a caller can always tell
# whether a given check was actually evaluated.
SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Public shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RideRow:
    """The subset of a `tracked_rides` row this module needs to verify one
    donation. The donation endpoint (owned by another lane) builds one of
    these from its own SELECT; nothing in this module reads the database,
    which is what keeps verify_track_chain pure and independently testable
    with a fake cursor (or, as here, no cursor use at all).

    `id` is compared against JWS `kid`/`rid` as a string -- pass
    str(tracked_rides.id) (a UUID stringifies to the same lowercase-hyphenated
    form the client embeds).

    `track_key_issued_at` and `user_reported_ended_at` must be tz-aware
    datetimes, the same convention every other TIMESTAMPTZ read in this
    codebase already follows (e.g. src/api_tracked_rides.py compares
    `watch_expires_at` straight against `datetime.now(timezone.utc)`).

    `start_lat`/`start_lon` are the ORIGINAL client-supplied ride start
    (tracked_rides.start_lat/lon) -- the check-5 fallback anchor used only
    when `feed_start_lat`/`feed_start_lon` are NULL (sql/049: the feed had
    no fresh observation at ride start).
    """
    id: str
    track_key: str | None
    track_nonce: str | None
    track_key_issued_at: datetime
    user_reported_ended_at: datetime
    start_lat: float | None
    start_lon: float | None
    feed_start_lat: float | None
    feed_start_lon: float | None
    gbfs_left_feed_at: datetime | None
    gbfs_reappeared_at: datetime | None
    gbfs_end_lat: float | None
    gbfs_end_lon: float | None


@dataclass(frozen=True)
class VerificationResult:
    """verify_track_chain's return shape. See module docstring for the
    per-check stop/continue rules that produce it.

    `verdict` is one of "eligible" | "ineligible" | "pending_feed" | "error"
    -- deliberately the SAME vocabulary as sql/049's
    tracked_rides_validation_status_allowed / src/api_tracked_rides.py's
    VALIDATION_STATUSES, minus "pending" (this function always runs to a
    decision; "pending" is the pre-donation default those modules stamp
    before any donation exists, never something this function produces).
    "error" is new here relative to the task text's illustrative
    "eligible|ineligible|pending_feed|pending_review" list -- see this
    module's docstring note below and the implementation's deviations for
    why "pending_review" is NOT a `verdict` value.

    `reasons` is a subset of the A2 reason vocabulary (start_mismatch,
    end_mismatch, tracking_not_opted, too_few_waypoints, trip_too_short,
    chain_invalid, internal_error) -- `tracking_not_opted` is never
    produced BY this function (that gate is the donation endpoint's own
    422, checked before verify_track_chain is even called) but is listed
    here because it is part of the shared vocabulary a caller may need to
    fold in alongside these reasons.

    `points_status` is "ok" | "pending_review" -- a flag INDEPENDENT of
    `verdict`, read by the points-awarding lane to hold an award even when
    the ride is otherwise "eligible". It is computed once, in check 4, and
    carried through unchanged by every later check (a later gbfs/volume
    rejection doesn't erase the physical-plausibility signal; it's simply
    moot once the ride cannot be paid at all).

    `chain_root_hash` is the final H_n, lowercase hex -- populated only
    when check 2 (chain integrity) actually completed; None when checks 1
    or 2 failed (a rejected chain has no root a caller can rely on -- see
    the module docstring's "don't run later checks against garbage").

    `failing_batch_seq` is the `seq` of the batch checks 1/2 stopped on
    (its position in `batches`, which equals its own claimed seq once
    check 2's contiguity has been confirmed up to that point -- for a
    check-1 signature failure it is simply the batch's index in the
    submitted list). None once past check 2, or when `batches` was empty.
    Exists so a 422 response can name "the failing check + batch seq" per
    PLAN_RIDE_MODE_API.md's donation-endpoint error shape.

    `per_check` always has all six CHECK_KEYS present; a key that never
    ran reads "skipped" (see SKIPPED).

    `waypoints` is the full flattened, chain-verified track -- tuples of
    `(absolute_epoch_ms, lat, lon, acc_m)`, RAW (un-accuracy-adjusted), in
    the same order `distance_meters`/`waypoint_count` were computed over.
    INTEGRATOR ADDITION, not in the original lane brief: the donation
    endpoint has to persist `donated_track_points` (sql/051) -- raw JWS
    strings are discarded after verification (RIDE_MODE_OVERHAUL_PLAN.md
    Part 2), so this is the only place those decoded points are ever
    available, and re-deriving them a second time by re-parsing the batches
    would duplicate checks 1/2's own parsing logic outside this module.
    Populated (as a tuple) the moment check 2 (chain integrity) succeeds --
    same gate as `distance_meters`/`waypoint_count` -- and carried unchanged
    through every later check; empty `()` when checks 1/2 failed or on
    verdict="error".
    """
    verdict: str
    reasons: list[str]
    chain_root_hash: str | None
    distance_meters: float
    waypoint_count: int
    per_check: dict[str, str]
    points_status: str = "ok"
    failing_batch_seq: int | None = None
    waypoints: tuple[tuple[int, float, float, float | None], ...] = ()

    def as_response(self) -> dict[str, Any]:
        """The two sub-objects the donation endpoint's response documents:
        `{"verification": {...per_check...}, "validation": {"status":
        verdict, "reasons": reasons}}`. A convenience for the integrating
        lane; verify_track_chain's caller is free to build these directly
        off the dataclass fields instead.
        """
        return {
            "verification": dict(self.per_check),
            "validation": {"status": self.verdict, "reasons": list(self.reasons)},
        }


# A flattened, chain-verified waypoint: (absolute_epoch_ms, lat, lon, acc_m).
_Point = tuple[int, float, float, float | None]


# ---------------------------------------------------------------------------
# base64url helpers (JWS uses unpadded base64url throughout)
# ---------------------------------------------------------------------------

def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ---------------------------------------------------------------------------
# Checks 1 + 2: signature + chain integrity (one outward "chain" key)
# ---------------------------------------------------------------------------

@dataclass
class _ChainOutcome:
    ok: bool
    points: list[_Point] = field(default_factory=list)
    batch_bounds: list[tuple[float, float]] = field(default_factory=list)
    chain_root_hash: str | None = None
    failing_batch_seq: int | None = None


def _decode_compact_jws(jws: str) -> tuple[str, str, dict, dict, bytes]:
    """(header_b64, payload_b64, header, payload, signature_bytes).

    Raises (ValueError, TypeError, KeyError, binascii.Error,
    json.JSONDecodeError) on any malformed input -- the caller treats every
    one of those uniformly as a check-1 (signature) failure. Malformed
    input here is attacker-controlled (the donation body), so "we could
    not even parse this" is exactly as much a signature/format failure as
    a bad HMAC, not a server bug -- see the module docstring's
    `internal_error` note.
    """
    parts = jws.split(".")
    if len(parts) != 3:
        raise ValueError("compact JWS must have exactly 3 dot-separated parts")
    header_b64, payload_b64, sig_b64 = parts
    header = json.loads(_b64url_decode(header_b64))
    payload = json.loads(_b64url_decode(payload_b64))
    sig = _b64url_decode(sig_b64)
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise ValueError("header and payload must be JSON objects")
    return header_b64, payload_b64, header, payload, sig


def _signature_binds_this_ride(
    header_b64: str, payload_b64: str, header: dict, payload: dict, sig: bytes,
    *, ride_row: RideRow, hmac_key: bytes,
) -> bool:
    """The triple binding PLAN_RIDE_MODE_API.md's check 1 requires: header
    `kid`, payload `rid`, and payload `non` must all match THIS ride, on
    top of the HMAC itself verifying under THIS ride's key -- so a chain
    built for any other ride or account fails here, not a later
    heuristic."""
    if header.get("alg") != JWS_ALG:
        return False
    if header.get("typ") != JWS_TYP:
        return False
    if header.get("kid") != ride_row.id:
        return False
    if payload.get("rid") != ride_row.id:
        return False
    if payload.get("non") != ride_row.track_nonce:
        return False
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = hmac.new(hmac_key, signing_input, hashlib.sha256).digest()
    return hmac.compare_digest(expected, sig)


def _verify_chain(batches: Sequence[str], ride_row: RideRow) -> _ChainOutcome:
    """Checks 1 (signature) and 2 (chain integrity), fully. Stops and
    returns ok=False the instant either sub-check fails on any batch --
    see module docstring. On success, `points` is the full flattened,
    ordered track and `chain_root_hash` is the final rolling H_n."""
    if not ride_row.track_key or not ride_row.track_nonce:
        # No signing material on this ride at all (predates sql/049, or a
        # private/guest ride with no server key) -- nothing to verify
        # against. Any donation against such a ride is a check-1 failure.
        return _ChainOutcome(ok=False)
    try:
        hmac_key = _b64url_decode(ride_row.track_key)
        nonce_raw = bytes.fromhex(ride_row.track_nonce)
    except (ValueError, binascii.Error):
        return _ChainOutcome(ok=False)

    # --- check 1: every batch's signature + triple binding, in order ----
    decoded: list[tuple[str, dict]] = []
    for seq, jws in enumerate(batches):
        try:
            header_b64, payload_b64, header, payload, sig = _decode_compact_jws(jws)
        except (ValueError, TypeError, KeyError, binascii.Error, json.JSONDecodeError):
            return _ChainOutcome(ok=False, failing_batch_seq=seq)
        if not _signature_binds_this_ride(
            header_b64, payload_b64, header, payload, sig,
            ride_row=ride_row, hmac_key=hmac_key,
        ):
            return _ChainOutcome(ok=False, failing_batch_seq=seq)
        decoded.append((jws, payload))

    # --- check 2: seq contiguity, prev-hash chaining, rolling H_n -------
    prev_hex = ""
    rolling_hash = hashlib.sha256(nonce_raw).digest()  # H_-1 = sha256(nonce)
    points: list[_Point] = []
    batch_bounds: list[tuple[float, float]] = []
    for seq, (jws, payload) in enumerate(decoded):
        if payload.get("seq") != seq:
            return _ChainOutcome(ok=False, failing_batch_seq=seq)
        if payload.get("prev") != prev_hex:
            return _ChainOutcome(ok=False, failing_batch_seq=seq)
        # `v`/`rec` are part of the signed schema (the golden vectors'
        # own decoded payloads carry both) but were never enforced here --
        # a stale/future/malformed producer's batches would otherwise
        # verify as if current. `type(...) is int`, not `isinstance`: bool
        # is a subclass of int in Python, so `True != 1` is False and a
        # boolean `v` would silently satisfy version 1 under `isinstance`.
        if type(payload.get("v")) is not int or payload["v"] != PAYLOAD_VERSION:
            return _ChainOutcome(ok=False, failing_batch_seq=seq)
        if type(payload.get("rec")) is not bool:
            return _ChainOutcome(ok=False, failing_batch_seq=seq)

        pts = payload.get("pts")
        t0 = payload.get("t0")
        t1 = payload.get("t1")
        if (
            not isinstance(pts, list)
            or isinstance(t0, bool) or not isinstance(t0, (int, float)) or not math.isfinite(t0)
            or isinstance(t1, bool) or not isinstance(t1, (int, float)) or not math.isfinite(t1)
        ):
            return _ChainOutcome(ok=False, failing_batch_seq=seq)
        try:
            for pt in pts:
                if not (isinstance(pt, list) and len(pt) == 4):
                    raise ValueError("malformed waypoint tuple")
                dt_ms, lat, lon, acc = pt
                if (
                    isinstance(dt_ms, bool) or not isinstance(dt_ms, (int, float))
                    or not math.isfinite(dt_ms)
                ):
                    raise ValueError("malformed dt_ms")
                if (
                    isinstance(lat, bool) or not isinstance(lat, (int, float)) or not math.isfinite(lat)
                    or isinstance(lon, bool) or not isinstance(lon, (int, float)) or not math.isfinite(lon)
                ):
                    raise ValueError("malformed lat/lon")
                # `acc` is optional: missing or a non-numeric value is
                # tolerated as "unknown" (contributes 0 to check 4's
                # adjustment -- see `_clamped_accuracy`'s own doc comment).
                # A NUMERIC-but-invalid value (NaN/Infinity/negative) is
                # NOT tolerated -- `json.loads` accepts `NaN`/`Infinity`
                # literals by default, and letting one reach check 4's
                # arithmetic unrejected can suppress the speed comparison
                # entirely (comparisons against NaN are always False).
                if isinstance(acc, bool):
                    raise ValueError("malformed acc")
                if isinstance(acc, (int, float)) and (not math.isfinite(acc) or acc < 0):
                    raise ValueError("malformed acc")
                points.append((t0 + dt_ms, float(lat), float(lon),
                                float(acc) if isinstance(acc, (int, float)) else None))
        except (ValueError, TypeError):
            return _ChainOutcome(ok=False, failing_batch_seq=seq)

        batch_bounds.append((t0, t1))
        jws_hash = hashlib.sha256(jws.encode("ascii")).digest()
        rolling_hash = hashlib.sha256(rolling_hash + jws_hash).digest()
        prev_hex = jws_hash.hex()

    return _ChainOutcome(ok=True, points=points, batch_bounds=batch_bounds,
                          chain_root_hash=rolling_hash.hex())


# ---------------------------------------------------------------------------
# Check 3: monotonicity + bounds
# ---------------------------------------------------------------------------

def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _verify_monotonic(
    points: list[_Point], batch_bounds: list[tuple[float, float]], ride_row: RideRow,
) -> str:
    """"ok" or a failure code. No dedicated reason token exists for this
    check in the A2 vocabulary (see PLAN_RIDE_MODE_API.md's golden-vector
    note on the out-of-bounds-timestamps scenario), so a failure here never
    adds anything to `reasons` -- only to `per_check["monotonic"]`."""
    for t0, t1 in batch_bounds:
        if t0 > t1:
            return "invalid_timing"

    times = [p[0] for p in points]
    if not times:
        return "invalid_timing"
    for a, b in zip(times, times[1:]):
        if b <= a:
            return "invalid_timing"

    issued_ms = _epoch_ms(ride_row.track_key_issued_at)
    ended_ms = _epoch_ms(ride_row.user_reported_ended_at)
    if times[0] < issued_ms - BOUNDS_SLACK_MS:
        return "invalid_timing"
    if times[-1] > ended_ms + BOUNDS_SLACK_MS:
        return "invalid_timing"
    return "ok"


# ---------------------------------------------------------------------------
# Check 4: physical plausibility
# ---------------------------------------------------------------------------

def _clamped_accuracy(acc: float | None) -> float:
    """The anti-abuse clamp: a claimed accuracy contributes at most
    MAX_ACCURACY_ADJUSTMENT_M to a segment's speed adjustment, no matter
    how large the client claims it is. Missing/non-numeric accuracy
    contributes nothing (0.0), same as a perfectly accurate fix would --
    it does NOT get treated as maximally inaccurate, which would make
    omitting accuracy a cheaper way to erase distance than clamping was
    built to prevent. Defensive belt-and-suspenders: `_verify_chain`'s own
    parsing loop already rejects a non-finite/negative `acc` outright
    before any point reaches here, but a non-finite value is treated the
    same as "unknown" (0.0) rather than propagated, in case this function
    is ever called from a path that skips that parse-time rejection."""
    if acc is None or not math.isfinite(acc) or acc < 0:
        return 0.0
    return min(acc, MAX_ACCURACY_ADJUSTMENT_M)


def _verify_speed(points: list[_Point]) -> tuple[str, bool]:
    """(per_check status, pending_review flag).

    Per-segment speed = haversine(p1, p2) / dt, accuracy-adjusted: each
    point's accuracy is clamped to MAX_ACCURACY_ADJUSTMENT_M and the pair's
    clamped accuracies are subtracted from the raw segment distance BEFORE
    dividing by dt (never below zero -- a segment cannot have negative
    distance). `distance_meters` (src/geo.py) is the same flat-earth
    approximation the rest of this codebase's distance math already uses
    (src/ride_limits.py, src/points.py's GBFS-validation check) -- accurate
    enough at Denver-ride scales and kept consistent with the codebase's
    one distance primitive rather than introducing a second (haversine)
    implementation that could disagree with it at the margins.

    `pending_review` is a flag, not a rejection: True when more than
    SUSTAINED_FAST_SEGMENT_FRACTION of segments (by count) exceed
    SUSTAINED_FAST_SEGMENT_MPS. It is computed over ALL segments
    regardless of whether the hard-reject below also fires, but is moot in
    that case (the caller never reaches "eligible" for a ride whose check
    4 status isn't "ok", so the flag's value there is inert)."""
    if len(points) < 2:
        return "ok", False

    total_segments = 0
    fast_sustained = 0
    for (t1, lat1, lon1, acc1), (t2, lat2, lon2, acc2) in zip(points, points[1:]):
        dt_s = (t2 - t1) / 1000.0
        if dt_s <= 0:
            # Guaranteed not to happen once check 3 has passed (strictly
            # increasing timestamps) -- guarded here too so this function
            # never divides by zero if ever called out of the documented
            # check-3-then-check-4 order.
            return "implausible_speed", False

        raw = distance_meters(lat1, lon1, lat2, lon2)
        adjusted = max(0.0, raw - _clamped_accuracy(acc1) - _clamped_accuracy(acc2))
        speed = adjusted / dt_s

        total_segments += 1
        if speed > MAX_SEGMENT_SPEED_MPS:
            return "implausible_speed", False
        if speed > SUSTAINED_FAST_SEGMENT_MPS:
            fast_sustained += 1

    pending_review = (
        total_segments > 0
        and (fast_sustained / total_segments) > SUSTAINED_FAST_SEGMENT_FRACTION
    )
    return "ok", pending_review


# ---------------------------------------------------------------------------
# Check 5: GBFS correlation
# ---------------------------------------------------------------------------

def _verify_gbfs_start(points: list[_Point], ride_row: RideRow) -> str:
    """"ok" or "start_mismatch". Anchor is feed_start_lat/lon when A1's
    start handler stamped them (a fresh feed observation existed at ride
    start), falling back to the client-supplied start_lat/lon only when
    the feed columns are NULL -- the weaker, client-vs-client check, kept
    only so a pre-sql/049 or feed-miss ride is still verifiable at all."""
    if not points:
        return "start_mismatch"
    _, lat, lon, _ = points[0]

    anchor_lat = ride_row.feed_start_lat if ride_row.feed_start_lat is not None else ride_row.start_lat
    anchor_lon = ride_row.feed_start_lon if ride_row.feed_start_lon is not None else ride_row.start_lon
    if anchor_lat is None or anchor_lon is None:
        return "start_mismatch"
    if distance_meters(lat, lon, anchor_lat, anchor_lon) > GBFS_CORRELATION_RADIUS_M:
        return "start_mismatch"

    if ride_row.gbfs_left_feed_at is not None:
        issued_ms = _epoch_ms(ride_row.track_key_issued_at)
        left_ms = _epoch_ms(ride_row.gbfs_left_feed_at)
        if abs(issued_ms - left_ms) > GBFS_TIME_WINDOW_MS:
            return "start_mismatch"
    return "ok"


def _verify_gbfs_end(points: list[_Point], ride_row: RideRow) -> str:
    """"ok" | "end_mismatch" | "pending_feed". "pending_feed" is a STATUS,
    not a failure -- see CHECK_KEYS / module docstring: it does not stop
    the pipeline and contributes no reason token."""
    if ride_row.gbfs_reappeared_at is None:
        return "pending_feed"
    if not points or ride_row.gbfs_end_lat is None or ride_row.gbfs_end_lon is None:
        return "end_mismatch"

    last_ms, lat, lon, _ = points[-1]
    if distance_meters(lat, lon, ride_row.gbfs_end_lat, ride_row.gbfs_end_lon) > GBFS_CORRELATION_RADIUS_M:
        return "end_mismatch"
    reappeared_ms = _epoch_ms(ride_row.gbfs_reappeared_at)
    if abs(reappeared_ms - last_ms) > GBFS_TIME_WINDOW_MS:
        return "end_mismatch"
    return "ok"


# ---------------------------------------------------------------------------
# Check 6: volume
# ---------------------------------------------------------------------------

def _verify_volume(points: list[_Point], distance_m: float) -> list[str]:
    """The reasons list for check 6 -- BOTH `too_few_waypoints` and
    `trip_too_short` are included whenever both their conditions hold
    (NOT an exclusive pick): the shared golden vectors' own
    volume-too-few-waypoints scenario asserts exactly this
    ("This chain misses all three [minimums], so both reasons apply;
    assert reasons as a set."). too_few_waypoints is waypoint_count alone;
    trip_too_short is distance OR duration alone -- the two tokens never
    overlap in what they describe, so there is no real ambiguity to
    tie-break: a ride can independently be both too-sparse and too-short,
    and says so."""
    reasons: list[str] = []
    if len(points) < MIN_WAYPOINTS:
        reasons.append("too_few_waypoints")

    duration_ms = (points[-1][0] - points[0][0]) if len(points) >= 2 else 0
    if distance_m < MIN_DISTANCE_METERS or duration_ms < MIN_DURATION_MS:
        reasons.append("trip_too_short")
    return reasons


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _empty_per_check() -> dict[str, str]:
    return {key: SKIPPED for key in CHECK_KEYS}


def _verify_track_chain(ride_row: RideRow, batches: Sequence[str]) -> VerificationResult:
    per_check = _empty_per_check()

    chain = _verify_chain(batches, ride_row)
    if not chain.ok:
        per_check["chain"] = "chain_invalid"
        return VerificationResult(
            verdict="ineligible", reasons=["chain_invalid"],
            chain_root_hash=None, distance_meters=0.0, waypoint_count=0,
            per_check=per_check, failing_batch_seq=chain.failing_batch_seq,
        )
    per_check["chain"] = "ok"

    # Metrics over trusted points -- see module docstring for why these
    # are computed unconditionally here rather than gated behind checks
    # 3-6. RAW, un-adjusted points: the accuracy adjustment belongs to
    # check 4's speed math only (PLAN_RIDE_MODE_API.md is explicit that
    # the distance this function reports must not carry it).
    path = [(lat, lon) for (_, lat, lon, _) in chain.points]
    measured, _excluded_legs = measure_path(path, cap_legs=True)
    distance_m, _clamped_from = clamp_distance(measured)
    waypoint_count = len(chain.points)
    waypoints = tuple(chain.points)

    monotonic_status = _verify_monotonic(chain.points, chain.batch_bounds, ride_row)
    per_check["monotonic"] = monotonic_status
    if monotonic_status != "ok":
        return VerificationResult(
            verdict="ineligible", reasons=[],
            chain_root_hash=chain.chain_root_hash,
            distance_meters=distance_m, waypoint_count=waypoint_count,
            per_check=per_check, waypoints=waypoints,
        )

    speed_status, pending_review = _verify_speed(chain.points)
    per_check["speed"] = speed_status
    points_status = "pending_review" if pending_review else "ok"
    if speed_status != "ok":
        return VerificationResult(
            verdict="ineligible", reasons=[],
            chain_root_hash=chain.chain_root_hash,
            distance_meters=distance_m, waypoint_count=waypoint_count,
            per_check=per_check, points_status=points_status, waypoints=waypoints,
        )

    gbfs_start_status = _verify_gbfs_start(chain.points, ride_row)
    per_check["gbfs_start"] = gbfs_start_status

    gbfs_end_status = _verify_gbfs_end(chain.points, ride_row)
    per_check["gbfs_end"] = gbfs_end_status

    volume_reasons = _verify_volume(chain.points, distance_m)
    per_check["volume"] = volume_reasons[0] if volume_reasons else "ok"

    reasons: list[str] = []
    if gbfs_start_status == "start_mismatch":
        reasons.append("start_mismatch")
    if gbfs_end_status == "end_mismatch":
        reasons.append("end_mismatch")
    reasons.extend(volume_reasons)

    if reasons:
        verdict = "ineligible"
    elif gbfs_end_status == "pending_feed":
        verdict = "pending_feed"
    else:
        verdict = "eligible"

    return VerificationResult(
        verdict=verdict, reasons=reasons,
        chain_root_hash=chain.chain_root_hash,
        distance_meters=distance_m, waypoint_count=waypoint_count,
        per_check=per_check, points_status=points_status, waypoints=waypoints,
    )


def verify_track_chain(cur: Any, ride_row: RideRow, batches: Sequence[str]) -> VerificationResult:
    """Entry point. See module docstring for the check pipeline and
    per_check/verdict semantics.

    Never raises: any unexpected exception (malformed `ride_row`, a bug in
    this module, anything checks 1/2's own defensive parsing didn't
    already catch as a signature failure) is caught here and turned into
    verdict="error", reasons=["internal_error"] -- the A2 reason
    vocabulary's `internal_error` entry exists for exactly this, and a
    verifier that can crash the donation endpoint on attacker-controlled
    input would defeat the entire point of validating that input.
    """
    try:
        return _verify_track_chain(ride_row, batches)
    except Exception:
        # getattr, not ride_row.id: a malformed ride_row (wrong type
        # entirely, not just a bad field value) is exactly the kind of
        # caller bug this branch exists to survive -- the logging call
        # itself must not be a second way to raise.
        log.exception(
            "track_verify: unexpected exception verifying ride %s",
            getattr(ride_row, "id", "<unknown>"),
        )
        return VerificationResult(
            verdict="error", reasons=["internal_error"],
            chain_root_hash=None, distance_meters=0.0, waypoint_count=0,
            per_check=_empty_per_check(),
        )
