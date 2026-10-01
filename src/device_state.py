"""Per-scooter persistent state + history maintenance.

Called once per ingest cycle (after compute, before transmit). Reads the
current `device_state` rows for any vehicle_identifier we observed this
cycle, applies a four-way branch per device, and writes back updated state
+ optional history rows.

The branches:
  * NEW              — never seen this identifier before. Insert state row,
                       insert first history row. Counter starts at 0.
  * IN_RENTAL        — observed with is_reserved true. Freeze: stored
                       position is NOT updated, no trip, no new history
                       row. See RENTALS below.
  * MOVED            — distance to stored position > threshold. Close the
                       prior open history row (set departed_at), insert a
                       new one, reset first_observed_at_location, reset
                       failed-starts counter.
  * FAILED_START     — distance ≤ threshold AND device_id rotated. Bump
                       counter on the state row and on the currently-open
                       history row.
  * STATIONARY       — distance ≤ threshold AND device_id unchanged. Just
                       update last_observed_at.

Devices with no vehicle_identifier (the upstream payload didn't embed a
plate in rental_uris) are skipped entirely — we have no stable key to
track them across cycles.

Each MOVED transition also appends one row to `trip_events` (a
"successful trip" for popularity-tracking purposes — see
src/daily_trips.py for the daily rollup computed at 9am alongside the
compliance SLA job).

RENTALS — why IN_RENTAL exists (sql/069)
----------------------------------------
"Distance moved between consecutive cycles means somebody rode it there"
is only true if a rented vehicle is INVISIBLE while it is being ridden,
so that the next observation is already the drop point. Veo does not do
that: it keeps the vehicle in free_bike_status for the whole rental,
sampled every 2 minutes, broadcasting its live moving position, with
is_reserved true (measured — see src/ride_watch.py's own
"WHAT CHECKED OUT ACTUALLY LOOKS LIKE", and the correction note in
API_REQUIREMENTS.md).

So every 2-minute sample of a moving rental cleared the threshold and
appended its own trip_events row. One rental became ~10 "trips", and one
stop in device_history fragmented into ~10 two-minute stops — which is
what dwell_stats reads. On 2026-08-09: 187,820 MOVED steps over 16 m, of
which 161,160 (86%) fall inside a reservation episode, against 30,566
reservation episodes. Roughly a 6x over-count, inherited by
daily_trip_summary, daily_vehicle_trip_counts, H3 popularity and area
leaders.

IN_RENTAL fixes it by NOT following the vehicle. On the first reserved
cycle we close the open history row (dwell at the origin genuinely ends
when the rider unlocks it) and stamp device_state.rental_started_at, but
leave current_lat/current_lon pointing at the origin. Every later
reserved cycle only bumps last_observed_at. When is_reserved clears, the
stored position is still the origin, so the ordinary distance comparison
below sees origin -> drop point and fires exactly ONE MOVED with exactly
one trip_events row. No new branch is needed for the release itself —
that is what MOVED was always for.

Two details that fall out of this and are deliberate:

  * A rental that ends within the threshold of where it started (a
    cancelled reservation, or a genuine round trip) produces NO
    trip_events row, the same as any other sub-threshold observation. It
    still gets a fresh history row and a reset dwell clock, because the
    vehicle demonstrably left and came back — see _release_reopens_stop
    in the write section. 8,384 of that day's 30,566 episodes lasted
    ≤ 2 minutes and are mostly this.
  * A vehicle that IS absent while rented still works untouched: nothing
    observes it, so nothing updates, and its reappearance elsewhere is a
    single MOVED. Both operator conventions land on the same answer.

is_reserved is None when upstream omits it or sends a non-bool
(src/ingest.py normalises that) and reads as NOT in a rental — a feed
that stops publishing the flag degrades to the old behaviour rather than
freezing every device forever.

ABSENCE — a stop also ends when the vehicle leaves the feed (sql/083)
--------------------------------------------------------------------
Until sql/083 a stop ended only on MOVED or a rental's start, so a vehicle
that simply left the feed (pulled for repair, retired, re-keyed) kept an
open stop forever: a "ghost stop". src/equity_backfill.py reconstructs the
fleet at a past instant from open stops, and by 2026-09 ghosts had pushed
that reconstruction 10-17% above the fleet the cycles actually recorded. Every
snapshot from 2026-09-12 on failed its ±10% fidelity gate.

Absence from the feed is real absence. Veo keeps a RENTED vehicle in the
feed for the whole rental (is_reserved, above), and every branch here,
IN_RENTAL included, refreshes last_observed_at. So a vehicle whose
last_observed_at is old is not being ridden; it is not there.
`close_absent_stops` runs at the end of every cycle, after this cycle's
observations are written, and closes the open stop of every vehicle that
  * has been out of the feed for longer than ABSENT_STOP_AFTER, AND
  * has missed at least ABSENT_MIN_MISSED_CYCLES consecutive observed cycles.
The second condition matters after an ingest outage. Without it, the first
cycle back would judge every vehicle against a wall-clock gap made by our
own downtime.

departed_at is set to the vehicle's LAST OBSERVED time, the last moment it
was known to be there, and never to the moment the rule fired. So the
threshold decides only WHICH absences count as a departure, not how much
of the absence is still counted as parked. departure_reason is 'absent'.
Every other close now records 'moved'.

REAPPEARANCE. What happens when a vehicle whose stop was closed as absent
comes back:
  * Elsewhere (beyond stationary_threshold_meters): an ordinary MOVED. It opens a new stop and
    writes the same trip_events row it always did. The prior stop is
    already closed, so the MOVED close leaves its departed_at alone.
  * At the same place (within it), with the same bike_id (STATIONARY) or
    a rotated one (FAILED_START): a NEW device_history row is opened at the
    reappearance time. The old stop is NOT reopened. Reopening it would
    claim the vehicle was parked there for the whole absence, and that is
    the ghost interval this change removes. In the reconstruction it would
    count a vehicle the feed did not contain.
    device_state is NOT reset. current position, first_observed_at_location
    (the dwell clock) and number_failed_starts carry on as before. The dwell
    clock answers "how long has this scooter sat at this spot", and nothing
    observed says it left the spot. A scooter that drops out of the feed and
    reappears in place has not been ridden. If it has sat there 72 h, the
    ghost rule in src/quality.py should still say so. Resetting the clock
    would clear that verdict, and it would change live dwell and reliability
    figures for a reason that has nothing to do with them. (Measured
    2026-09-29: most returns after 6-24 h out of the feed were within 16 m.)
So device_history becomes "contiguous presence in the feed at one place",
and device_state keeps "time at this place". The two agree except across an
absence longer than ABSENT_STOP_AFTER.

The new row is triggered by "this vehicle is in the feed, not in a rental,
and has no open stop". That is checked against device_history itself, not a
flag that could go stale, so stops closed by
`python -m src.cli close_ghost_stops` are caught too. The check probes only
vehicles last seen more than ABSENT_STOP_AFTER ago, since no other vehicle
can have had its stop closed as absent.

Distance is computed flat-earth using the device's own latitude as the
local longitude scale. At Denver's ~40° latitude over 16m distances the
approximation error is < 1cm — irrelevant.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from . import device_features
from .config import load
from .geo import distance_meters as _distance_meters
from .ingest import TaggedDevice
from .pg import connection

log = logging.getLogger(__name__)

#: How long a vehicle must be out of the feed before its open stop is closed
#: as 'absent' (see ABSENCE in the module docstring). departed_at is always
#: the last observed time, so this does not decide how much of an absence is
#: removed. It decides WHICH absences count as a departure. Any absence
#: shorter than this stays inside the stop, and src/equity_backfill.py will
#: count that vehicle as parked while the recorded fleet did not.
#:
#: Measured on production, 2026-09-29, from per-cycle raw telemetry and
#: device_state:
#:  * Short gaps are feed blips, not departures. Vehicles that came back
#:    within 5-30 min reappeared within 16 m of where they vanished 76-85% of
#:    the time. At 30-60 min that fell to 65%, and "came back somewhere
#:    else" rises from there.
#:  * Nothing marks "gone for good". Vehicles came back after 12-24 h, after
#:    days, and after weeks. So the threshold cannot wait for permanence, and
#:    need not, because a returning vehicle gets a new stop.
#:  * What the gate needs. At 06:00 Denver, inside the SLA window, the open
#:    Denver-core stops of vehicles out of the feed for under 1 h were 0.5% of
#:    the fleet, under 2 h 1.1%, under 6 h 4.5%, and under 12 h 8.4%. That is
#:    the most a threshold of that length can leave counted. The overnight
#:    pulls sit in the 2-12 h range and redeploy through the morning, so a
#:    threshold of several hours would spend most of the ±10% gate on them.
#: So one hour. It is long enough that the common few-minute blips do not
#: split a stop, and short enough to hold the residual overcount near half a
#: percent.
ABSENT_STOP_AFTER = timedelta(hours=1)

#: ...and it must also have missed this many consecutive OBSERVED cycles
#: (snapshot_metadata_core rows, which only fresh-payload cycles write). After
#: an ingest outage, wall-clock time alone would count our own downtime as the
#: vehicles' absence, and one partial recovery cycle could close stops that
#: are still in use. Five cycles is ten minutes of real observations.
ABSENT_MIN_MISSED_CYCLES = 5

#: The per-cycle sweep looks only at vehicles that became absent within the
#: last this-many OBSERVED cycles (about an hour at the 2-minute cadence).
#: The window is counted in cycles, not wall-clock time, so an ingest outage
#: of any length is still covered by the first sweeps after it. Up to this
#: many consecutive failed sweeps can be absorbed. The one-off cleanup
#: (`python -m src.cli close_ghost_stops`) applies the same rule with no
#: window, and can be re-run at any time as the backstop.
#:
#: Why bound it at all: an unbounded sweep has to visit every open stop or
#: every long-gone vehicle, on every cycle. On production (2026-09-29) that
#: cost 190-240 ms, most of it dead index entries in device_history's
#: open-stop range, against a storage phase whose median is 2.7 s. The
#: bounded sweep visits the few vehicles that crossed the line since the
#: last hour of cycles, in a few ms.
ABSENT_SWEEP_WINDOW_CYCLES = 30


@dataclass(frozen=True)
class AbsenceWindow:
    """`since <= last_observed_at < cutoff` selects the vehicles to close.

    `since` None means no lower bound: the one-off cleanup and its dry run.
    """
    cutoff: datetime
    since: datetime | None = None


def absence_window(cur, now: datetime, *, bounded: bool) -> AbsenceWindow | None:
    """The absence rule as of `now`, from the recent observed cycles.

    cutoff = the earlier of `now - ABSENT_STOP_AFTER` and the time of the
    ABSENT_MIN_MISSED_CYCLES-th most recent observed cycle. A vehicle last seen
    before it has been out of the feed long enough AND missed enough real
    cycles.

    since (bounded only) = the cutoff as it stood ABSENT_SWEEP_WINDOW_CYCLES
    observed cycles ago. Everything absent before that was already eligible
    for an earlier sweep.

    Returns None when fewer than ABSENT_MIN_MISSED_CYCLES observed cycles
    exist. Nothing can be judged absent then.

    KNOWN LIMITATION, AND THE THIRD FAILURE CASE. `snapshot_metadata_core`
    rows prove a feed snapshot was WRITTEN, not that this module observed it:
    `cycle.py` commits the core snapshot and then calls `update_for_cycle`
    inside a try/except that logs and swallows. So rows can exist for cycles
    `device_state` never processed, and the missed-cycle term counts them.

    The guard survives failed sweeps (the window absorbs them) and ingest
    outages (no rows, so no cycles to miss). It does NOT cover the case where
    ingest is healthy and this updater alone is down, because then the rows
    keep arriving while `last_observed_at` goes stale.

    The exposure is narrow in both directions. `cutoff` takes the EARLIER of
    `now - ABSENT_STOP_AFTER` and `times[k-1]`; at the healthy two-minute
    cadence `times[k-1]` is only ~8 minutes back, so the one-hour term binds
    and the cycle term never fires. It takes **more than an hour of continuous
    updater-only failure** to backdate a stop, and past roughly two hours the
    bounded `since` floor slides below the stale `last_observed_at`, so those
    vehicles drop out of the per-cycle sweep and become ordinary ghosts for
    `close_ghost_stops`.

    Inside that window a stop is truncated at the outage start and a fresh one
    opens on recovery — a hole the length of the outage. That makes the
    reconstruction UNDERCOUNT, the opposite direction from the ghost bias, so
    it trips the ±10% fidelity gate rather than publishing a confident wrong
    number; with sql/084 the day reads `unmeasurable`. Failing closed is why
    this is a follow-up and not a blocker.

    The fix is to record the cycles this module actually processed, atomically
    with its own transaction, and read those instead. Deliberately not done
    here: it needs its own migration and a retention trim.
    """
    k, m = ABSENT_MIN_MISSED_CYCLES, ABSENT_SWEEP_WINDOW_CYCLES
    cur.execute(
        """
        SELECT snapshot_time FROM snapshot_metadata_core
        WHERE snapshot_time <= %s
        ORDER BY snapshot_time DESC
        LIMIT %s
        """,
        (now, k + m),
    )
    times = [r[0] for r in cur.fetchall()]
    if len(times) < k:
        return None
    cutoff = min(now - ABSENT_STOP_AFTER, times[k - 1])
    since = None
    if bounded and len(times) >= k + m:
        # times[m] is the observed cycle m cycles back. The cutoff it would
        # have computed is min(times[m] - T, its k-th most recent cycle),
        # which is times[m + k - 1].
        since = min(times[m] - ABSENT_STOP_AFTER, times[m + k - 1])
    return AbsenceWindow(cutoff=cutoff, since=since)


# One rule, three users: the per-cycle sweep, the one-off cleanup
# (src/ghost_stops.py) and its dry run. Driven from device_state's
# last_observed_at index into sql/004's partial open-stop index.
#
# The writing form LOCKS the device_state rows it acts on, with SKIP LOCKED.
# Without that lock, a vehicle could come back in a concurrent cycle between
# that cycle's open-stop probe and this close, and be left in the feed with
# no open stop. With it, the two serialise on the device_state row. A row
# some cycle already holds is a vehicle that cycle has just seen, so skipping
# it is the right answer, and nothing ever waits (so no deadlock between
# overlapping cycles either).
#
# GREATEST() is defensive only: last_observed_at is never earlier than the
# arrival of the vehicle's open stop, because every write that inserts a stop
# also sets it.
def _absent_vehicles_sql(window: AbsenceWindow, *, lock: bool) -> str:
    sql = """
        SELECT vehicle_identifier, last_observed_at FROM device_state
        WHERE last_observed_at < %(cutoff)s
    """
    if window.since is not None:
        sql += " AND last_observed_at >= %(since)s"
    if lock:
        sql += " FOR UPDATE SKIP LOCKED"
    return sql


def close_absent_stops(cur, window: AbsenceWindow | None) -> int:
    """Close every open stop whose vehicle is absent under `window`.

    Stamps departed_at with the vehicle's last observed time and
    departure_reason 'absent'. Runs on the caller's cursor and transaction
    and does not commit. Idempotent: a closed stop no longer matches
    `departed_at IS NULL`. Returns the number of stops closed.
    """
    if window is None:
        return 0
    cur.execute(
        f"""
        UPDATE device_history h
        SET departed_at = GREATEST(s.last_observed_at, h.snapshot_time),
            departure_reason = 'absent'
        FROM ({_absent_vehicles_sql(window, lock=True)}) s
        WHERE h.vehicle_identifier = s.vehicle_identifier
          AND h.departed_at IS NULL
        """,
        {"cutoff": window.cutoff, "since": window.since},
    )
    return max(cur.rowcount or 0, 0)


def absent_stop_candidates(cur, window: AbsenceWindow | None) -> list[dict[str, Any]]:
    """What `close_absent_stops(cur, window)` would close. Writes and locks
    nothing, so it runs on a read-only session."""
    if window is None:
        return []
    cur.execute(
        f"""
        SELECT h.id, h.vehicle_identifier, h.snapshot_time,
               GREATEST(s.last_observed_at, h.snapshot_time) AS departed_at,
               h.spatial_status
        FROM device_history h
        JOIN ({_absent_vehicles_sql(window, lock=False)}) s
          ON h.vehicle_identifier = s.vehicle_identifier
        WHERE h.departed_at IS NULL
        ORDER BY h.id
        """,
        {"cutoff": window.cutoff, "since": window.since},
    )
    cols = ("id", "vehicle_identifier", "arrived", "departed_at", "spatial_status")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@dataclass
class StateUpdateStats:
    new_devices: int = 0
    moved: int = 0
    failed_starts: int = 0
    stationary: int = 0
    skipped_no_identifier: int = 0
    # sql/069. rentals_started + rentals_held are the samples that USED to
    # be counted as moved; rentals_ended is how many of this cycle's `moved`
    # are the one-per-rental relocation the fix exists to produce.
    rentals_started: int = 0
    rentals_held: int = 0
    rentals_ended: int = 0
    # sql/072: of this cycle's rentals_ended, how many took the rider nowhere.
    rentals_no_go: int = 0
    # sql/083. stops_reopened: vehicles back in the feed, at the spot where
    # their stop was closed as absent, that got a new stop (see ABSENCE).
    # stops_closed_absent: stops this cycle's sweep closed as absent.
    stops_reopened: int = 0
    stops_closed_absent: int = 0


def update_for_cycle(
    cycle_id: uuid.UUID,
    snapshot_time: datetime,
    devices: Iterable[TaggedDevice],
) -> StateUpdateStats:
    """Apply state + history updates for one cycle's worth of devices.

    Runs in a single transaction. The set of devices passed in is the
    polygon-refined observation list — but spatial_status here is taken
    from the device as it stood after envelope tagging; whether a device
    falls in denver_core vs other_outlier doesn't change its state-tracking
    eligibility (we still want to know if a scooter migrated to Aurora).
    """
    threshold = load().device_tracking.stationary_threshold_meters
    stats = StateUpdateStats()

    # Filter to devices with a usable identifier
    eligible = [d for d in devices if d.vehicle_identifier]
    stats.skipped_no_identifier = sum(1 for d in devices if not d.vehicle_identifier)

    if not eligible:
        # Nothing observable this cycle — but absence still needs sweeping.
        # A fresh payload with no usable identifier in it is the strongest
        # evidence of absence there is, and it is exactly the case where
        # every open stop wants closing: a withdrawal of the whole fleet, or
        # a feed that stopped carrying plates. Returning here without the
        # sweep would leave behind precisely the ghost stops this rule
        # exists to remove, in the one case that matters most. Nothing
        # upstream aborts a zero-device cycle (src/cycle.py writes the core
        # snapshot and calls this either way), so it has to be handled here.
        with connection() as conn:
            with conn.cursor() as cur:
                stats.stops_closed_absent = close_absent_stops(
                    cur, absence_window(cur, snapshot_time, bounded=True))
            conn.commit()
        log.info(
            "device_state cycle=%s: nothing eligible (skipped=%d), "
            "swept only: stops(closed_absent=%d)",
            cycle_id, stats.skipped_no_identifier, stats.stops_closed_absent,
        )
        return stats

    with connection() as conn:
        with conn.cursor() as cur:
            # Pull existing state for everything we saw, in one round-trip.
            ids = [d.vehicle_identifier for d in eligible]
            cur.execute(
                """
                SELECT vehicle_identifier, current_device_id, current_lat, current_lon,
                       first_observed_at_location, number_failed_starts,
                       first_ever_observed_at, rental_started_at, last_observed_at
                FROM device_state
                WHERE vehicle_identifier = ANY(%s)
                FOR UPDATE
                """,
                (ids,),
            )
            prior: dict[str, tuple] = {row[0]: row[1:] for row in cur.fetchall()}

            # sql/083. Which of the vehicles seen now lost their open stop to
            # the absence rule? Only a vehicle last seen more than
            # ABSENT_STOP_AFTER ago can have, because both the sweep and the
            # cleanup require exactly that. So only those few are probed, not
            # the whole fleet (see ABSENT_SWEEP_WINDOW_CYCLES on why a
            # fleet-wide open-stop probe is not free).
            long_absent = [
                vid for vid, row in prior.items()
                if row[7] is not None and row[7] < snapshot_time - ABSENT_STOP_AFTER
            ]
            without_open_stop: set[str] = set()
            if long_absent:
                cur.execute(
                    """
                    SELECT DISTINCT vehicle_identifier FROM device_history
                    WHERE vehicle_identifier = ANY(%s) AND departed_at IS NULL
                    """,
                    (long_absent,),
                )
                with_open = {r[0] for r in cur.fetchall()}
                without_open_stop = set(long_absent) - with_open

            new_state_rows: list[tuple] = []
            moved_updates: list[tuple] = []
            failed_start_updates: list[tuple] = []
            stationary_updates: list[tuple] = []
            new_history_rows: list[tuple] = []
            close_history_ids: list[str] = []
            trip_event_rows: list[tuple] = []
            rental_start_updates: list[tuple] = []   # sql/069
            rental_hold_updates: list[tuple] = []
            rental_outcome_updates: list[tuple] = []  # sql/072

            for d in eligible:
                vid = d.vehicle_identifier
                if vid not in prior:
                    # NEW device — first observation ever
                    stats.new_devices += 1
                    # max_observed_range_{meters,at} seed: only set if this
                    # first sighting reported a charge level. Otherwise leave
                    # NULL so a later cycle with a real value becomes the seed.
                    seed_max_at = snapshot_time if d.current_range_meters is not None else None
                    new_state_rows.append((
                        vid, d.vehicle_plate, d.device_id, d.lat, d.lon,
                        d.spatial_status, d.form_factor,
                        snapshot_time,    # first_observed_at_location
                        0,                # number_failed_starts
                        snapshot_time,    # first_ever_observed_at
                        snapshot_time,    # last_observed_at
                        str(cycle_id),
                        d.h3_8_index, d.h3_9_index, d.h3_10_index,
                        d.current_range_meters,  # max_observed_range_meters
                        seed_max_at,             # max_observed_range_at
                        d.vehicle_use_type, d.vehicle_model_name,
                        d.vehicle_type_id,
                        # sql/069. A device whose very first sighting is
                        # mid-rental still needs the flag, or its release
                        # would read as an ordinary MOVED from wherever it
                        # happened to be when we first saw it. The origin is
                        # unknowable in that case — this at least keeps the
                        # ONE-trip-per-rental invariant.
                        snapshot_time if d.is_reserved is True else None,
                    ))
                    if d.is_reserved is True:
                        stats.rentals_started += 1
                    new_history_rows.append((
                        vid, d.vehicle_plate, str(cycle_id), snapshot_time,
                        d.lat, d.lon, d.spatial_status, d.form_factor,
                        d.device_id, 0,
                        d.h3_8_index, d.h3_9_index, d.h3_10_index,
                        d.vehicle_use_type, d.vehicle_model_name,
                    ))
                    continue

                (prev_device_id, prev_lat, prev_lon, prev_first_seen, prev_fs,
                 _ever, prev_rental_started_at, _prev_last_seen) = prior[vid]

                # IN_RENTAL (sql/069) — see RENTALS in the module docstring.
                # Freeze before any distance is computed: the stored position
                # must keep pointing at the origin so the release below is a
                # single origin -> drop-point MOVED.
                if d.is_reserved is True:
                    if prev_rental_started_at is None:
                        stats.rentals_started += 1
                        # The rider has it; dwell at the origin ends now.
                        close_history_ids.append(vid)
                        rental_start_updates.append((
                            snapshot_time,    # rental_started_at
                            d.device_id, d.spatial_status,
                            snapshot_time, str(cycle_id), vid,
                        ))
                    else:
                        stats.rentals_held += 1
                        rental_hold_updates.append((
                            d.spatial_status, snapshot_time, str(cycle_id), vid,
                        ))
                    continue

                released = prev_rental_started_at is not None
                if released:
                    stats.rentals_ended += 1

                if prev_lat is None or prev_lon is None:
                    distance = float("inf")
                else:
                    distance = _distance_meters(
                        float(prev_lat), float(prev_lon), d.lat, d.lon
                    )

                if distance > threshold or released:
                    # MOVED — close prior stop, open a new one. This is a
                    # "successful trip" for popularity-tracking purposes
                    # (src/daily_trips.py): the vehicle relocated between
                    # consecutive cycles, which for a dockless fleet means
                    # someone rode it somewhere.
                    #
                    # `released` (sql/069) enters here too, and it is the
                    # ONLY way a rental ever produces a trip: prev_lat/lon
                    # were frozen at the origin for the whole rental, so
                    # `distance` is the actual origin -> drop-point
                    # relocation and this fires exactly once. A release that
                    # lands back within the threshold — a cancelled
                    # reservation, or a round trip — takes this branch for
                    # the history row and the dwell reset (the vehicle
                    # demonstrably left and came back), but records neither
                    # `moved` nor a trip_events row, exactly like any other
                    # sub-threshold observation.
                    real_move = distance > threshold
                    if real_move:
                        stats.moved += 1
                    # sql/072: a completed rental's outcome, recorded where it
                    # is already known. `released` means this MOVED closes a
                    # rental rather than an ordinary relocation, and `distance`
                    # is origin-to-drop-point because sql/069 froze the origin
                    # for its duration. A rental that ends within the
                    # stationary threshold of where the rider unlocked it did
                    # not take them anywhere.
                    if released:
                        rental_outcome_updates.append(
                            (0 if real_move else 1, vid))
                        if not real_move:
                            stats.rentals_no_go += 1
                    close_history_ids.append(vid)
                    moved_updates.append((
                        d.vehicle_plate, d.device_id, d.lat, d.lon,
                        d.spatial_status, d.form_factor,
                        snapshot_time,    # new first_observed_at_location
                        snapshot_time,    # last_observed_at
                        str(cycle_id),
                        d.h3_8_index, d.h3_9_index, d.h3_10_index,
                        d.vehicle_use_type, d.vehicle_model_name,
                        d.vehicle_type_id,
                        vid,
                    ))
                    new_history_rows.append((
                        vid, d.vehicle_plate, str(cycle_id), snapshot_time,
                        d.lat, d.lon, d.spatial_status, d.form_factor,
                        d.device_id, 0,
                        d.h3_8_index, d.h3_9_index, d.h3_10_index,
                        d.vehicle_use_type, d.vehicle_model_name,
                    ))
                    if real_move:
                        from_lat = float(prev_lat) if prev_lat is not None else None
                        from_lon = float(prev_lon) if prev_lon is not None else None
                        trip_event_rows.append((
                            vid, d.vehicle_plate, str(cycle_id), snapshot_time,
                            d.form_factor, d.vehicle_use_type, d.vehicle_model_name,
                            from_lat, from_lon, d.lat, d.lon,
                            None if distance == float("inf") else distance,
                        ))
                elif d.device_id != prev_device_id:
                    # FAILED_START — same spot, new bike_id. We deliberately
                    # do NOT update the stored h3 cells here: the scooter
                    # hasn't moved enough to trip the threshold, so its
                    # "current location" cells are unchanged. (GPS drift
                    # could otherwise cause noisy h3_10 flips on every
                    # failed start.)
                    stats.failed_starts += 1
                    failed_start_updates.append((
                        d.device_id, d.spatial_status, d.form_factor,
                        d.vehicle_use_type, d.vehicle_model_name,
                        d.vehicle_type_id,
                        snapshot_time, str(cycle_id), vid,
                    ))
                else:
                    # STATIONARY — same spot, same bike_id
                    stats.stationary += 1
                    stationary_updates.append((
                        d.spatial_status, snapshot_time, str(cycle_id), vid,
                    ))

                # Back in the feed where its stop was closed as absent
                # (sql/083; see REAPPEARANCE in the module docstring): a new
                # stop from now. The old stop is not reopened, and device_state
                # (dwell clock included) is updated exactly as the branch
                # above decided. A MOVED return never gets here, because it
                # opens its own stop.
                if distance <= threshold and not released and vid in without_open_stop:
                    stats.stops_reopened += 1
                    new_history_rows.append((
                        vid, d.vehicle_plate, str(cycle_id), snapshot_time,
                        d.lat, d.lon, d.spatial_status, d.form_factor,
                        d.device_id,
                        # The rotation FAILED_START just counted on
                        # device_state belongs to this stop too. The
                        # dwell_failed_starts bump below only reaches stops
                        # that are already open, and this one is inserted
                        # after it.
                        1 if d.device_id != prev_device_id else 0,
                        d.h3_8_index, d.h3_9_index, d.h3_10_index,
                        d.vehicle_use_type, d.vehicle_model_name,
                    ))

            # Apply writes ----------------------------------------------------
            if new_state_rows:
                cur.executemany(
                    """
                    INSERT INTO device_state (
                        vehicle_identifier, vehicle_plate, current_device_id,
                        current_lat, current_lon, current_spatial_status,
                        current_form_factor, first_observed_at_location,
                        number_failed_starts, first_ever_observed_at,
                        last_observed_at, last_cycle_id,
                        current_h3_8_index, current_h3_9_index, current_h3_10_index,
                        max_observed_range_meters, max_observed_range_at,
                        current_vehicle_use_type, current_vehicle_model_name,
                        current_vehicle_type_id, rental_started_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    new_state_rows,
                )

            # max-observed-range tracker: a single batch UPDATE for every
            # already-existing device that reported a charge this cycle. Only
            # bumps the stored max when the current reading is strictly higher
            # (or no prior max exists), and stamps the observation time so we
            # know when the peak was set. NEW devices were seeded above.
            range_updates = [
                (d.current_range_meters, snapshot_time, d.vehicle_identifier)
                for d in eligible
                if d.current_range_meters is not None
                and d.vehicle_identifier in prior
            ]
            if range_updates:
                cur.executemany(
                    """
                    UPDATE device_state SET
                        max_observed_range_meters = %s,
                        max_observed_range_at     = %s
                    WHERE vehicle_identifier = %s
                      AND (max_observed_range_meters IS NULL
                           OR %s > max_observed_range_meters)
                    """,
                    [(r[0], r[1], r[2], r[0]) for r in range_updates],
                )

            if moved_updates:
                cur.executemany(
                    """
                    UPDATE device_state SET
                        vehicle_plate = %s,
                        current_device_id = %s,
                        current_lat = %s,
                        current_lon = %s,
                        current_spatial_status = %s,
                        current_form_factor = %s,
                        first_observed_at_location = %s,
                        number_failed_starts = 0,
                        last_observed_at = %s,
                        last_cycle_id = %s,
                        current_h3_8_index = %s,
                        current_h3_9_index = %s,
                        current_h3_10_index = %s,
                        current_vehicle_use_type = %s,
                        current_vehicle_model_name = %s,
                        current_vehicle_type_id = %s,
                        -- sql/069: this is the only place a rental is
                        -- cleared, and it is unconditional — every release
                        -- routes through MOVED (see `or released` above),
                        -- so the flag can never outlive the rental it
                        -- describes.
                        rental_started_at = NULL
                    WHERE vehicle_identifier = %s
                    """,
                    moved_updates,
                )

            # IN_RENTAL, first cycle (sql/069). Deliberately does NOT touch
            # current_lat/current_lon/first_observed_at_location: freezing the
            # origin is the whole mechanism. device_id is picked up because
            # GBFS rotates bike_id per trip and the post-rental FAILED_START
            # comparison would otherwise misread the rotation as a failed
            # unlock at the drop point.
            if rental_start_updates:
                cur.executemany(
                    """
                    UPDATE device_state SET
                        rental_started_at = %s,
                        current_device_id = %s,
                        current_spatial_status = %s,
                        last_observed_at = %s,
                        last_cycle_id = %s
                    WHERE vehicle_identifier = %s
                    """,
                    rental_start_updates,
                )

            # sql/072. Separate from moved_updates because a release lands in
            # the MOVED branch whether or not the vehicle actually went
            # anywhere, and only the counters distinguish the two.
            if rental_outcome_updates:
                cur.executemany(
                    """
                    UPDATE device_state SET
                        rentals_observed = rentals_observed + 1,
                        rentals_no_go    = rentals_no_go + %s
                    WHERE vehicle_identifier = %s
                    """,
                    rental_outcome_updates,
                )

            # IN_RENTAL, every later cycle — liveness only.
            if rental_hold_updates:
                cur.executemany(
                    """
                    UPDATE device_state SET
                        current_spatial_status = %s,
                        last_observed_at = %s,
                        last_cycle_id = %s
                    WHERE vehicle_identifier = %s
                    """,
                    rental_hold_updates,
                )

            if failed_start_updates:
                cur.executemany(
                    """
                    UPDATE device_state SET
                        current_device_id = %s,
                        current_spatial_status = %s,
                        current_form_factor = %s,
                        current_vehicle_use_type = %s,
                        current_vehicle_model_name = %s,
                        current_vehicle_type_id = %s,
                        number_failed_starts = number_failed_starts + 1,
                        last_observed_at = %s,
                        last_cycle_id = %s
                    WHERE vehicle_identifier = %s
                    """,
                    failed_start_updates,
                )

            if stationary_updates:
                cur.executemany(
                    """
                    UPDATE device_state SET
                        current_spatial_status = %s,
                        last_observed_at = %s,
                        last_cycle_id = %s
                    WHERE vehicle_identifier = %s
                    """,
                    stationary_updates,
                )

            # Close out history rows for devices that just moved (or were
            # just unlocked). The "open stop" is the most recent row with
            # departed_at IS NULL. A vehicle whose stop was already closed as
            # absent matches nothing here, so its absent close, stamped at the
            # last moment it was seen, stands.
            if close_history_ids:
                cur.execute(
                    """
                    UPDATE device_history
                    SET departed_at = %s, departure_reason = 'moved'
                    WHERE vehicle_identifier = ANY(%s)
                      AND departed_at IS NULL
                    """,
                    (snapshot_time, close_history_ids),
                )

            # Increment dwell_failed_starts on the currently-open stop for
            # any failed-start events. Same idea: targets the row where
            # departed_at IS NULL.
            if failed_start_updates:
                fs_ids = [u[-1] for u in failed_start_updates]
                cur.execute(
                    """
                    UPDATE device_history SET dwell_failed_starts = dwell_failed_starts + 1
                    WHERE vehicle_identifier = ANY(%s)
                      AND departed_at IS NULL
                    """,
                    (fs_ids,),
                )

            if new_history_rows:
                cur.executemany(
                    """
                    INSERT INTO device_history (
                        vehicle_identifier, vehicle_plate, cycle_id, snapshot_time,
                        lat, lon, spatial_status, form_factor,
                        device_id_observed, dwell_failed_starts,
                        h3_8_index, h3_9_index, h3_10_index,
                        vehicle_use_type, vehicle_model_name
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    new_history_rows,
                )

            if trip_event_rows:
                cur.executemany(
                    """
                    INSERT INTO trip_events (
                        vehicle_identifier, vehicle_plate, cycle_id, detected_at,
                        form_factor, vehicle_use_type, vehicle_model_name,
                        from_lat, from_lon, to_lat, to_lon, distance_meters
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    trip_event_rows,
                )

            # ABSENCE (sql/083). Deliberately LAST among the history writes:
            # everything seen this cycle now has last_observed_at =
            # snapshot_time, so it cannot match. Only vehicles missing from
            # this and the preceding cycles can.
            stats.stops_closed_absent = close_absent_stops(
                cur, absence_window(cur, snapshot_time, bounded=True))

            # Catalog-standard equipment for devices that just gained (or
            # just arrived with) a model that ships with it — e.g. every
            # Rover's cargo basket. After the model-name writes above so a
            # device NEW this cycle is seeded this cycle, not next.
            device_features.seed_catalog_features(cur)

        conn.commit()

    log.info(
        "device_state cycle=%s: new=%d moved=%d failed_starts=%d stationary=%d "
        "skipped=%d rentals(started=%d held=%d ended=%d no_go=%d) "
        "stops(closed_absent=%d reopened=%d)",
        cycle_id, stats.new_devices, stats.moved, stats.failed_starts,
        stats.stationary, stats.skipped_no_identifier,
        stats.rentals_started, stats.rentals_held, stats.rentals_ended,
        stats.rentals_no_go, stats.stops_closed_absent, stats.stops_reopened,
    )
    return stats
