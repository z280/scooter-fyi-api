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
  watching  -> left_feed  vehicle_identifier ABSENT from this cycle's
                           payload (GBFS omits vehicles while checked out).
  left_feed -> resolved   vehicle_identifier PRESENT again. Records the
                           observed lat/lon/battery on tracked_rides as
                           the GBFS-side end signal, independent of any
                           user report (sql/027_tracked_rides.sql).

"Removed from the feed at its present location" is read as simply
"absent from this cycle's device list" — GBFS gives no location for an
absent device to compare against, so there's nothing else to check.
Reappearance is recorded unconditionally (no "must differ from start
location" gate) — anti-abuse filtering on that data belongs to the points
system, not this detection layer.

A watch that expires without ever resolving is left alone here —
src/cli.py:expire_stale_watches() closes those out on its own cadence.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .ingest import TaggedDevice
from .pg import connection
from .quality import compute_battery_percent

log = logging.getLogger(__name__)


@dataclass
class WatchUpdateStats:
    open_watches: int = 0
    newly_left_feed: int = 0
    newly_reappeared: int = 0


def _classify(
    watch_rows: list[tuple[int, uuid.UUID, str, str]],  # (watch_id, tracked_ride_id, vehicle_identifier, status)
    observed: dict[str, TaggedDevice],
) -> tuple[list[tuple[int, uuid.UUID]], list[tuple[int, uuid.UUID, TaggedDevice]]]:
    """Pure partitioning, no DB/IO — unit-testable without a fake cursor."""
    newly_left: list[tuple[int, uuid.UUID]] = []
    newly_reappeared: list[tuple[int, uuid.UUID, TaggedDevice]] = []
    for watch_id, tracked_ride_id, vehicle_identifier, status in watch_rows:
        device = observed.get(vehicle_identifier)
        if status == "watching" and device is None:
            newly_left.append((watch_id, tracked_ride_id))
        elif status == "left_feed" and device is not None:
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

    log.info(
        "ride_watch cycle=%s: open=%d left_feed=%d reappeared=%d",
        cycle_id, stats.open_watches, stats.newly_left_feed, stats.newly_reappeared,
    )
    return stats
