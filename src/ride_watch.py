"""Rider-declared ride watch: detect a watched scooter leaving/rejoining
the GBFS feed (item 5). Called once per cycle from src/cycle.py:run_once(),
immediately after device_state.update_for_cycle — same isolation contract
(a failure here must never fail the cycle; the caller wraps this in
try/except).

Unlike device_state (every device in the feed), this only touches
user_device_watch_list rows with status IN ('watching','left_feed') and an
unexpired watch_expires_at — one row per in-progress rider-declared ride,
expected to be tiny relative to the fleet. That's the "targeted indexed
query, not a full table scan" the performance requirement asks for.

Two transitions only:
  watching  -> left_feed  vehicle_identifier CHECKED OUT this cycle —
                           see _is_checked_out below.
  left_feed -> resolved   vehicle_identifier AVAILABLE again. Records the
                           observed lat/lon/battery on tracked_rides as
                           the GBFS-side end signal, independent of any
                           user report (sql/027_tracked_rides.sql).

WHAT "CHECKED OUT" ACTUALLY LOOKS LIKE. This module shipped reading it as
"absent from this cycle's device list", on the assumption every operator
drops a rented vehicle from free_bike_status. **Veo does not.** Measured
against production telemetry on 2026-08-10: a rented Veo vehicle stays in
the feed for the whole rental, at 2-minute granularity, broadcasting its
live moving position, with `is_reserved` flipping true for the duration.
Over a 100-minute window, consecutive samples of `is_reserved` vehicles
moved 320 m on average (68% of steps > 50 m — scooter pace), against 1.2 m
for the rest of the fleet (0.2% of steps > 50 m — GPS jitter). The whole
arc is visible on one vehicle: parked, `is_reserved` true at 08:34,
~1 km of travel, `is_reserved` false again at 08:58 at the new kerb.

Presence alone therefore never fired: 19 of 19 tracked rides had
`gbfs_left_feed_at` NULL, all 17 donations settled `gbfs_end:
pending_feed`, and every one aged out to `ineligible`/`end_mismatch` for
0 points. Both signals are honoured now — absence still counts (operators
that DO drop rented vehicles, and genuine feed dropouts), and so does the
reservation flag.

`is_reserved` is None when upstream omits it or sends a non-bool
(src/ingest.py already normalises that), and None reads as available —
i.e. exactly the pre-2026-08-10 presence-only behaviour, so a feed that
stops publishing the flag degrades to the old model rather than pinning
every watch open.

"Removed from the feed at its present location" is read as "absent from
this cycle's device list, or present but flagged checked out".
Reappearance is recorded unconditionally (no "must differ from start
location" gate) — anti-abuse filtering on that data belongs to the points
system, not this detection layer.

A watch that expires without ever resolving is left alone here —
src/cli.py:expire_stale_watches() closes those out on its own cadence.

VALIDATION FINISHER (PLAN_RIDE_MODE_API.md phase A2, "Validation finisher"):
finalize_validation() below settles a ride's contribution eligibility once
the thing a `pending_feed` ride was waiting on resolves — either GBFS
reappearing (this module's own resolve path, right below) or the watch
window elapsing without it ever reappearing (src/cli.py:
expire_stale_watches). See finalize_validation's own docstring for the
full contract; see the ADVISORY-LOCK ORDERING note just above
update_watches_for_cycle's reappeared-branch for why this module's own
resolve path had to change, not just gain a new function.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .api_tracked_rides import _provisional_validation
from .battery_model import ingest_donated_observation
from .geo import distance_meters
from .ingest import TaggedDevice
from .pg import connection
from .points import credit_battery_contribution, credit_nav_distance_bonus
from .quality import compute_battery_percent

log = logging.getLogger(__name__)


@dataclass
class WatchUpdateStats:
    open_watches: int = 0
    newly_left_feed: int = 0
    newly_reappeared: int = 0
    # A2's validation finisher: how many of this cycle's newly_reappeared
    # rides had a pending_feed donation/status finalize_validation actually
    # settled (eligible or ineligible) or refreshed. 0 is the overwhelming
    # common case — most reappearances have no donation waiting on them at
    # all.
    finalized_validations: int = 0


def _is_checked_out(device: TaggedDevice | None) -> bool:
    """True when this cycle says the vehicle is in someone's hands — the
    module docstring's "WHAT CHECKED OUT ACTUALLY LOOKS LIKE" measurement.

    Two operator conventions, both accepted, because we can't require the
    upstream feed to pick one:
      * the vehicle drops out of free_bike_status entirely (the GBFS-spec
        reading, and what this module assumed exclusively until
        2026-08-10), and
      * the vehicle stays listed with `is_reserved` true (what Veo
        actually does — the only signal available on that feed).

    `is_disabled` is deliberately NOT read as checked out: it marks a
    vehicle taken out of service, not one being ridden, and the same
    measurement window shows disabled-but-unreserved vehicles sitting
    still (1.1 m per step). A disabled vehicle mid-rental is already
    covered by `is_reserved` being true alongside it.
    """
    return device is None or device.is_reserved is True


def _classify(
    watch_rows: list[tuple[int, uuid.UUID, str, str]],  # (watch_id, tracked_ride_id, vehicle_identifier, status)
    observed: dict[str, TaggedDevice],
) -> tuple[list[tuple[int, uuid.UUID]], list[tuple[int, uuid.UUID, TaggedDevice]]]:
    """Pure partitioning, no DB/IO — unit-testable without a fake cursor."""
    newly_left: list[tuple[int, uuid.UUID]] = []
    newly_reappeared: list[tuple[int, uuid.UUID, TaggedDevice]] = []
    for watch_id, tracked_ride_id, vehicle_identifier, status in watch_rows:
        device = observed.get(vehicle_identifier)
        checked_out = _is_checked_out(device)
        if status == "watching" and checked_out:
            newly_left.append((watch_id, tracked_ride_id))
        elif status == "left_feed" and not checked_out:
            # `device` is necessarily non-None here: _is_checked_out is
            # True for every absent vehicle, so "not checked out" implies
            # "observed this cycle" — and the resolve branch below needs
            # the observation to stamp gbfs_end_lat/lon/battery from.
            assert device is not None
            newly_reappeared.append((watch_id, tracked_ride_id, device))
    return newly_left, newly_reappeared


def update_watches_for_cycle(
    cycle_id: uuid.UUID,
    snapshot_time: datetime,
    devices: Iterable[TaggedDevice],
) -> WatchUpdateStats:
    observed = {d.vehicle_identifier: d for d in devices if d.vehicle_identifier}
    stats = WatchUpdateStats()

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, tracked_ride_id, vehicle_identifier, status
                FROM user_device_watch_list
                WHERE status IN ('watching', 'left_feed')
                  AND watch_expires_at > %s
                FOR UPDATE
                """,
                (snapshot_time,),
            )
            watch_rows = cur.fetchall()
            stats.open_watches = len(watch_rows)
            if not watch_rows:
                return stats

            newly_left, newly_reappeared = _classify(watch_rows, observed)
            stats.newly_left_feed = len(newly_left)
            stats.newly_reappeared = len(newly_reappeared)

            if newly_left:
                left_watch_ids = [w for w, _ in newly_left]
                left_ride_ids = [str(r) for _, r in newly_left]
                cur.execute(
                    "UPDATE user_device_watch_list SET status = 'left_feed', "
                    "last_checked_cycle_id = %s WHERE id = ANY(%s)",
                    (str(cycle_id), left_watch_ids),
                )
                cur.execute(
                    """
                    UPDATE tracked_rides SET
                        status = 'left_feed',
                        gbfs_left_feed_at = %s,
                        gbfs_left_feed_cycle_id = %s,
                        updated_at = NOW()
                    WHERE id = ANY(%s)
                    """,
                    (snapshot_time, str(cycle_id), left_ride_ids),
                )

            if newly_reappeared:
                watch_ids = [w for w, _, _ in newly_reappeared]
                cur.execute(
                    "UPDATE user_device_watch_list SET status = 'resolved', "
                    "last_checked_cycle_id = %s WHERE id = ANY(%s)",
                    (str(cycle_id), watch_ids),
                )
                # ADVISORY-LOCK ORDERING (the "ride_watch advisory-lock fix",
                # PLAN_RIDE_MODE_API.md phase A2 "Validation finisher"): take
                # every reappearing ride's ride_validation:<ride_id> lock
                # BEFORE the tracked_rides UPDATE just below touches its row
                # — not after, and not only inside finalize_validation
                # (called later, once this transaction has committed — see
                # the loop after this `with` block). A2's donation
                # transaction is itself lock-then-write
                # (src/api_tracked_rides.py's start handler is the
                # `pg_advisory_xact_lock(hashtextextended(key, 0))` idiom);
                # writing this row FIRST and locking second would deadlock
                # against a donation mid-flight on the same ride — this
                # transaction would hold the row's write lock while waiting
                # on the advisory lock the donation holds, and the donation
                # would be waiting on this row's lock in turn. Postgres
                # detects and aborts one side (this cycle simply retries
                # next pass), but shipping that inversion turns an
                # occasional donation into a guaranteed abort. Lock first,
                # always. (Re-acquiring the same lock inside
                # finalize_validation later, in a NEW transaction, is
                # unaffected either way — by then this one has committed and
                # released it.)
                for _, tracked_ride_id, _ in newly_reappeared:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"ride_validation:{tracked_ride_id}",),
                    )
                cur.executemany(
                    """
                    UPDATE tracked_rides SET
                        gbfs_reappeared_at = %s,
                        gbfs_reappeared_cycle_id = %s,
                        gbfs_end_lat = %s,
                        gbfs_end_lon = %s,
                        gbfs_end_battery_percent = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    [
                        (snapshot_time, str(cycle_id), d.lat, d.lon,
                         compute_battery_percent(d.current_range_meters), str(tracked_ride_id))
                        for _, tracked_ride_id, d in newly_reappeared
                    ],
                )

            changed = {w for w, _ in newly_left} | {w for w, _, _ in newly_reappeared}
            unchanged = [w for w, _, _, _ in watch_rows if w not in changed]
            if unchanged:
                cur.execute(
                    "UPDATE user_device_watch_list SET last_checked_cycle_id = %s WHERE id = ANY(%s)",
                    (str(cycle_id), unchanged),
                )
        conn.commit()

        # Validation finisher (PLAN_RIDE_MODE_API.md phase A2). Deliberately
        # a SEPARATE transaction per ride, run only after the block above
        # has committed and thereby released the advisory locks it took: by
        # the time finalize_validation re-takes `ride_validation:<ride_id>`
        # here, there is no held row-lock left over from this cycle's own
        # UPDATE for it to deadlock against, and gbfs_reappeared_at is
        # already durably visible for finalize_validation's own read. One
        # ride's failure (a bug, an unexpected DB error) is rolled back and
        # logged without discarding this cycle's already-committed
        # left/reappeared/unchanged watch-list work, and without preventing
        # the NEXT reappeared ride in the same cycle from being finalized —
        # same "a failure here must never fail the cycle" contract this
        # module's own docstring states for update_watches_for_cycle as a
        # whole, applied one level finer.
        for _, tracked_ride_id, _ in newly_reappeared:
            try:
                with conn.cursor() as fcur:
                    result = finalize_validation(fcur, str(tracked_ride_id))
                conn.commit()
            except Exception:
                conn.rollback()
                log.exception(
                    "ride_watch cycle=%s: finalize_validation failed for ride %s",
                    cycle_id, tracked_ride_id,
                )
                continue
            if result is not None:
                stats.finalized_validations += 1

    log.info(
        "ride_watch cycle=%s: open=%d left_feed=%d reappeared=%d finalized=%d",
        cycle_id, stats.open_watches, stats.newly_left_feed, stats.newly_reappeared,
        stats.finalized_validations,
    )
    return stats


# ---------------------------------------------------------------------------
# Validation finisher (PLAN_RIDE_MODE_API.md phase A2, "Validation
# finisher")
# ---------------------------------------------------------------------------
#
# Duplicated from src/track_verify.py's check 5 (_verify_gbfs_end), not
# imported: that module's checks need the RAW batch strings to recompute
# from scratch (signature, chain integrity, monotonicity, speed), and those
# strings are discarded right after verification
# (RIDE_MODE_OVERHAUL_PLAN.md Part 2: "Raw JWS strings are discarded after
# verification"), so by the time a pending_feed donation reaches this
# finisher there is nothing left to feed verify_track_chain(). Per that
# module's own pipeline, a donation only ever settles at 'pending_feed'
# when every OTHER check (chain, monotonic, speed, gbfs START, volume)
# already came back clean — gbfs_end reading 'pending_feed' is the ONE
# thing that doesn't stop the pipeline there — so re-deriving this one
# check against the LAST stored `donated_track_points` row is sufficient to
# turn 'pending_feed' into a final verdict. Same constants, same decision.
_GBFS_CORRELATION_RADIUS_M = 150.0
_GBFS_TIME_WINDOW_MS = 10 * 60 * 1000  # +/- 10 minutes


def _gbfs_end_matches(
    last_point: tuple[int, float, float] | None,  # (recorded_ms, lat, lon)
    *,
    gbfs_reappeared_at: datetime | None,
    gbfs_end_lat: float | None,
    gbfs_end_lon: float | None,
) -> bool:
    """True when the donation's last waypoint corroborates the GBFS
    reappearance — src/track_verify.py's check 5 ("gbfs_end"), re-run over
    stored data instead of a fresh chain. False (== 'end_mismatch') for an
    unresolved feed too: by the time this runs, "still pending" is no
    longer an available answer — the caller is settling because either the
    feed just resolved or the watch window ran out waiting for it, and per
    the A2 spec the finisher must settle to "award or end_mismatch", not
    leave the ride in limbo a second time.
    """
    if gbfs_reappeared_at is None or last_point is None:
        return False
    if gbfs_end_lat is None or gbfs_end_lon is None:
        return False
    last_ms, lat, lon = last_point
    if distance_meters(lat, lon, gbfs_end_lat, gbfs_end_lon) > _GBFS_CORRELATION_RADIUS_M:
        return False
    reappeared_ms = int(gbfs_reappeared_at.timestamp() * 1000)
    return abs(reappeared_ms - last_ms) <= _GBFS_TIME_WINDOW_MS


def finalize_validation(cur, ride_id: str) -> dict[str, Any] | None:
    """Settle a ride's contribution eligibility once the thing a
    `pending_feed` status was waiting on resolves. Called from this
    module's own resolve path (update_watches_for_cycle, above) and from
    src/cli.py:expire_stale_watches, so a `pending_feed` ride settles
    (an award, or `end_mismatch`) without the rider having to do anything.

    LOCK BEFORE THE READ — load-bearing, see the ADVISORY-LOCK ORDERING
    note on update_watches_for_cycle's reappeared branch above.
    `ride_validation:<ride_id>` is the exact same key/idiom
    src/api_tracked_rides.py's start handler uses for its own advisory
    lock (`pg_advisory_xact_lock(hashtextextended(key, 0))`), and the A2
    donation transaction takes this SAME lock before it touches the ride
    row too — so this function and a donation landing on the same ride
    serialize against each other instead of racing.

    `cur` is an open cursor in the CALLER's transaction; commit is the
    caller's responsibility (same contract as src/points.py:credit_points
    and src/battery_model.py:ingest_donated_observation). Two situations,
    both routed through this one function:

    1. No donation exists for this ride yet (by far the common case — most
       `pending_feed` rides are never donated before their watch expires).
       Nothing to settle; this just refreshes validation_status the same
       way PATCH .../end already computes it
       (api_tracked_rides._provisional_validation, reused verbatim), now
       that gbfs_reappeared_at may have changed since /end last computed
       it. Cannot reach 'eligible' (that function never returns it) and
       therefore never touches ingest_donated_observation.
    2. A donation exists and is still `points_settled_at IS NULL` — the
       donation transaction wrote 'pending_feed' because GBFS had not
       resolved at donation time. Settles eligible/ineligible by
       re-deriving the one outstanding check (_gbfs_end_matches, above)
       against the now-current (or, on an expiry call, still-absent)
       gbfs_reappeared_at/gbfs_end_lat/gbfs_end_lon; stamps
       track_donations.points_settled_at UNCONDITIONALLY — eligible or
       denied both start the de-id clock — and on eligible ALSO
       (a) credits `battery_contribution`/`nav_distance_bonus` (the
       "distance-dependent points held" that POST .../track's own
       docstring describes for a `pending_feed` donation — see
       src/api_tracked_rides.py:donate_track, same gating logic:
       `ride_options.battery_modeling`/not own-device/both batteries known
       for battery, `ride_options.nav_improvement`/a `ride_routes` row for
       nav, and NEITHER when the donation's stored `points_status` reads
       "pending_review" — the flag track_verify.py computed at donation
       time and this function reads back off `track_donations.verification`,
       since the raw batches themselves are long gone by settle time) and
       then (b) runs ingest_donated_observation, the ONLY ingestion path
       for a donation that arrived before GBFS resolved (the donation
       transaction itself ingests only when GBFS had already resolved at
       donation time) — same "recompute validation_status -> award points
       -> ingest battery observation" order PLAN_RIDE_MODE_API.md states
       for the donation transaction.

    Returns None when there was nothing to do: no such ride; validation_status
    was not 'pending_feed' (already settled, or never reached that state);
    or situation 1 recomputed the SAME provisional status the ride already
    had (the un-donated, gbfs-still-unresolved case — see that branch's own
    comment). This makes a call idempotent no matter how many times the
    same ride is re-selected. Otherwise a small summary dict; situation 2's
    dict additionally carries `points_awarded` (a list of `{"action",
    "points"}`, empty when nothing was credited).
    """
    # LOCK BEFORE THE READ.
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"ride_validation:{ride_id}",),
    )

    cur.execute(
        """
        SELECT vehicle_identifier, ride_options, validation_status,
               gbfs_reappeared_at, gbfs_end_lat, gbfs_end_lon,
               track_key_issued_at, user_reported_ended_at,
               feed_start_battery_percent, reported_start_battery_percent,
               reported_battery_percent, start_lat, start_lon, account_id
        FROM tracked_rides WHERE id = %s FOR UPDATE
        """,
        (str(ride_id),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    (vehicle_identifier, ride_options, validation_status,
     gbfs_reappeared_at, gbfs_end_lat, gbfs_end_lon,
     track_key_issued_at, user_reported_ended_at,
     feed_start_battery_percent, reported_start_battery_percent,
     reported_battery_percent, start_lat, start_lon, account_id) = row

    if validation_status != "pending_feed":
        return None  # already settled, or never reached pending_feed

    cur.execute(
        """
        SELECT id, vehicle_model, distance_meters, verification
        FROM track_donations
        WHERE tracked_ride_id = %s AND points_settled_at IS NULL
        """,
        (str(ride_id),),
    )
    donation = cur.fetchone()

    if donation is None:
        # Situation 1 — see docstring: nothing donated (yet). Refresh the
        # provisional status only.
        new_status, new_reasons = _provisional_validation(
            ride_options, gbfs_reappeared_at=gbfs_reappeared_at)

        # A genuine no-op when nothing actually changed — this matters for
        # a ride whose watch expired WITHOUT gbfs ever resolving and that
        # was never donated: _provisional_validation has no way to express
        # "gbfs will now never resolve" (it recomputes 'pending_feed'
        # right back, since gbfs_reappeared_at is still None), and
        # expire_stale_watches' selection (watch_expires_at < NOW(),
        # gbfs_reappeared_at IS NULL) keeps matching that same ride on
        # every future run forever — skipping the write when the status
        # didn't move at least avoids a pointless UPDATE each time; the
        # ride stays genuinely undecided until either gbfs resolves after
        # all or the rider donates (verify_track_chain's own check 5 then
        # settles it as this SAME branch's "has a pending donation" sibling
        # below would, on the next run).
        if new_status == validation_status:
            return None

        validated_at = (
            datetime.now(timezone.utc)
            if new_status not in ("pending", "pending_feed") else None
        )
        cur.execute(
            """
            UPDATE tracked_rides SET
                validation_status = %s, validation_reasons = %s::jsonb,
                validated_at = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (new_status, json.dumps(new_reasons), validated_at, str(ride_id)),
        )
        return {"ride_id": str(ride_id), "status": new_status,
                "reasons": new_reasons, "ingested": False}

    donation_id, vehicle_model, distance_meters_, verification = donation

    cur.execute(
        "SELECT recorded_ms, lat, lon FROM donated_track_points "
        "WHERE donation_id = %s ORDER BY seq DESC LIMIT 1",
        (str(donation_id),),
    )
    last = cur.fetchone()
    last_point = (last[0], last[1], last[2]) if last else None

    eligible = _gbfs_end_matches(
        last_point, gbfs_reappeared_at=gbfs_reappeared_at,
        gbfs_end_lat=gbfs_end_lat, gbfs_end_lon=gbfs_end_lon,
    )
    new_status = "eligible" if eligible else "ineligible"
    new_reasons: list[str] = [] if eligible else ["end_mismatch"]
    now = datetime.now(timezone.utc)

    # points_settled_at stamped UNCONDITIONALLY — see docstring.
    cur.execute(
        "UPDATE track_donations SET points_settled_at = %s WHERE id = %s",
        (now, str(donation_id)),
    )
    cur.execute(
        """
        UPDATE tracked_rides SET
            validation_status = %s, validation_reasons = %s::jsonb,
            validated_at = %s, updated_at = NOW()
        WHERE id = %s
        """,
        (new_status, json.dumps(new_reasons), now, str(ride_id)),
    )

    points_awarded: list[dict[str, Any]] = []
    ingested = False
    if eligible:
        # "distance-dependent points held" (src/api_tracked_rides.py:
        # donate_track's own docstring, for the pending_feed case this
        # function is settling) -- awarded HERE on the late transition,
        # with the SAME gating and points_status hold that donate_track
        # applies at donation time. points_status itself never got a
        # column of its own; it rides along inside track_donations.verification
        # (stamped there by donate_track) because the raw batches
        # verify_track_chain computed it from are long gone by settle time.
        points_status = (
            verification.get("points_status", "ok")
            if isinstance(verification, dict) else "ok"
        )
        own_device = isinstance(ride_options, dict) and ride_options.get("own_device") is True
        battery_modeling_on = (
            isinstance(ride_options, dict) and ride_options.get("battery_modeling") is True
        )
        nav_improvement_on = (
            isinstance(ride_options, dict) and ride_options.get("nav_improvement") is True
        )
        soc_start = (
            feed_start_battery_percent if feed_start_battery_percent is not None
            else reported_start_battery_percent
        )
        both_batteries_known = soc_start is not None and reported_battery_percent is not None
        may_award = points_status == "ok"

        if may_award and battery_modeling_on and not own_device and both_batteries_known:
            award = credit_battery_contribution(
                cur, account_id=account_id, vehicle_identifier=vehicle_identifier,
                distance_m=distance_meters_, start_lat=start_lat, start_lng=start_lon,
                ride_id=str(ride_id),
            )
            if award is not None:
                points_awarded.append({"action": award["action"], "points": award["points"]})

        if may_award and nav_improvement_on:
            # ride_routes doesn't exist until PLAN_RIDE_MODE_API.md phase
            # A3 (sql/052) -- same to_regclass guard as
            # src/api_tracked_rides.py:donate_track and
            # src/cli.py:deidentify_donations.
            cur.execute("SELECT to_regclass('ride_routes')")
            (ride_routes_relid,) = cur.fetchone()
            has_route_row = False
            if ride_routes_relid is not None:
                cur.execute(
                    "SELECT 1 FROM ride_routes WHERE tracked_ride_id = %s LIMIT 1",
                    (str(ride_id),),
                )
                has_route_row = cur.fetchone() is not None
            if has_route_row:
                award = credit_nav_distance_bonus(
                    cur, account_id=account_id, vehicle_identifier=vehicle_identifier,
                    distance_m=distance_meters_, start_lat=start_lat, start_lng=start_lon,
                    ride_id=str(ride_id),
                )
                if award is not None:
                    points_awarded.append({"action": award["action"], "points": award["points"]})

        if points_awarded:
            # track_donations.points_awarded defaults to 0 at INSERT time
            # (src/api_tracked_rides.py:donate_track never knew this ride's
            # eventual award when it wrote the row, since GBFS hadn't
            # resolved yet) -- stamp the real total now that it's known,
            # same column the donation endpoint itself updates on an
            # immediate-eligible settle.
            cur.execute(
                "UPDATE track_donations SET points_awarded = %s WHERE id = %s",
                (sum(p["points"] for p in points_awarded), str(donation_id)),
            )

        ride_row = {
            "vehicle_identifier": vehicle_identifier,
            "track_key_issued_at": track_key_issued_at,
            "user_reported_ended_at": user_reported_ended_at,
            "feed_start_battery_percent": feed_start_battery_percent,
            "reported_start_battery_percent": reported_start_battery_percent,
            "reported_battery_percent": reported_battery_percent,
        }
        donation_row = {
            "id": donation_id, "vehicle_model": vehicle_model,
            "distance_meters": distance_meters_,
        }
        ingested = ingest_donated_observation(
            cur, ride_row=ride_row, donation_row=donation_row) is not None

    return {"ride_id": str(ride_id), "status": new_status,
            "reasons": new_reasons, "ingested": ingested,
            "points_awarded": points_awarded}
