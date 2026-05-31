"""Per-scooter persistent state + history maintenance.

Called once per ingest cycle (after compute, before transmit). Reads the
current `device_state` rows for any vehicle_identifier we observed this
cycle, applies a four-way branch per device, and writes back updated state
+ optional history rows.

The four branches:
  * NEW              — never seen this identifier before. Insert state row,
                       insert first history row. Counter starts at 0.
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

Distance is computed flat-earth using the device's own latitude as the
local longitude scale. At Denver's ~40° latitude over 16m distances the
approximation error is < 1cm — irrelevant.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .config import load
from .ingest import TaggedDevice
from .pg import connection

log = logging.getLogger(__name__)


# Earth's radius is not needed — at the scales we care about (single-digit
# meters), a degree of latitude is 111,320 m and a degree of longitude is
# 111,320 × cos(lat) m. We use the cosine of the device's own latitude as
# the local east-west scale.
_METERS_PER_DEG_LAT = 111_320.0


def _distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Flat-earth distance, accurate enough for sub-100m comparisons at
    Denver's latitude."""
    avg_lat_rad = math.radians((lat1 + lat2) / 2.0)
    meters_per_deg_lon = _METERS_PER_DEG_LAT * math.cos(avg_lat_rad)
    dy = (lat2 - lat1) * _METERS_PER_DEG_LAT
    dx = (lon2 - lon1) * meters_per_deg_lon
    return math.sqrt(dx * dx + dy * dy)


@dataclass
class StateUpdateStats:
    new_devices: int = 0
    moved: int = 0
    failed_starts: int = 0
    stationary: int = 0
    skipped_no_identifier: int = 0


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
                       first_ever_observed_at
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

            for d in eligible:
                vid = d.vehicle_identifier
                if vid not in prior:
                    # NEW device — first observation ever
                    stats.new_devices += 1
                    new_state_rows.append((
                        vid, d.vehicle_plate, d.device_id, d.lat, d.lon,
                        d.spatial_status, d.form_factor,
                        snapshot_time,    # first_observed_at_location
                        0,                # number_failed_starts
                        snapshot_time,    # first_ever_observed_at
                        snapshot_time,    # last_observed_at
                        str(cycle_id),
                        d.h3_8_index, d.h3_9_index, d.h3_10_index,
                    ))
                    new_history_rows.append((
                        vid, d.vehicle_plate, str(cycle_id), snapshot_time,
                        d.lat, d.lon, d.spatial_status, d.form_factor,
                        d.device_id, 0,
                        d.h3_8_index, d.h3_9_index, d.h3_10_index,
                    ))
                    continue

                prev_device_id, prev_lat, prev_lon, prev_first_seen, prev_fs, _ever = prior[vid]
                if prev_lat is None or prev_lon is None:
                    distance = float("inf")
                else:
                    distance = _distance_meters(
                        float(prev_lat), float(prev_lon), d.lat, d.lon
                    )

                if distance > threshold:
                    # MOVED — close prior stop, open a new one
                    stats.moved += 1
                    close_history_ids.append(vid)
                    moved_updates.append((
                        d.vehicle_plate, d.device_id, d.lat, d.lon,
                        d.spatial_status, d.form_factor,
                        snapshot_time,    # new first_observed_at_location
                        snapshot_time,    # last_observed_at
                        str(cycle_id),
                        d.h3_8_index, d.h3_9_index, d.h3_10_index,
                        vid,
                    ))
                    new_history_rows.append((
                        vid, d.vehicle_plate, str(cycle_id), snapshot_time,
                        d.lat, d.lon, d.spatial_status, d.form_factor,
                        d.device_id, 0,
                        d.h3_8_index, d.h3_9_index, d.h3_10_index,
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
                        current_h3_8_index, current_h3_9_index, current_h3_10_index
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    new_state_rows,
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
                        current_h3_10_index = %s
                    WHERE vehicle_identifier = %s
                    """,
                    moved_updates,
                )

            if failed_start_updates:
                cur.executemany(
                    """
                    UPDATE device_state SET
                        current_device_id = %s,
                        current_spatial_status = %s,
                        current_form_factor = %s,
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
                        h3_8_index, h3_9_index, h3_10_index
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    new_history_rows,
                )

        conn.commit()

    log.info(
        "device_state cycle=%s: new=%d moved=%d failed_starts=%d stationary=%d skipped=%d",
        cycle_id, stats.new_devices, stats.moved, stats.failed_starts,
        stats.stationary, stats.skipped_no_identifier,
    )
    return stats
