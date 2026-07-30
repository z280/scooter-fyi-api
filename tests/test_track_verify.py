"""Tests for src/track_verify.py -- the server-side track chain verifier
(PLAN_RIDE_MODE_API.md phase A2, "Verification"; format spec
RIDE_MODE_OVERHAUL_PLAN.md Part 2).

Two families of tests:

1. GOLDEN VECTORS (`test_golden_vector`, parametrized over
   tests/fixtures/track-chain-vectors.json's `scenarios`) -- the shared,
   byte-identical, cross-repo fixture. Every scenario it encodes is
   asserted here (valid, flipped-bit, foreign-key-signed, reordered,
   teleport, out-of-bounds timestamps, recovered-batch, truncated-tail,
   volume-too-few-waypoints).

   `truncated-tail` gets special handling -- see `_assert_truncated_tail`
   for the full reasoning, confirmed by hand against the real GBFS-end
   math before being written this way: the fixture's own note ("Whether
   the ride stays ELIGIBLE is then decided by check 5") means check 5's
   outcome for that ONE scenario is deliberately left unencoded by the
   shared fixture (which the frontend also consumes, and the frontend has
   no GBFS data at all) -- not asserted to pass. Running the real check-5
   math confirms the surviving last waypoint sits ~258 m from
   `gbfs_end_lat/lon`, well outside the 150 m radius, so a correct
   implementation rejects it there. That is the "truncation buys a
   forger nothing" property the note is naming.

2. HAND-CONSTRUCTED SCENARIOS the shared fixture cannot encode because
   they need ride_row fields the frontend never sees (GBFS anchors) or
   because the fixture doesn't happen to cover this module's own named
   edge cases (the accuracy-clamp abuse case, the >10% sustained-fast
   pending_review flag, pending_feed, the feed_start_lat fallback, and
   the volume boundaries). These use `_seal_one_batch_per_chunk` /
   `_build_single_batch`, a small test-only chain builder deliberately
   NOT sharing any code with src/track_verify.py's own decoder -- so a
   bug shared between builder and verifier could not hide a test failure.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import geo, track_verify

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "track-chain-vectors.json"
_FIXTURE = json.loads(FIXTURE_PATH.read_text())

# golden-vector `pipeline_order` name -> this module's per_check key.
# Checks 1 (signature) and 2 (chain) share the single "chain" key -- see
# src/track_verify.py's module docstring and the golden vectors' own note
# on this ("The response's `verification` dict has no separate signature
# key, so the observable field is `chain`.").
_PIPELINE_KEY = {
    "signature": "chain", "chain": "chain", "monotonic": "monotonic",
    "speed": "speed", "gbfs_start": "gbfs_start", "gbfs_end": "gbfs_end",
    "volume": "volume",
}

_METERS_PER_DEG_LAT = 111_320.0  # matches src/geo.py's private constant;
# used only to CONSTRUCT synthetic fixtures with a predictable
# point-to-point distance. Every geometry this file builds moves due
# NORTH only (constant longitude), so src.geo.distance_meters reduces to
# exactly |delta_lat| * _METERS_PER_DEG_LAT (its east-west/cos(lat)
# component is zero) -- test_walk_with_distances_matches_geo_distance_meters
# below cross-checks this against the real production function rather
# than trusting it by construction alone.

_RIDE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_NONCE_HEX = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
_KEY_B64 = base64.urlsafe_b64encode(
    hashlib.sha256(b"track-verify-test-key").digest()).rstrip(b"=").decode("ascii")
_BASE_LAT = 39.7392
_BASE_LON = -104.9903


# ---------------------------------------------------------------------------
# Golden vectors
# ---------------------------------------------------------------------------

def _ride_row_from_fixture(ride: dict) -> track_verify.RideRow:
    def _dt(ms):
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms is not None else None

    return track_verify.RideRow(
        id=ride["ride_id"],
        track_key=ride["key_b64url"],
        track_nonce=ride["nonce"],
        track_key_issued_at=_dt(ride["started_at_ms"]),
        user_reported_ended_at=_dt(ride["ended_at_ms"]),
        start_lat=ride["feed_start_lat"], start_lon=ride["feed_start_lon"],
        feed_start_lat=ride["feed_start_lat"], feed_start_lon=ride["feed_start_lon"],
        gbfs_left_feed_at=_dt(ride.get("gbfs_left_feed_at_ms")),
        gbfs_reappeared_at=_dt(ride.get("gbfs_reappeared_at_ms")),
        gbfs_end_lat=ride.get("gbfs_end_lat"), gbfs_end_lon=ride.get("gbfs_end_lon"),
    )


def _assert_truncated_tail(result: track_verify.VerificationResult) -> None:
    """See the module docstring above for the full reasoning. Checks 1-4
    and 6 pass exactly as the fixture's note promises ("Chain-level
    checks pass by design"); check 5 (gbfs_end) genuinely fails against
    the 'primary' ride context's real gbfs_end_lat/lon (~258 m away,
    outside the 150 m radius) -- confirmed by hand computation before
    this assertion was written this way, not assumed."""
    assert result.per_check["chain"] == "ok"
    assert result.per_check["monotonic"] == "ok"
    assert result.per_check["speed"] == "ok"
    assert result.per_check["volume"] == "ok"
    assert result.per_check["gbfs_start"] == "ok"
    assert result.per_check["gbfs_end"] == "end_mismatch"
    assert result.verdict == "ineligible"
    assert result.reasons == ["end_mismatch"]


@pytest.mark.parametrize("scenario", _FIXTURE["scenarios"], ids=lambda s: s["name"])
def test_golden_vector(scenario):
    ride = _FIXTURE["rides"][scenario["ride"]]
    ride_row = _ride_row_from_fixture(ride)
    result = track_verify.verify_track_chain(None, ride_row, scenario["batches"])

    # Every scenario's per_check dict always carries exactly the six
    # documented keys, regardless of where the pipeline stopped.
    assert set(result.per_check.keys()) == set(track_verify.CHECK_KEYS)

    if scenario["name"] == "truncated-tail":
        _assert_truncated_tail(result)
        return

    exp = scenario["expected"]
    failing_check = exp["failing_check"]

    if failing_check is None:
        assert all(v == "ok" for v in result.per_check.values()), result.per_check
        assert result.reasons == []
        assert result.chain_root_hash == scenario["chain_root_hash"]
        assert result.verdict == "eligible"
        return

    key = _PIPELINE_KEY[failing_check]
    assert result.per_check[key] != "ok", (scenario["name"], result.per_check)
    # expected.reasons is a SET per the fixture's own conventions ("assert
    # reasons as a set") -- volume-too-few-waypoints legitimately produces
    # two simultaneously.
    assert set(result.reasons) == set(exp["reasons"]), (scenario["name"], result.reasons)

    if key == "chain":
        # A rejected chain exposes no root hash -- see module docstring
        # ("don't run later checks against garbage"). The fixture's own
        # chain_root_hash for these scenarios is what a full, un-short-
        # circuited recompute would yield (useful to the frontend, which
        # verifies differently); this verifier deliberately never computes
        # it once check 1/2 has failed.
        assert result.chain_root_hash is None
    else:
        assert result.chain_root_hash == scenario["chain_root_hash"]


def test_chain_format_constants_match_the_shared_contract():
    limits = _FIXTURE["limits"]
    assert track_verify.JWS_ALG == limits["jws_alg"]
    assert track_verify.JWS_TYP == limits["jws_typ"]
    assert track_verify.PAYLOAD_VERSION == limits["payload_version"]


def test_reason_vocabulary_matches_the_shared_contract():
    """The vocabulary this module draws `reasons` from is a SUBSET of the
    shared fixture's list (tracking_not_opted is never produced by this
    module -- that gate lives in the donation endpoint, before
    verify_track_chain is even called)."""
    shared = set(_FIXTURE["reason_vocabulary"])
    produced = {"start_mismatch", "end_mismatch", "too_few_waypoints",
                "trip_too_short", "chain_invalid", "internal_error"}
    assert produced <= shared


# ---------------------------------------------------------------------------
# Test-only chain builder (deliberately independent of src/track_verify.py)
# ---------------------------------------------------------------------------

def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _seal_one_batch_per_chunk(chunks, *, ride_id, nonce_hex, key_b64):
    """chunks: list[list[(abs_ms, lat, lon, acc)]] -- each inner list
    becomes exactly one batch (seq = its index). Returns
    (batches: list[str], chain_root_hash_hex: str). track_verify.py never
    itself checks batch-sealing size/span rules (25 pts / 60 s is a
    CLIENT-side sealing convention, not one of the six server checks), so
    tests are free to put any number of points in one batch."""
    key = _b64url_decode(key_b64)
    prev_hex = ""
    rolling = hashlib.sha256(bytes.fromhex(nonce_hex)).digest()  # H_-1
    batches = []
    for seq, chunk in enumerate(chunks):
        t0 = chunk[0][0]
        t1 = chunk[-1][0]
        pts = [[f[0] - t0, round(f[1], 6), round(f[2], 6),
                int(round(f[3])) if f[3] is not None else 0] for f in chunk]
        header = {"alg": "HS256", "typ": "sfyi-track+jws", "kid": ride_id}
        payload = {"v": 1, "rid": ride_id, "non": nonce_hex, "seq": seq,
                   "prev": prev_hex, "t0": t0, "t1": t1, "pts": pts, "rec": False}
        h_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
        p_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
        sig = hmac.new(key, f"{h_b64}.{p_b64}".encode("ascii"), hashlib.sha256).digest()
        jws = f"{h_b64}.{p_b64}.{_b64url_encode(sig)}"
        batches.append(jws)

        jws_hash = hashlib.sha256(jws.encode("ascii")).digest()
        rolling = hashlib.sha256(rolling + jws_hash).digest()
        prev_hex = jws_hash.hex()
    return batches, rolling.hex()


def _build_single_batch(*, ride_id, nonce_hex, key_b64, fixes,
                         header_overrides=None, payload_overrides=None):
    """A single seq-0 batch with full control over header/payload fields
    -- used to test the triple binding and malformed-payload handling."""
    key = _b64url_decode(key_b64)
    t0, t1 = fixes[0][0], fixes[-1][0]
    pts = [[f[0] - t0, round(f[1], 6), round(f[2], 6),
            int(round(f[3])) if f[3] is not None else 0] for f in fixes]
    header = {"alg": "HS256", "typ": "sfyi-track+jws", "kid": ride_id}
    header.update(header_overrides or {})
    payload = {"v": 1, "rid": ride_id, "non": nonce_hex, "seq": 0, "prev": "",
               "t0": t0, "t1": t1, "pts": pts, "rec": False}
    payload.update(payload_overrides or {})
    h_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    p_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(key, f"{h_b64}.{p_b64}".encode("ascii"), hashlib.sha256).digest()
    return f"{h_b64}.{p_b64}.{_b64url_encode(sig)}"


def _walk_with_distances(start_lat, start_lon, t0_ms, segment_specs, *, acc):
    """segment_specs: [(dt_ms, meters_north), ...]. Moves due NORTH only
    (constant longitude) -- see the _METERS_PER_DEG_LAT comment above for
    why that makes the resulting distance predictable."""
    lat, lon, t = start_lat, start_lon, t0_ms
    fixes = [(t, lat, lon, acc)]
    for dt_ms, meters in segment_specs:
        lat += meters / _METERS_PER_DEG_LAT
        t += dt_ms
        fixes.append((t, lat, lon, acc))
    return fixes


def _walk_with_speeds(start_lat, start_lon, t0_ms, segment_specs, *, acc):
    """segment_specs: [(dt_ms, speed_mps), ...] -- convenience over
    _walk_with_distances for speed-focused tests."""
    distance_specs = [(dt_ms, speed_mps * (dt_ms / 1000.0)) for dt_ms, speed_mps in segment_specs]
    return _walk_with_distances(start_lat, start_lon, t0_ms, distance_specs, acc=acc)


def test_walk_with_distances_matches_geo_distance_meters():
    """Sanity check on the test helpers themselves, against the real
    production distance function -- if src/geo.py's meters-per-degree
    assumption ever changes, this fails loudly instead of silently
    drifting every other test's margins."""
    fixes = _walk_with_distances(_BASE_LAT, _BASE_LON, 0, [(1000, 42.0)], acc=None)
    d = geo.distance_meters(fixes[0][1], fixes[0][2], fixes[1][1], fixes[1][2])
    assert d == pytest.approx(42.0, abs=1e-6)


def _good_ride_fixes():
    """12 waypoints, 220 s, ~660 m at a steady ~3 m/s -- comfortably
    clears every check-6 minimum and every check-4 speed threshold, so
    tests that don't care about volume/speed can build a ride_row off
    this and focus on the one check they're isolating."""
    segment_specs = [(20_000, 3.0)] * 11  # 12 points
    return _walk_with_speeds(_BASE_LAT, _BASE_LON, 0, segment_specs, acc=None)


def _ride_row_for(fixes, **overrides) -> track_verify.RideRow:
    """A RideRow whose feed/GBFS anchors line up exactly with `fixes`'
    first/last points and whose issued/ended bounds line up exactly with
    `fixes`' first/last timestamps -- i.e. a ride that, by default, passes
    every check. Individual tests override just the field(s) they want to
    push out of alignment."""
    first_ms, last_ms = fixes[0][0], fixes[-1][0]
    kwargs = dict(
        id=_RIDE_ID, track_key=_KEY_B64, track_nonce=_NONCE_HEX,
        track_key_issued_at=datetime.fromtimestamp(first_ms / 1000, tz=timezone.utc),
        user_reported_ended_at=datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc),
        start_lat=fixes[0][1], start_lon=fixes[0][2],
        feed_start_lat=fixes[0][1], feed_start_lon=fixes[0][2],
        gbfs_left_feed_at=None,
        gbfs_reappeared_at=datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc),
        gbfs_end_lat=fixes[-1][1], gbfs_end_lon=fixes[-1][2],
    )
    kwargs.update(overrides)
    return track_verify.RideRow(**kwargs)


def _verify(fixes, **row_overrides):
    """(result, chain_root_hash) for a single-batch chain over `fixes`,
    signed for a ride_row built by _ride_row_for(fixes, **row_overrides)."""
    batches, root_hash = _seal_one_batch_per_chunk(
        [fixes], ride_id=_RIDE_ID, nonce_hex=_NONCE_HEX, key_b64=_KEY_B64)
    ride_row = _ride_row_for(fixes, **row_overrides)
    result = track_verify.verify_track_chain(None, ride_row, batches)
    return result, root_hash


# ---------------------------------------------------------------------------
# Hand-constructed: accuracy-clamp abuse (named A1-review fix)
# ---------------------------------------------------------------------------

def test_accuracy_clamp_cannot_erase_a_teleport():
    """The anti-abuse fix: without the MAX_ACCURACY_ADJUSTMENT_M clamp, a
    rider could claim a huge accuracy value to subtract enough from a
    segment's distance to make any teleport look plausible. With the
    clamp (each point's contribution capped), a real teleport is still
    caught."""
    fixes = [
        (0, _BASE_LAT, _BASE_LON, 999_999),
        (2_000, _BASE_LAT + 0.05, _BASE_LON, 999_999),  # ~5566 m north in 2 s
    ]
    raw = geo.distance_meters(fixes[0][1], fixes[0][2], fixes[1][1], fixes[1][2])
    # Confirm this really IS a case an unclamped subtraction would erase
    # (i.e. the test is exercising the clamp, not something else).
    assert raw - 2 * fixes[0][3] < 0

    result, _ = _verify(fixes)
    assert result.per_check["speed"] == "implausible_speed"
    assert result.verdict == "ineligible"
    assert result.reasons == []


def test_accuracy_below_the_clamp_still_applies_in_full():
    """Companion: a MODEST claimed accuracy (well under the clamp) still
    legitimately reduces the adjusted speed -- the clamp bounds abuse, it
    doesn't zero out the adjustment entirely."""
    # ~25 m in 2 s = 12.5 m/s raw; with two 10 m accuracies subtracted,
    # adjusted = 5 m / 2 s = 2.5 m/s -- comfortably under the sustained
    # threshold too, isolating the clamp behavior itself.
    fixes = _walk_with_distances(_BASE_LAT, _BASE_LON, 0, [(2_000, 25.0)], acc=10.0)
    result, _ = _verify(fixes)
    assert result.per_check["speed"] == "ok"


# ---------------------------------------------------------------------------
# Hand-constructed: sustained-fast pending_review flag
# ---------------------------------------------------------------------------

def test_more_than_10pct_fast_segments_flags_pending_review_without_rejecting():
    segment_specs = (
        [(6_000, 3.0)] * 35          # 35 slow segments, ~3 m/s
        + [(6_000, 15.0)] * 5        # 5 segments over the 11 m/s sustained line
    )                                # (none over the 20 m/s hard-reject line)
    fast_fraction = 5 / len(segment_specs)
    assert fast_fraction > track_verify.SUSTAINED_FAST_SEGMENT_FRACTION

    fixes = _walk_with_speeds(_BASE_LAT, _BASE_LON, 0, segment_specs, acc=None)
    result, _ = _verify(fixes)

    assert result.per_check["speed"] == "ok", "flagged, not rejected"
    assert result.points_status == "pending_review"
    assert result.verdict == "eligible"


def test_10pct_or_fewer_fast_segments_does_not_flag_pending_review():
    segment_specs = (
        [(6_000, 3.0)] * 36
        + [(6_000, 15.0)] * 4        # exactly 10% (4/40) -- NOT "more than"
    )
    fast_fraction = 4 / len(segment_specs)
    assert fast_fraction == pytest.approx(track_verify.SUSTAINED_FAST_SEGMENT_FRACTION)

    fixes = _walk_with_speeds(_BASE_LAT, _BASE_LON, 0, segment_specs, acc=None)
    result, _ = _verify(fixes)

    assert result.per_check["speed"] == "ok"
    assert result.points_status == "ok"
    assert result.verdict == "eligible"


def test_speed_hard_reject_is_independent_of_the_pending_review_flag():
    """A single segment over the 20 m/s hard cap rejects outright, even
    though by count it is nowhere near the 10% sustained-fast fraction."""
    segment_specs = [(2_000, 3.0)] * 9 + [(1_000, 25.0)]
    fixes = _walk_with_speeds(_BASE_LAT, _BASE_LON, 0, segment_specs, acc=None)
    result, _ = _verify(fixes)
    assert result.per_check["speed"] == "implausible_speed"
    assert result.verdict == "ineligible"


@pytest.mark.parametrize("speed_mps,expect_ok", [
    (19.9, True),
    (20.1, False),
])
def test_speed_hard_reject_threshold(speed_mps, expect_ok):
    """A small margin either side of MAX_SEGMENT_SPEED_MPS, rather than
    bit-exact 20.0 -- the constructed distance round-trips through a
    lat-degree conversion before src.geo.distance_meters re-measures it,
    so chasing floating-point exactness at the literal boundary would be
    fragile; a +/-0.1 m/s margin still exercises the threshold
    meaningfully without depending on which way sub-double-precision noise
    happens to round."""
    fixes = _walk_with_speeds(_BASE_LAT, _BASE_LON, 0, [(1_000, speed_mps)], acc=None)
    result, _ = _verify(fixes)
    assert (result.per_check["speed"] == "ok") is expect_ok


# ---------------------------------------------------------------------------
# Hand-constructed: pending_feed
# ---------------------------------------------------------------------------

def test_pending_feed_when_gbfs_has_not_resolved():
    fixes = _good_ride_fixes()
    result, _ = _verify(fixes, gbfs_reappeared_at=None)
    assert result.per_check["gbfs_end"] == "pending_feed"
    assert result.verdict == "pending_feed"
    assert result.reasons == []


def test_pending_feed_is_not_a_stop_condition_volume_still_runs():
    """The golden vectors' own worked per_check example shows "volume":
    "ok" alongside "gbfs_end": "pending_feed" -- confirming check 6 still
    runs rather than being skipped."""
    fixes = _good_ride_fixes()
    result, _ = _verify(fixes, gbfs_reappeared_at=None)
    assert result.per_check["volume"] == "ok"
    assert result.per_check["gbfs_start"] == "ok"


def test_pending_feed_does_not_mask_a_genuine_start_mismatch():
    """An unresolved feed defers ONLY the question check 5b answers.
    Something check 5a can already answer -- the start doesn't match --
    still makes the ride ineligible outright rather than pending."""
    fixes = _good_ride_fixes()
    result, _ = _verify(
        fixes, gbfs_reappeared_at=None,
        feed_start_lat=fixes[0][1] + 1.0, feed_start_lon=fixes[0][2],
    )
    assert result.per_check["gbfs_start"] == "start_mismatch"
    assert result.per_check["gbfs_end"] == "pending_feed"
    assert result.verdict == "ineligible"
    assert result.reasons == ["start_mismatch"]


# ---------------------------------------------------------------------------
# Hand-constructed: feed_start_lat/lon NULL fallback to start_lat/lon
# ---------------------------------------------------------------------------

def test_feed_start_fallback_to_client_supplied_start_when_feed_columns_are_null():
    fixes = _good_ride_fixes()
    result = _verify(
        fixes,
        feed_start_lat=None, feed_start_lon=None,
        start_lat=fixes[0][1], start_lon=fixes[0][2],
    )[0]
    assert result.per_check["gbfs_start"] == "ok"


def test_feed_start_fallback_is_actually_consulted_not_coincidental():
    """Companion: move the CLIENT-supplied start far away (feed_start_*
    still NULL) and confirm gbfs_start now fails -- proving the previous
    pass really came from start_lat/lon, not some other default."""
    fixes = _good_ride_fixes()
    result = _verify(
        fixes,
        feed_start_lat=None, feed_start_lon=None,
        start_lat=fixes[0][1] + 1.0, start_lon=fixes[0][2],
    )[0]
    assert result.per_check["gbfs_start"] == "start_mismatch"


def test_feed_start_takes_priority_over_start_when_both_present():
    """When feed_start_* IS present, it wins even if start_lat/lon would
    also have passed -- the feed anchor is the stronger check and must be
    preferred, not merely tried first among equals."""
    fixes = _good_ride_fixes()
    result = _verify(
        fixes,
        feed_start_lat=fixes[0][1] + 1.0, feed_start_lon=fixes[0][2],  # bad feed anchor
        start_lat=fixes[0][1], start_lon=fixes[0][2],                  # good client start
    )[0]
    assert result.per_check["gbfs_start"] == "start_mismatch"


# ---------------------------------------------------------------------------
# Hand-constructed: GBFS start/end mismatch (explicit, beyond the fallback
# and pending_feed cases above)
# ---------------------------------------------------------------------------

def test_gbfs_end_mismatch_when_last_waypoint_is_far_from_the_anchor():
    fixes = _good_ride_fixes()
    result, _ = _verify(fixes, gbfs_end_lat=fixes[-1][1] + 1.0, gbfs_end_lon=fixes[-1][2])
    assert result.per_check["gbfs_end"] == "end_mismatch"
    assert "end_mismatch" in result.reasons
    assert result.verdict == "ineligible"


def test_gbfs_end_mismatch_on_timestamp_even_when_position_matches():
    fixes = _good_ride_fixes()
    last_ms = fixes[-1][0]
    far_reappeared = datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc) + timedelta(minutes=20)
    result, _ = _verify(fixes, gbfs_reappeared_at=far_reappeared)
    assert result.per_check["gbfs_end"] == "end_mismatch"


def test_gbfs_start_timing_mismatch_against_gbfs_left_feed_at():
    fixes = _good_ride_fixes()
    issued_at = datetime.fromtimestamp(fixes[0][0] / 1000, tz=timezone.utc)
    far_left_feed_at = issued_at + timedelta(minutes=20)
    result, _ = _verify(fixes, gbfs_left_feed_at=far_left_feed_at)
    assert result.per_check["gbfs_start"] == "start_mismatch"


def test_gbfs_start_timing_is_skipped_when_gbfs_left_feed_at_is_unset():
    """gbfs_left_feed_at is None on plenty of legitimate rides (still
    'watching', never left the feed) -- absence must not itself be a
    failure."""
    fixes = _good_ride_fixes()
    result, _ = _verify(fixes, gbfs_left_feed_at=None)
    assert result.per_check["gbfs_start"] == "ok"


@pytest.mark.parametrize("radius_offset_m,expect_ok", [(-5.0, True), (5.0, False)])
def test_gbfs_correlation_radius_boundary(radius_offset_m, expect_ok):
    """A small margin either side of GBFS_CORRELATION_RADIUS_M, same
    floating-point-margin rationale as the speed threshold test."""
    fixes = _good_ride_fixes()
    target_m = track_verify.GBFS_CORRELATION_RADIUS_M + radius_offset_m
    moved_lat = fixes[-1][1] + target_m / _METERS_PER_DEG_LAT
    result, _ = _verify(fixes, gbfs_end_lat=moved_lat, gbfs_end_lon=fixes[-1][2])
    assert (result.per_check["gbfs_end"] == "ok") is expect_ok


# ---------------------------------------------------------------------------
# Hand-constructed: monotonicity + bounds
# ---------------------------------------------------------------------------

def test_monotonic_rejects_a_non_increasing_flattened_track():
    fixes = _good_ride_fixes()
    # Duplicate a timestamp -- two consecutive points at the same instant.
    bad = fixes[:5] + [(fixes[4][0], fixes[5][1], fixes[5][2], fixes[5][3])] + fixes[6:]
    result, _ = _verify(bad)
    assert result.per_check["monotonic"] == "invalid_timing"
    assert result.reasons == []


def test_monotonic_bounds_reject_a_track_that_predates_key_issuance():
    fixes = _good_ride_fixes()
    first_dt = datetime.fromtimestamp(fixes[0][0] / 1000, tz=timezone.utc)
    result, _ = _verify(fixes, track_key_issued_at=first_dt + timedelta(minutes=5))
    assert result.per_check["monotonic"] == "invalid_timing"
    assert result.verdict == "ineligible"
    assert result.reasons == []


def test_monotonic_bounds_reject_a_track_that_outlives_the_reported_end():
    fixes = _good_ride_fixes()
    last_dt = datetime.fromtimestamp(fixes[-1][0] / 1000, tz=timezone.utc)
    result, _ = _verify(fixes, user_reported_ended_at=last_dt - timedelta(minutes=5))
    assert result.per_check["monotonic"] == "invalid_timing"


def test_monotonic_bounds_slack_boundary_is_inclusive_at_the_start():
    """Integer-millisecond arithmetic throughout (epoch-ms floats at this
    magnitude carry far more than millisecond precision), so this boundary
    IS asserted bit-exactly, unlike the geometry-based ones above."""
    fixes = _good_ride_fixes()
    first_dt = datetime.fromtimestamp(fixes[0][0] / 1000, tz=timezone.utc)

    at_the_edge = first_dt + timedelta(milliseconds=track_verify.BOUNDS_SLACK_MS)
    assert _verify(fixes, track_key_issued_at=at_the_edge)[0].per_check["monotonic"] == "ok"

    one_ms_over = first_dt + timedelta(milliseconds=track_verify.BOUNDS_SLACK_MS + 1)
    assert _verify(fixes, track_key_issued_at=one_ms_over)[0].per_check["monotonic"] == "invalid_timing"


def test_monotonic_bounds_slack_boundary_is_inclusive_at_the_end():
    fixes = _good_ride_fixes()
    last_dt = datetime.fromtimestamp(fixes[-1][0] / 1000, tz=timezone.utc)

    at_the_edge = last_dt - timedelta(milliseconds=track_verify.BOUNDS_SLACK_MS)
    assert _verify(fixes, user_reported_ended_at=at_the_edge)[0].per_check["monotonic"] == "ok"

    one_ms_over = last_dt - timedelta(milliseconds=track_verify.BOUNDS_SLACK_MS + 1)
    assert _verify(fixes, user_reported_ended_at=one_ms_over)[0].per_check["monotonic"] == "invalid_timing"


# ---------------------------------------------------------------------------
# Hand-constructed: volume boundaries
# ---------------------------------------------------------------------------

def _boundary_ride_fixes(*, waypoints, total_distance_m, total_duration_ms):
    """`waypoints` evenly-spaced fixes whose CUMULATIVE duration is
    EXACT (plain integer-ms arithmetic, any remainder absorbed by the
    final segment) and whose cumulative distance is very close to
    `total_distance_m` (subject to the same float round-trip as every
    other geometry helper here -- callers wanting a real PASS/FAIL boundary
    on the distance dimension should offset `total_distance_m` a few
    metres either side of MIN_DISTANCE_METERS rather than passing it
    unmodified; see the volume boundary tests below)."""
    segments = waypoints - 1
    dt_ms = total_duration_ms // segments
    remainder_ms = total_duration_ms - dt_ms * segments
    meters = total_distance_m / segments
    specs = [(dt_ms, meters)] * (segments - 1)
    specs.append((dt_ms + remainder_ms, total_distance_m - meters * (segments - 1)))
    return _walk_with_distances(_BASE_LAT, _BASE_LON, 0, specs, acc=None)


def test_boundary_ride_fixes_hits_the_requested_waypoint_count_and_duration_exactly():
    fixes = _boundary_ride_fixes(waypoints=10, total_distance_m=505.0, total_duration_ms=180_000)
    assert len(fixes) == 10
    assert fixes[-1][0] - fixes[0][0] == 180_000


def test_volume_boundary_at_every_minimum_passes():
    fixes = _boundary_ride_fixes(
        waypoints=track_verify.MIN_WAYPOINTS,
        total_distance_m=track_verify.MIN_DISTANCE_METERS + 5.0,  # see _boundary_ride_fixes docstring
        total_duration_ms=track_verify.MIN_DURATION_MS,
    )
    result, _ = _verify(fixes)
    assert result.waypoint_count == track_verify.MIN_WAYPOINTS
    assert result.distance_meters >= track_verify.MIN_DISTANCE_METERS
    assert result.per_check["volume"] == "ok"
    assert result.verdict == "eligible"


def test_volume_boundary_one_waypoint_under_fails_only_on_waypoint_count():
    fixes = _boundary_ride_fixes(
        waypoints=track_verify.MIN_WAYPOINTS - 1,
        total_distance_m=track_verify.MIN_DISTANCE_METERS + 5.0,
        total_duration_ms=track_verify.MIN_DURATION_MS,
    )
    result, _ = _verify(fixes)
    assert result.reasons == ["too_few_waypoints"]
    assert result.verdict == "ineligible"


def test_volume_boundary_distance_just_under_fails_only_on_trip_too_short():
    fixes = _boundary_ride_fixes(
        waypoints=track_verify.MIN_WAYPOINTS,
        total_distance_m=track_verify.MIN_DISTANCE_METERS - 5.0,
        total_duration_ms=track_verify.MIN_DURATION_MS,
    )
    result, _ = _verify(fixes)
    assert result.reasons == ["trip_too_short"]
    assert result.verdict == "ineligible"


def test_volume_boundary_duration_one_ms_under_fails_only_on_trip_too_short():
    """Duration IS integer-ms exact via _boundary_ride_fixes, so this one
    is asserted at the literal 1 ms boundary."""
    fixes = _boundary_ride_fixes(
        waypoints=track_verify.MIN_WAYPOINTS,
        total_distance_m=track_verify.MIN_DISTANCE_METERS + 5.0,
        total_duration_ms=track_verify.MIN_DURATION_MS - 1,
    )
    result, _ = _verify(fixes)
    assert result.reasons == ["trip_too_short"]
    assert result.verdict == "ineligible"


def test_volume_boundary_duration_exactly_at_minimum_passes():
    fixes = _boundary_ride_fixes(
        waypoints=track_verify.MIN_WAYPOINTS,
        total_distance_m=track_verify.MIN_DISTANCE_METERS + 5.0,
        total_duration_ms=track_verify.MIN_DURATION_MS,
    )
    result, _ = _verify(fixes)
    assert "trip_too_short" not in result.reasons


def test_volume_failure_reports_both_reasons_when_both_conditions_hold():
    """Not an exclusive pick -- see _verify_volume's docstring and the
    volume-too-few-waypoints golden vector, which asserts the same thing:
    a ride that is both too sparse AND too short/short-lived reports
    BOTH tokens."""
    fixes = _boundary_ride_fixes(
        waypoints=3, total_distance_m=10.0, total_duration_ms=5_000,
    )
    result, _ = _verify(fixes)
    assert set(result.reasons) == {"too_few_waypoints", "trip_too_short"}


# ---------------------------------------------------------------------------
# Hand-constructed: the triple binding (check 1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("header_overrides,payload_overrides,label", [
    ({"kid": "some-other-ride-id"}, None, "kid"),
    (None, {"rid": "some-other-ride-id"}, "rid"),
    (None, {"non": "ff" * 16}, "non"),
])
def test_triple_binding_rejects_any_mismatched_component(header_overrides, payload_overrides, label):
    fixes = _good_ride_fixes()
    jws = _build_single_batch(
        ride_id=_RIDE_ID, nonce_hex=_NONCE_HEX, key_b64=_KEY_B64, fixes=fixes,
        header_overrides=header_overrides, payload_overrides=payload_overrides,
    )
    ride_row = _ride_row_for(fixes)
    result = track_verify.verify_track_chain(None, ride_row, [jws])
    assert result.per_check["chain"] == "chain_invalid", label
    assert result.verdict == "ineligible"
    assert result.reasons == ["chain_invalid"]
    assert result.chain_root_hash is None


def test_bad_alg_in_header_is_rejected():
    fixes = _good_ride_fixes()
    jws = _build_single_batch(
        ride_id=_RIDE_ID, nonce_hex=_NONCE_HEX, key_b64=_KEY_B64, fixes=fixes,
        header_overrides={"alg": "none"},
    )
    result = track_verify.verify_track_chain(None, _ride_row_for(fixes), [jws])
    assert result.per_check["chain"] == "chain_invalid"


def test_bad_typ_in_header_is_rejected():
    fixes = _good_ride_fixes()
    jws = _build_single_batch(
        ride_id=_RIDE_ID, nonce_hex=_NONCE_HEX, key_b64=_KEY_B64, fixes=fixes,
        header_overrides={"typ": "JWT"},
    )
    result = track_verify.verify_track_chain(None, _ride_row_for(fixes), [jws])
    assert result.per_check["chain"] == "chain_invalid"


def test_malformed_payload_shape_is_rejected_not_crashed():
    fixes = _good_ride_fixes()
    jws = _build_single_batch(
        ride_id=_RIDE_ID, nonce_hex=_NONCE_HEX, key_b64=_KEY_B64, fixes=fixes,
        payload_overrides={"pts": "not-a-list"},
    )
    result = track_verify.verify_track_chain(None, _ride_row_for(fixes), [jws])
    assert result.per_check["chain"] == "chain_invalid"


def test_a_ride_with_no_signing_material_rejects_every_donation():
    """A ride that predates sql/049 (or a private/guest ride with no
    server key) has nothing to verify a donated chain against."""
    fixes = _good_ride_fixes()
    batches, _ = _seal_one_batch_per_chunk([fixes], ride_id=_RIDE_ID,
                                            nonce_hex=_NONCE_HEX, key_b64=_KEY_B64)
    ride_row = _ride_row_for(fixes, track_key=None, track_nonce=None)
    result = track_verify.verify_track_chain(None, ride_row, batches)
    assert result.per_check["chain"] == "chain_invalid"


def test_failing_batch_seq_identifies_the_bad_batch():
    fixes = _good_ride_fixes()
    assert len(fixes) == 12
    chunks = [fixes[0:4], fixes[4:8], fixes[8:12]]
    batches, _ = _seal_one_batch_per_chunk(
        chunks, ride_id=_RIDE_ID, nonce_hex=_NONCE_HEX, key_b64=_KEY_B64)

    header_b64, payload_b64, sig_b64 = batches[1].split(".")
    ch = sig_b64[5]
    tampered_sig = sig_b64[:5] + ("A" if ch != "A" else "B") + sig_b64[6:]
    batches[1] = f"{header_b64}.{payload_b64}.{tampered_sig}"

    result = track_verify.verify_track_chain(None, _ride_row_for(fixes), batches)
    assert result.per_check["chain"] == "chain_invalid"
    assert result.failing_batch_seq == 1


def test_seq_gap_is_rejected():
    """seq must be contiguous from 0 IN THE ORDER GIVEN -- a gap (0, 2)
    fails even though nothing was reordered."""
    fixes = _good_ride_fixes()
    chunks = [fixes[0:6], fixes[6:12]]
    batches, _ = _seal_one_batch_per_chunk(
        chunks, ride_id=_RIDE_ID, nonce_hex=_NONCE_HEX, key_b64=_KEY_B64)
    # Rebuild batch 1 with seq=2 instead of 1 (re-signed so check 1 still
    # passes and only check 2's contiguity test is exercised).
    jws = _build_single_batch(
        ride_id=_RIDE_ID, nonce_hex=_NONCE_HEX, key_b64=_KEY_B64, fixes=fixes[6:12],
        payload_overrides={"seq": 2, "prev": hashlib.sha256(batches[0].encode()).hexdigest()},
    )
    batches[1] = jws
    result = track_verify.verify_track_chain(None, _ride_row_for(fixes), batches)
    assert result.per_check["chain"] == "chain_invalid"


def test_prev_hash_mismatch_is_rejected():
    fixes = _good_ride_fixes()
    chunks = [fixes[0:6], fixes[6:12]]
    batches, _ = _seal_one_batch_per_chunk(
        chunks, ride_id=_RIDE_ID, nonce_hex=_NONCE_HEX, key_b64=_KEY_B64)
    jws = _build_single_batch(
        ride_id=_RIDE_ID, nonce_hex=_NONCE_HEX, key_b64=_KEY_B64, fixes=fixes[6:12],
        payload_overrides={"seq": 1, "prev": "00" * 32},
    )
    batches[1] = jws
    result = track_verify.verify_track_chain(None, _ride_row_for(fixes), batches)
    assert result.per_check["chain"] == "chain_invalid"


# ---------------------------------------------------------------------------
# Hand-constructed: purity, error handling, response shape
# ---------------------------------------------------------------------------

class _ExplodingCursor:
    """A cursor stand-in that fails the test the instant anything touches
    it -- the strongest available proof that verify_track_chain really is
    pure with respect to `cur`."""

    def __getattr__(self, name):
        raise AssertionError(f"track_verify touched cur.{name} -- it must stay a pure function")


def test_the_cursor_argument_is_never_touched():
    fixes = _good_ride_fixes()
    batches, _ = _seal_one_batch_per_chunk(
        [fixes], ride_id=_RIDE_ID, nonce_hex=_NONCE_HEX, key_b64=_KEY_B64)
    result = track_verify.verify_track_chain(_ExplodingCursor(), _ride_row_for(fixes), batches)
    assert result.verdict == "eligible"


def test_internal_error_is_caught_and_reported_not_raised():
    """A ride_row that violates its own documented contract (a required
    datetime is None) must not crash the donation endpoint -- it must
    come back as a reportable result."""
    fixes = _good_ride_fixes()
    batches, _ = _seal_one_batch_per_chunk(
        [fixes], ride_id=_RIDE_ID, nonce_hex=_NONCE_HEX, key_b64=_KEY_B64)
    ride_row = _ride_row_for(fixes, track_key_issued_at=None)
    result = track_verify.verify_track_chain(None, ride_row, batches)
    assert result.verdict == "error"
    assert result.reasons == ["internal_error"]
    assert result.chain_root_hash is None
    assert result.waypoint_count == 0
    assert all(v == track_verify.SKIPPED for v in result.per_check.values())


def test_internal_error_survives_a_ride_row_of_the_wrong_type_entirely():
    """Stronger than test_internal_error_is_caught_and_reported_not_raised:
    here `ride_row` isn't even a RideRow (no `.id` at all), so the
    exception handler's own logging call is exercised too -- it must not
    be a second way for this function to raise."""
    result = track_verify.verify_track_chain(None, object(), ["not-a-jws"])
    assert result.verdict == "error"
    assert result.reasons == ["internal_error"]


def test_empty_batches_list_is_handled_gracefully_not_crashed():
    fixes = _good_ride_fixes()
    ride_row = _ride_row_for(fixes)
    result = track_verify.verify_track_chain(None, ride_row, [])
    assert result.verdict == "ineligible"
    assert result.per_check["chain"] == "ok"
    assert result.per_check["monotonic"] != "ok"
    assert result.waypoint_count == 0
    assert result.chain_root_hash is not None  # H_-1 alone, the nonce-only root


def test_as_response_shape():
    fixes = _good_ride_fixes()
    result, _ = _verify(fixes)
    response = result.as_response()
    assert set(response.keys()) == {"verification", "validation"}
    assert response["verification"] == result.per_check
    assert response["validation"] == {"status": result.verdict, "reasons": result.reasons}


def test_per_check_keys_are_stable_across_every_outcome():
    fixes = _good_ride_fixes()
    outcomes = [
        _verify(fixes)[0],
        _verify(fixes, gbfs_reappeared_at=None)[0],
        _verify(fixes, track_key_issued_at=None)[0],  # -> "error"
    ]
    for result in outcomes:
        assert set(result.per_check.keys()) == set(track_verify.CHECK_KEYS)


def test_distance_meters_is_computed_over_raw_unadjusted_points():
    """PLAN_RIDE_MODE_API.md check 4: the accuracy adjustment is only for
    the speed GATE, never for the reported distance -- a ride with large
    (but individually plausible) accuracy values must still report the
    RAW measured distance, not one shrunk by the accuracy subtraction."""
    fixes = _walk_with_distances(
        _BASE_LAT, _BASE_LON, 0,
        [(60_000, 200.0)] * 3,  # 3 segments, 200 m/60 s = 3.33 m/s each
        acc=45.0,  # under the clamp, so it WOULD shrink an adjusted metric
    )
    result, _ = _verify(fixes)
    assert result.distance_meters == pytest.approx(600.0, abs=0.5)
