"""Per-scooter persistent state + history maintenance.

Called once per ingest cycle (after compute, before transmit). Reads the
current `device_state` rows for any vehicle_identifier we observed this
cycle, applies a branch per device, and writes back updated state
+ optional history rows.

The branches:
  * NEW              — never seen this identifier before. Insert state row,
                       insert first history row. Counter starts at 0.
  * IN_RENTAL        — observed with is_reserved true. Freeze: stored
                       position is NOT updated, no trip, no new history
                       row. See RENTALS below.
  * IN-PLACE RELEASE — is_reserved just cleared, and the rental never left
                       IN_PLACE_RADIUS_M of its origin (sql/087). With a
                       rotated bike_id it is a FAILED START: bump the
                       counter, keep position and dwell clock, reopen the
                       stop, no trip. Without one it is a reservation blip:
                       the same, minus the counter. See FAILED STARTS below.
  * MOVED            — a release that went somewhere, or (not a rental)
                       distance to stored position > JITTER_RADIUS_M with a
                       rotated bike_id, or > UNROTATED_MOVE_M without. Close
                       the prior open history row (set departed_at), insert a
                       new one, reset first_observed_at_location; clear the
                       failed-starts counter only if the relocation covered
                       FAILED_START_DECAY_M.
  * FAILED_START     — not a rental, distance ≤ JITTER_RADIUS_M AND
                       device_id rotated. Bump counter on the state row and
                       on the currently-open history row.
  * STATIONARY       — not a rental, distance ≤ UNROTATED_MOVE_M AND
                       device_id unchanged. Just update last_observed_at.

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
that is what MOVED was always for — with one exception since sql/087: a
release that never left IN_PLACE_RADIUS_M (see FAILED STARTS below).

Two details that fall out of this and are deliberate:

  * A rental that never left IN_PLACE_RADIUS_M is not a trip at all (see
    FAILED STARTS below). A rental that went farther and came back within
    the stationary threshold (a round trip) produces NO trip_events row,
    the same as any other sub-threshold observation, but still gets a fresh
    history row and a reset dwell clock, because the vehicle demonstrably
    left and came back.
  * A vehicle that IS absent while rented still works untouched: nothing
    observes it, so nothing updates, and its reappearance elsewhere is a
    single MOVED. Both operator conventions land on the same answer.

is_reserved is None when upstream omits it or sends a non-bool
(src/ingest.py normalises that) and reads as NOT in a rental — a feed
that stops publishing the flag degrades to the old behaviour rather than
freezing every device forever.

FAILED STARTS AND JITTER (sql/087)
----------------------------------
Until sql/087 a failed start was counted only for a vehicle that was NOT
reserved, within the 16 m stationary threshold, with a rotated bike_id. On
this feed a rider's unlock IS a reservation, so the common failure (unlock,
it will not go, give up) is a one- or two-cycle rental released where it
stood with a new bike_id. That release routed to MOVED, even 0 m from the
unlock point, which reset number_failed_starts to 0 and restarted the dwell
clock: the strongest predictor of the next failure erased the evidence.
Replaying 2026-09-29, 670 rentals ended inside 50 m of where they started,
never having left it, with a rotated bike_id, and counted for nothing. Over
four days of archive, the next rental after one fails the same way 37.1% of
the time, against 1.8% after any other rental.

Now a release whose rental never got farther than IN_PLACE_RADIUS_M from its
origin, and ended inside it, is an IN-PLACE RELEASE. sql/087 keeps the
rental's running maximum distance (rental_max_distance_m) and its starting
bike_id (rental_origin_device_id) for this. With a rotated bike_id it is a
failed start. Without one it is a reservation blip (followed by a real failure
no more often than any rental) and counts for nothing. Either way nothing
about the stop changes: the stored position and first_observed_at_location
stay, the stop closed at the rental's start is reopened, and no trip_events
row is written. rentals_observed / rentals_no_go (sql/072) are counted exactly
as before.

The 16 m threshold also turned GPS jitter into trips. A parked vehicle's
reported fix wanders, and a new fix sticks until the next one, so a 20 m
jump persists like a real move does. On 2026-09-29 that produced 48,429
non-rental MOVEDs, each resetting the dwell clock and the failed-start
count and writing a trip_events row, about 3x the day's real rentals. So a
vehicle not coming out of a rental must now move more than JITTER_RADIUS_M
with a rotated bike_id, or more than UNROTATED_MOVE_M without one, to be
MOVED. Within JITTER_RADIUS_M a rotated bike_id is a failed start; anything
else that is not MOVED is STATIONARY.

A failed-start count decays only with evidence the vehicle works: a
relocation of at least FAILED_START_DECAY_M. And recent_no_go_mask records
the last RECENT_RENTALS_KEPT rental outcomes (failed start or went
somewhere), which src/quality.py reads as a tier rule of its own, so two
failures separated by one good ride still read high_risk.

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

WHAT COUNTS AS AN OBSERVED CYCLE (sql/086). Not a snapshot_metadata_core
row: src/cycle.py commits that before calling `update_for_cycle`, and
swallows this module's failures, so it proves only that a snapshot was
written. Counting it let a healthy ingest with a failing updater close, and
backdate, the stops of vehicles that were in every one of those feeds
(PR #98 review, finding 1). Instead `update_for_cycle` records each cycle it
processes in `device_state_processed_cycles`, in the same transaction as its
observations, and the rule counts those rows. A cycle whose updater failed
leaves no row, so it is never a miss, however long the failure lasts.

A processed cycle also has to be a plausible observation of the fleet to
count: its eligible payload (devices with a vehicle_identifier) must be
non-empty and at least ABSENT_FLOOR_RATIO of the recent baseline — at least,
not more than, so a payload at exactly half the baseline is believed. An empty
or plate-less payload is far more likely an upstream or ingest glitch than a
withdrawal of the whole fleet (PR #98 review, finding 2), and counting it
would close every open stop an hour in and reopen them all on recovery. Such
a cycle is still processed (what it does contain is observed) and still
sweeps, but it is never one of the missed cycles, so it cannot be the reason
a stop closes. Only vehicles that had already missed
ABSENT_MIN_MISSED_CYCLES real cycles before it can close during it.

departed_at is set to the vehicle's LAST OBSERVED time, the last moment it
was known to be there, and never to the moment the rule fired. So the
threshold decides only WHICH absences count as a departure, not how much
of the absence is still counted as parked. departure_reason is 'absent'.
Every other close now records 'moved'.

REAPPEARANCE. What happens when a vehicle whose stop was closed as absent
comes back:
  * Elsewhere (MOVED by the rules above): an ordinary MOVED. It opens a new stop and
    writes the same trip_events row it always did. The prior stop is
    already closed, so the MOVED close leaves its departed_at alone.
  * At the same place (not MOVED), with the same bike_id (STATIONARY) or
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
#: (sql/086: cycles this module committed, in device_state_processed_cycles,
#: whose payload passed ABSENT_FLOOR_RATIO; never a cycle whose update
#: failed, nor an empty payload, nor one below half the baseline). After
#: an ingest outage, wall-clock time alone would count our own downtime as the
#: vehicles' absence, and one partial recovery cycle could close stops that
#: are still in use. Five cycles is ten minutes of real observations.
ABSENT_MIN_MISSED_CYCLES = 5

#: The per-cycle sweep looks only at vehicles that became absent within the
#: last this-many OBSERVED cycles (about an hour at the 2-minute cadence).
#: The window is counted in cycles, not wall-clock time, so an ingest outage
#: of any length is still covered by the first sweeps after it. Up to this
#: many consecutive failed sweeps can be absorbed (and since sql/086 a failed
#: sweep leaves no ledger row, so any number of them). The one-off cleanup
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

#: A processed cycle counts as an observation of the fleet (one of the cycles
#: a vehicle must miss) only if its eligible count is non-zero AND at least
#: this fraction of the baseline: the median eligible count of the previous
#: ABSENT_BASELINE_CYCLES processed cycles (about 24 h at the 2-minute
#: cadence, so it spans a full daily deployment cycle).
#:
#: Measured read-only on production, 2026-10-02. The eligible count equals
#: snapshot_metadata_core's total_devices_denver + total_not_in_denver
#: exactly (828 of 828 cycles still in raw_telemetry_points, where every
#: device carried an identifier), so the core table gives the history back to
#: 2026-05-30: 66,429 cycles. In those:
#:  * no cycle had zero devices, and the smallest was 6,332;
#:  * against this exact baseline (median of the previous 720 cycles) the
#:    lowest cycle was 0.80; 0.1% of cycles fell below 0.93, 12 below 0.90,
#:    and none below 0.50 (66,403 cycles with >= 30 before them);
#:  * against the previous cycle alone the worst single drop was to 0.78
#:    (2026-06-11 10:10, back to normal two cycles later), and the longest
#:    dip was 2026-09-28 20:14-20:30 UTC, 8 cycles at 0.87-0.93 of the
#:    trailing mean.
#: So no real cycle in four months would have been excluded at 0.5. The floor
#: rejects an empty, plate-less or truncated payload, and a partial one that
#: loses MORE than half the fleet, while leaving room for every dip the fleet
#: has actually shown.
#:
#: THE FLOOR IS INCLUSIVE, which decides where the interesting boundary is.
#: `counts_as_observation` compares `>=`, so a payload at exactly half the
#: baseline counts immediately — it is a cycle we believe, not one we wait out.
#: Only a payload BELOW half is rejected, and only that case has anything to do
#: with the baseline adapting.
#:
#: And it does adapt, because the baseline includes cycles that did not count:
#: a fleet that genuinely drops below half and stays there counts again once
#: that is the median of the last day, about 12 h in. By the same token a
#: partial glitch that lasts longer than that would eventually be believed —
#: the alternative, a baseline of counted cycles only, would never adapt at all
#: and a real contraction would stop absence closing until somebody intervened.
#: A zero count never counts, whatever the baseline, however long it lasts.
#: Until ABSENT_BASELINE_MIN_CYCLES processed cycles exist (just after sql/086
#: is applied) there is no baseline and any non-zero cycle counts.
ABSENT_FLOOR_RATIO = 0.5
ABSENT_BASELINE_CYCLES = 720
ABSENT_BASELINE_MIN_CYCLES = 30

#: Retention for device_state_processed_cycles, trimmed every cycle. The rule
#: reads at most ABSENT_BASELINE_CYCLES rows; the rest is an audit trail of
#: what the updater processed. The newest ABSENT_LEDGER_KEEP_MIN rows are
#: always kept, whatever their age, so an updater outage longer than the
#: retention cannot empty the ledger: the first cycles back then still see the
#: pre-outage cycles, and judge absence against them.
ABSENT_LEDGER_RETENTION = timedelta(days=7)
ABSENT_LEDGER_KEEP_MIN = 1000


# FAILED STARTS AND JITTER (sql/087) — see the module docstring for the
# branches. All three constants were chosen from the R2 archive, 2026-09-27
# 08:00Z .. 2026-10-01 08:00Z (95,198 reservation episodes, 4 days), read-only.

#: A rental is a FAILED START when the vehicle never got farther than this
#: from its origin while reserved, is released within it, and comes back with
#: a rotated bike_id. How often the vehicle's NEXT rental is such a failure,
#: by where a rotated release landed and how far it ever got (4 days):
#:   never > 50 m, end <= 35 m:   40.6%  (2,540 rentals)
#:   never > 50 m, end 35-50 m:   32.4%  (343)
#:   never > 50 m, end 50-75 m:   26.4%  (329)   <- not counted, see below
#:   never > 50 m, end > 75 m:     3.8%  (5,565; sparse samples of real rides)
#:   got farther than 50 m:        1.8%  (78,916; the fleet baseline, 2.1%)
#: The signal tapers rather than stopping. 50 m keeps the geometry to one
#: circle, the same 50 m the jitter rule below uses, and is the top of the
#: owner's 30-50 m range; the 50-75 m band is a known, smaller residual.
#: Inside the circle a release with NO rotation (528) is followed by a
#: rotated failure 4.0% of the time, near baseline: a reservation blip, not
#: an attempt (they recur on the same vehicles), and it counts for nothing.
IN_PLACE_RADIUS_M = 50.0

#: A vehicle that is NOT coming out of a rental is never MOVED (new stop,
#: dwell reset, trip_events row) within this distance of its stored position.
#: Within it, a rotated bike_id is a FAILED START (as it always was inside the
#: stationary threshold) and anything else is STATIONARY: the stored position
#: and dwell clock stay put. Beyond it, a rotated bike_id is MOVED (most
#: likely a short ride between two samples); an unrotated one must also clear
#: UNROTATED_MOVE_M.
#:
#: On 2026-09-29 the old 16 m rule turned 48,429 non-rental position changes
#: into MOVED, two thirds of them 16-25 m, with no rotation, mostly bikes:
#: GPS jitter, ~3x the day's real rentals in trip_events. The alternative the
#: owner offered, "confirm the new position on two consecutive samples", does
#: not separate jitter: of consecutive-sample jumps of 16-25 m, 85.5% are
#: still there one sample later and 65% two samples later (25-35 m: 81%/56%,
#: 35-50 m: 78%/51%; for comparison > 100 m: 83%/66%). A jittered fix sticks
#: until the next fix, so persistence is not evidence of movement.
#: Displacement is, so 50 m.
JITTER_RADIUS_M = 50.0

#: ...and with no rotation and no rental, more than this. Replaying
#: 2026-09-29 with a flat 50 m left 9,113 non-rental MOVEDs, 5,254 of them
#: 50-100 m with no rotation. Whether the vehicle is back within 25 m of where
#: it "left" inside two hours separates drift from movement:
#:   non-rental, no rotation, 50-75 m:   44-47% back  (4,267)
#:                            75-100 m:  21-24%       (987)
#:                           100-150 m:  12%          (960)
#:                           150-300 m:   6-7%        (1,095)
#:                             > 300 m:   1-3%        (759)
#:   rental releases, rotated, > 75 m:    3-7% (the real-move baseline)
#: So 100 m: the drift-dominated bands are out, and what is left behaves
#: within a few points of real rides.
UNROTATED_MOVE_M = 100.0

#: A completed relocation (a rental that went somewhere, or a non-rental
#: MOVED) clears number_failed_starts only if it covered at least this much.
#: A shorter one carries the count to the new spot. After a failed start, the
#: chance the next rental fails too, by how far the rental in between went
#: (4 days): <= 300 m 36.2%, 300-500 m 12.2%, 500-1000 m 5.9%, 1-2 km 5.1%,
#: > 2 km 3.2% (baseline 1.8%). The owner's ~300 m is the knee; 500 m is where
#: a vehicle's risk is back within a few points of the fleet's.
FAILED_START_DECAY_M = 500.0

#: recent_no_go_mask keeps this many rental outcomes (sql/087).
RECENT_RENTALS_KEPT = 3
_RECENT_MASK = (1 << RECENT_RENTALS_KEPT) - 1


@dataclass(frozen=True)
class AbsenceWindow:
    """`since <= last_observed_at < cutoff` selects the vehicles to close.

    `since` None means no lower bound: the one-off cleanup and its dry run.
    """
    cutoff: datetime
    since: datetime | None = None


def counts_as_observation(eligible_count: int, baseline: float | None) -> bool:
    """Whether a processed cycle with `eligible_count` usable devices counts
    as an observation of the fleet (see ABSENT_FLOOR_RATIO)."""
    if eligible_count <= 0:
        return False
    if baseline is None:
        return True
    return eligible_count >= ABSENT_FLOOR_RATIO * float(baseline)


def record_processed_cycle(cur, cycle_id: uuid.UUID, snapshot_time: datetime,
                           eligible_count: int) -> bool:
    """Record that this module processed `cycle_id`, and trim the ledger.

    Runs on the caller's cursor and transaction and does not commit, so the
    row exists exactly when the cycle's observations were committed. Call it
    BEFORE `absence_window` in the same transaction: the current cycle is one
    of the observed cycles it counts. Returns whether the cycle counts as an
    observation (see ABSENT_FLOOR_RATIO).
    """
    cur.execute(
        """
        SELECT count(*), percentile_cont(0.5) WITHIN GROUP (ORDER BY eligible_count)
        FROM (
            SELECT eligible_count FROM device_state_processed_cycles
            WHERE snapshot_time < %s
            ORDER BY snapshot_time DESC
            LIMIT %s
        ) recent
        """,
        (snapshot_time, ABSENT_BASELINE_CYCLES),
    )
    row = cur.fetchone()
    n, median = (row if row is not None else (0, None))
    baseline = median if (n or 0) >= ABSENT_BASELINE_MIN_CYCLES else None
    counts = counts_as_observation(eligible_count, baseline)
    cur.execute(
        """
        INSERT INTO device_state_processed_cycles
            (cycle_id, snapshot_time, eligible_count, baseline_count, counts_as_observation)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (cycle_id) DO NOTHING
        """,
        (str(cycle_id), snapshot_time, eligible_count, baseline, counts),
    )
    # Older than the retention AND not among the newest KEEP_MIN rows. With
    # fewer rows than that the subquery is NULL and nothing is deleted.
    cur.execute(
        """
        DELETE FROM device_state_processed_cycles
        WHERE snapshot_time < %(before)s
          AND snapshot_time < (
              SELECT snapshot_time FROM device_state_processed_cycles
              ORDER BY snapshot_time DESC
              OFFSET %(keep)s LIMIT 1
          )
        """,
        {"before": snapshot_time - ABSENT_LEDGER_RETENTION,
         "keep": ABSENT_LEDGER_KEEP_MIN - 1},
    )
    if not counts:
        log.warning(
            "device_state cycle=%s: eligible=%d against baseline=%s is below "
            "the %.0f%% floor; not counted as an observed cycle for absence",
            cycle_id, eligible_count, baseline, ABSENT_FLOOR_RATIO * 100,
        )
    return counts


def absence_window(cur, now: datetime, *, bounded: bool) -> AbsenceWindow | None:
    """The absence rule as of `now`, from the recent observed cycles.

    The observed cycles are the rows of `device_state_processed_cycles` that
    count as observations (sql/086): cycles this module committed, with a
    plausible payload. Not snapshot_metadata_core, whose rows can exist for
    cycles this module never processed (see WHAT COUNTS AS AN OBSERVED CYCLE
    in the module docstring).

    cutoff = the earlier of `now - ABSENT_STOP_AFTER` and the time of the
    ABSENT_MIN_MISSED_CYCLES-th most recent observed cycle. A vehicle last seen
    before it has been out of the feed long enough AND missed enough real
    cycles.

    since (bounded only) = the cutoff as it stood ABSENT_SWEEP_WINDOW_CYCLES
    observed cycles ago. Everything absent before that was already eligible
    for an earlier sweep.

    Returns None when fewer than ABSENT_MIN_MISSED_CYCLES observed cycles
    exist. Nothing can be judged absent then.

    Why the three failure cases are covered:
      * a failed sweep: the bounded window spans m observed cycles, so the
        next sweep still covers what it missed;
      * an ingest outage: no cycles, so nothing to miss; the first cycle back
        cannot count our downtime;
      * an updater-only failure (ingest healthy, this module failing): its
        cycles leave no ledger row, so the cutoff stays at the pre-failure
        cycles and every vehicle seen in the last processed cycle survives.
        After any length of failure the first processed cycle closes only
        what had missed k processed cycles, and stamps it with the vehicle's
        real last-observed time.
    An empty or implausibly small payload is processed but does not count,
    so during one the cutoff cannot advance past the last real cycles.
    """
    k, m = ABSENT_MIN_MISSED_CYCLES, ABSENT_SWEEP_WINDOW_CYCLES
    cur.execute(
        """
        SELECT snapshot_time FROM device_state_processed_cycles
        WHERE counts_as_observation AND snapshot_time <= %s
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
    # sql/086: whether this cycle counts as an observed cycle for the rule.
    counted_as_observation: bool | None = None
    # sql/087. rentals_failed_start: of this cycle's rentals_ended, how many
    # were failed starts (also included in failed_starts).
    # rentals_blip: released inside IN_PLACE_RADIUS_M with no rotation.
    # jitter_held: non-rental SAMPLES beyond the stationary threshold that are
    # not MOVED (the old rule called each of them MOVED). Counted per sample,
    # so a vehicle that sits 30 m off its stored position counts every cycle.
    rentals_failed_start: int = 0
    rentals_blip: int = 0
    jitter_held: int = 0


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
        # Nothing observable this cycle. Nothing upstream aborts a zero-device
        # cycle (src/cycle.py writes the core snapshot and calls this either
        # way), so it is handled here. It is recorded as processed but NOT as
        # an observation: an empty or plate-less payload is far more likely an
        # upstream or ingest glitch than the whole fleet leaving, so it must
        # never be the reason a stop closes (sql/086, ABSENT_FLOOR_RATIO). The
        # sweep still runs, and can close only vehicles that had already
        # missed ABSENT_MIN_MISSED_CYCLES real cycles before it, so a glitch
        # that outlasts a failed sweep or two still lets those through.
        with connection() as conn:
            with conn.cursor() as cur:
                stats.counted_as_observation = record_processed_cycle(
                    cur, cycle_id, snapshot_time, 0)
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
                       first_ever_observed_at, rental_started_at, last_observed_at,
                       rental_max_distance_m, rental_origin_device_id,
                       last_fix_lat, last_fix_lon
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
            in_place_release_updates: list[tuple] = []  # sql/087
            reopen_stops: list[tuple] = []   # (vid, closed_at, failed_start 0/1)

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
                        d.lat, d.lon,     # last_fix_lat/lon (sql/087)
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
                 _ever, prev_rental_started_at, prev_last_seen,
                 prev_rental_max, prev_origin_device_id,
                 prev_fix_lat, prev_fix_lon) = prior[vid]

                if prev_lat is None or prev_lon is None:
                    distance = float("inf")
                else:
                    distance = _distance_meters(
                        float(prev_lat), float(prev_lon), d.lat, d.lon
                    )
                # sql/087. Rental geometry (how far a rental got, where it
                # ended, whether it went nowhere) is measured from the last
                # fix before the rental, i.e. where the rider unlocked it,
                # not from the stored stop position: that one deliberately
                # does not follow drift (UNROTATED_MOVE_M) and can sit tens
                # of metres off. last_fix_* is frozen during a rental because
                # IN_RENTAL never writes it. NULL (rows from before sql/087)
                # falls back to the stored position.
                if prev_fix_lat is not None and prev_fix_lon is not None:
                    from_fix = _distance_meters(
                        float(prev_fix_lat), float(prev_fix_lon), d.lat, d.lon)
                else:
                    from_fix = distance

                # IN_RENTAL (sql/069) — see RENTALS in the module docstring.
                # Freeze: the stored position keeps pointing at the origin so
                # the release below is a single origin -> drop-point
                # comparison. `distance` is therefore origin -> here, and
                # sql/087 keeps its running maximum so the release can tell
                # a rental that went nowhere from a round trip.
                if d.is_reserved is True:
                    if prev_rental_started_at is None:
                        stats.rentals_started += 1
                        # The rider has it; dwell at the origin ends now.
                        close_history_ids.append(vid)
                        rental_start_updates.append((
                            snapshot_time,    # rental_started_at
                            d.device_id, d.spatial_status,
                            snapshot_time, str(cycle_id),
                            None if from_fix == float("inf") else from_fix,
                            prev_device_id,   # rental_origin_device_id
                            vid,
                        ))
                    else:
                        stats.rentals_held += 1
                        # NULL stays NULL: the origin is unknown (first seen
                        # mid-rental, or in a rental when sql/087 landed).
                        new_max = (
                            None if prev_rental_max is None or from_fix == float("inf")
                            else max(float(prev_rental_max), from_fix)
                        )
                        rental_hold_updates.append((
                            d.spatial_status, snapshot_time, str(cycle_id),
                            new_max, vid,
                        ))
                    continue

                released = prev_rental_started_at is not None
                if released:
                    stats.rentals_ended += 1
                    # sql/072, unchanged: every release is an observed rental,
                    # and a no-go is one that ended inside the stationary
                    # threshold of where it was unlocked. smart_ride_grade is
                    # calibrated on exactly this, so sql/087 does not redefine
                    # it (it only measures from the unlock fix, see from_fix).
                    no_go = from_fix <= threshold
                    rental_outcome_updates.append((1 if no_go else 0, vid))
                    if no_go:
                        stats.rentals_no_go += 1

                # IN-PLACE RELEASE (sql/087). The rental never left
                # IN_PLACE_RADIUS_M of its origin and ended inside it. The
                # vehicle did not go anywhere, so nothing about its stop
                # changes: same stored position, same dwell clock, the stop
                # the rental start closed is reopened, and no trip_events row.
                # With a rotated bike_id it was an attempt that failed: a
                # FAILED START. Without one it was a reservation blip and
                # counts for nothing.
                in_place = (
                    released
                    and prev_rental_max is not None
                    and float(prev_rental_max) <= IN_PLACE_RADIUS_M
                    and from_fix <= IN_PLACE_RADIUS_M
                )
                if in_place:
                    origin_device_id = prev_origin_device_id or prev_device_id
                    failed = d.device_id != origin_device_id
                    if failed:
                        stats.failed_starts += 1
                        stats.rentals_failed_start += 1
                    else:
                        stats.rentals_blip += 1
                    in_place_release_updates.append((
                        d.device_id, d.spatial_status, d.form_factor,
                        d.vehicle_use_type, d.vehicle_model_name,
                        d.vehicle_type_id,
                        1 if failed else 0,          # number_failed_starts +
                        1 if failed else None,       # recent_no_go_mask push
                        snapshot_time, str(cycle_id),
                        d.lat, d.lon,                # last_fix_lat/lon
                        vid,
                    ))
                    # A vehicle that also vanished for longer than the absence
                    # threshold mid-rental gets a fresh stop instead, exactly
                    # as REAPPEARANCE does: reopening would claim it was
                    # parked there throughout.
                    long_gone = (prev_last_seen is not None
                                 and prev_last_seen < snapshot_time - ABSENT_STOP_AFTER)
                    reopen_stops.append((
                        vid, None if long_gone else prev_rental_started_at,
                        1 if failed else 0,
                        (vid, d.vehicle_plate, str(cycle_id), snapshot_time,
                         d.lat, d.lon, d.spatial_status, d.form_factor,
                         d.device_id, 1 if failed else 0,
                         d.h3_8_index, d.h3_9_index, d.h3_10_index,
                         d.vehicle_use_type, d.vehicle_model_name),
                    ))
                    continue

                # A release that went somewhere, a release whose origin is
                # unknown (old path), or a non-rental relocation: past
                # JITTER_RADIUS_M with a rotated bike_id, or past
                # UNROTATED_MOVE_M without one.
                rotated = d.device_id != prev_device_id
                if (released
                        or distance > UNROTATED_MOVE_M
                        or (rotated and distance > JITTER_RADIUS_M)):
                    # MOVED — close prior stop, open a new one. This is a
                    # "successful trip" for popularity-tracking purposes
                    # (src/daily_trips.py).
                    #
                    # `released` (sql/069) enters here and is the ONLY way a
                    # rental produces a trip: prev_lat/lon were frozen at the
                    # origin, so `distance` is origin -> drop point and this
                    # fires exactly once. A release that lands back within the
                    # stationary threshold after going farther than
                    # IN_PLACE_RADIUS_M (a round trip) takes this branch for
                    # the history row and the dwell reset (the vehicle
                    # demonstrably left and came back), but records neither
                    # `moved` nor a trip_events row.
                    real_move = distance > threshold
                    if real_move:
                        stats.moved += 1
                    # sql/087: a relocation clears the failed-start count only
                    # if it covered FAILED_START_DECAY_M; a shorter one
                    # carries it. A release pushes "went somewhere" into the
                    # recent-rentals mask; a non-rental move pushes nothing.
                    clears = distance >= FAILED_START_DECAY_M
                    close_history_ids.append(vid)
                    moved_updates.append((
                        d.vehicle_plate, d.device_id, d.lat, d.lon,
                        d.spatial_status, d.form_factor,
                        snapshot_time,    # new first_observed_at_location
                        clears,           # number_failed_starts := 0 ?
                        0 if released else None,   # recent_no_go_mask push
                        snapshot_time,    # last_observed_at
                        str(cycle_id),
                        d.h3_8_index, d.h3_9_index, d.h3_10_index,
                        d.vehicle_use_type, d.vehicle_model_name,
                        d.vehicle_type_id,
                        d.lat, d.lon,     # last_fix_lat/lon
                        vid,
                    ))
                    new_history_rows.append((
                        vid, d.vehicle_plate, str(cycle_id), snapshot_time,
                        d.lat, d.lon, d.spatial_status, d.form_factor,
                        d.device_id,
                        # The count a short relocation carries belongs to the
                        # new stop too.
                        0 if clears else int(prev_fs or 0),
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
                    continue

                if distance > threshold:
                    # Beyond the stationary threshold but not MOVED, not a
                    # rental: what the 16 m rule used to call MOVED.
                    stats.jitter_held += 1

                if rotated:
                    # FAILED_START — not reserved, within JITTER_RADIUS_M, new
                    # bike_id (sql/087 widened this from the stationary
                    # threshold). We deliberately do NOT update the stored
                    # position or h3 cells: the scooter has not moved, and GPS
                    # drift would otherwise flip h3_10 on every failed start.
                    stats.failed_starts += 1
                    failed_start_updates.append((
                        d.device_id, d.spatial_status, d.form_factor,
                        d.vehicle_use_type, d.vehicle_model_name,
                        d.vehicle_type_id,
                        snapshot_time, str(cycle_id), d.lat, d.lon, vid,
                    ))
                else:
                    # STATIONARY — same bike_id, within UNROTATED_MOVE_M.
                    stats.stationary += 1
                    stationary_updates.append((
                        d.spatial_status, snapshot_time, str(cycle_id),
                        d.lat, d.lon, vid,
                    ))

                # Back in the feed where its stop was closed as absent
                # (sql/083; see REAPPEARANCE in the module docstring): a new
                # stop from now. The old stop is not reopened, and device_state
                # (dwell clock included) is updated exactly as the branch
                # above decided. A MOVED return never gets here, because it
                # opens its own stop.
                if vid in without_open_stop:
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
                        current_vehicle_type_id, rental_started_at,
                        last_fix_lat, last_fix_lon
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                        -- sql/087: cleared only by a relocation of at
                        -- least FAILED_START_DECAY_M; a shorter one carries
                        -- the count to the new spot.
                        number_failed_starts = CASE WHEN %s THEN 0
                                                    ELSE number_failed_starts END,
                        recent_no_go_mask = CASE WHEN %s::int IS NULL THEN recent_no_go_mask
                            ELSE (((recent_no_go_mask::int << 1) | %s::int) & {mask})::smallint END,
                        last_observed_at = %s,
                        last_cycle_id = %s,
                        current_h3_8_index = %s,
                        current_h3_9_index = %s,
                        current_h3_10_index = %s,
                        current_vehicle_use_type = %s,
                        current_vehicle_model_name = %s,
                        current_vehicle_type_id = %s,
                        last_fix_lat = %s,
                        last_fix_lon = %s,
                        -- sql/069 + sql/087: every release routes through
                        -- here or through the in-place release below, and
                        -- both clear all three rental columns, so they can
                        -- never outlive the rental they describe.
                        rental_started_at = NULL,
                        rental_max_distance_m = NULL,
                        rental_origin_device_id = NULL
                    WHERE vehicle_identifier = %s
                    """.format(mask=_RECENT_MASK),
                    # The mask push appears twice in the SQL (IS NULL test,
                    # then the value).
                    [u[:8] + (u[8],) + u[8:] for u in moved_updates],
                )

            # IN-PLACE RELEASE (sql/087). Position, h3 cells and
            # first_observed_at_location are deliberately untouched: the
            # vehicle never left, so neither did its dwell clock. The bike_id
            # is picked up so the next cycle compares like with like.
            if in_place_release_updates:
                cur.executemany(
                    """
                    UPDATE device_state SET
                        current_device_id = %s,
                        current_spatial_status = %s,
                        current_form_factor = %s,
                        current_vehicle_use_type = %s,
                        current_vehicle_model_name = %s,
                        current_vehicle_type_id = %s,
                        number_failed_starts = number_failed_starts + %s,
                        recent_no_go_mask = CASE WHEN %s::int IS NULL THEN recent_no_go_mask
                            ELSE (((recent_no_go_mask::int << 1) | %s::int) & {mask})::smallint END,
                        last_observed_at = %s,
                        last_cycle_id = %s,
                        last_fix_lat = %s,
                        last_fix_lon = %s,
                        rental_started_at = NULL,
                        rental_max_distance_m = NULL,
                        rental_origin_device_id = NULL
                    WHERE vehicle_identifier = %s
                    """.format(mask=_RECENT_MASK),
                    [u[:7] + (u[7],) + u[7:] for u in in_place_release_updates],
                )

            # IN_RENTAL, first cycle (sql/069). Deliberately does NOT touch
            # current_lat/current_lon/first_observed_at_location: freezing the
            # origin is the whole mechanism. device_id is picked up because
            # GBFS rotates bike_id per trip; the bike_id before the rental is
            # kept in rental_origin_device_id (sql/087) so the release can
            # still tell whether it rotated.
            if rental_start_updates:
                cur.executemany(
                    """
                    UPDATE device_state SET
                        rental_started_at = %s,
                        current_device_id = %s,
                        current_spatial_status = %s,
                        last_observed_at = %s,
                        last_cycle_id = %s,
                        rental_max_distance_m = %s,
                        rental_origin_device_id = %s
                    WHERE vehicle_identifier = %s
                    """,
                    rental_start_updates,
                )

            # sql/072. Separate from moved_updates because a release lands in
            # the MOVED branch or the in-place release, and the counters are
            # the same for both.
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

            # IN_RENTAL, every later cycle — liveness, plus how far from the
            # origin the rental has got (sql/087).
            if rental_hold_updates:
                cur.executemany(
                    """
                    UPDATE device_state SET
                        current_spatial_status = %s,
                        last_observed_at = %s,
                        last_cycle_id = %s,
                        rental_max_distance_m = %s
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
                        last_cycle_id = %s,
                        last_fix_lat = %s,
                        last_fix_lon = %s
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
                        last_cycle_id = %s,
                        last_fix_lat = %s,
                        last_fix_lon = %s
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

            # IN-PLACE RELEASE (sql/087): reopen the stop the rental's first
            # cycle closed (departed_at = rental_started_at, 'moved'), adding
            # the failed start to it. Only when the vehicle has no other open
            # stop. Anything not reopened (the stop was closed some other
            # way, or the vehicle vanished mid-rental for longer than
            # ABSENT_STOP_AFTER) gets a fresh stop from now instead, so the
            # vehicle is never left without one.
            if reopen_stops:
                reopenable = [r for r in reopen_stops if r[1] is not None]
                reopened: set[str] = set()
                if reopenable:
                    cur.execute(
                        """
                        UPDATE device_history h SET
                            departed_at = NULL,
                            departure_reason = NULL,
                            dwell_failed_starts = h.dwell_failed_starts + v.fs
                        FROM (
                            SELECT unnest(%s::text[]) AS vid,
                                   unnest(%s::timestamptz[]) AS closed_at,
                                   unnest(%s::int[]) AS fs
                        ) v
                        WHERE h.vehicle_identifier = v.vid
                          AND h.departed_at = v.closed_at
                          AND h.departure_reason = 'moved'
                          AND NOT EXISTS (
                              SELECT 1 FROM device_history o
                              WHERE o.vehicle_identifier = v.vid
                                AND o.departed_at IS NULL)
                        RETURNING h.vehicle_identifier
                        """,
                        ([r[0] for r in reopenable], [r[1] for r in reopenable],
                         [r[2] for r in reopenable]),
                    )
                    reopened = {row[0] for row in (cur.fetchall() or [])}
                new_history_rows.extend(r[3] for r in reopen_stops if r[0] not in reopened)

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
            # this and the preceding cycles can. The ledger row goes in first
            # (sql/086): this cycle is one of the observed cycles the window
            # counts, and the row commits or rolls back with everything above.
            stats.counted_as_observation = record_processed_cycle(
                cur, cycle_id, snapshot_time, len(eligible))
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
        "skipped=%d jitter_held=%d rentals(started=%d held=%d ended=%d no_go=%d "
        "failed_start=%d blip=%d) stops(closed_absent=%d reopened=%d)",
        cycle_id, stats.new_devices, stats.moved, stats.failed_starts,
        stats.stationary, stats.skipped_no_identifier, stats.jitter_held,
        stats.rentals_started, stats.rentals_held, stats.rentals_ended,
        stats.rentals_no_go, stats.rentals_failed_start, stats.rentals_blip,
        stats.stops_closed_absent, stats.stops_reopened,
    )
    return stats
