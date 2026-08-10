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

Distance is computed flat-earth using the device's own latitude as the
local longitude scale. At Denver's ~40° latitude over 16m distances the
approximation error is < 1cm — irrelevant.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from . import device_features
from .config import load
from .geo import distance_meters as _distance_meters
from .ingest import TaggedDevice
from .pg import connection

log = logging.getLogger(__name__)


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
        return stats

    with connection() as conn:
        with conn.cursor() as cur:
            # Pull existing state for everything we saw, in one round-trip.
            ids = [d.vehicle_identifier for d in eligible]
            cur.execute(
                """
                SELECT vehicle_identifier, current_device_id, current_lat, current_lon,
                       first_observed_at_location, number_failed_starts,
                       first_ever_observed_at, rental_started_at
                FROM device_state
                WHERE vehicle_identifier = ANY(%s)
                FOR UPDATE
                """,
                (ids,),
            )
            prior: dict[str, tuple] = {row[0]: row[1:] for row in cur.fetchall()}

            new_state_rows: list[tuple] = []
            moved_updates: list[tuple] = []
            failed_start_updates: list[tuple] = []
            stationary_updates: list[tuple] = []
            new_history_rows: list[tuple] = []
            close_history_ids: list[str] = []
            trip_event_rows: list[tuple] = []
            rental_start_updates: list[tuple] = []   # sql/069
            rental_hold_updates: list[tuple] = []

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
                 _ever, prev_rental_started_at) = prior[vid]

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

            # Close out history rows for devices that just moved. The "open
            # stop" is the most recent row with departed_at IS NULL.
            if close_history_ids:
                cur.execute(
                    """
                    UPDATE device_history SET departed_at = %s
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

            # Catalog-standard equipment for devices that just gained (or
            # just arrived with) a model that ships with it — e.g. every
            # Rover's cargo basket. After the model-name writes above so a
            # device NEW this cycle is seeded this cycle, not next.
            device_features.seed_catalog_features(cur)

        conn.commit()

    log.info(
        "device_state cycle=%s: new=%d moved=%d failed_starts=%d stationary=%d "
        "skipped=%d rentals(started=%d held=%d ended=%d)",
        cycle_id, stats.new_devices, stats.moved, stats.failed_starts,
        stats.stationary, stats.skipped_no_identifier,
        stats.rentals_started, stats.rentals_held, stats.rentals_ended,
    )
    return stats
