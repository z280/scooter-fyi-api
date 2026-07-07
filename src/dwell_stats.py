"""Per-cycle dwell peer statistics, computed once and cached in-process.

The peer-relative dwell-outlier math (src/quality.py
`compute_dwell_peer_stats`) needs the WHOLE denver_core fleet — a
bbox- or form_factor-filtered request must still judge each device
against all of its geographic neighbors, not just the ones that
happened to match the filter. So the inputs come from their own lean
query here, independent of whatever filter the calling endpoint applies,
and the result is cached per cycle_id (new cycle → new stats, ~every
10 minutes).

Dwell values are anchored at the cycle's ``snapshot_time`` (NOT wall-clock
now), so the result is a pure function of the cycle: every caller in the
cycle sees identical stats, which is what lets the h3-aggregate ETag treat
the payload as cycle-deterministic. The anchor is at most one cycle length
(~10 min) behind real time — negligible for a dwell metric measured in
hours, and worth it for cache-correctness.

Peer population: state-tracked (has a vehicle_identifier + dwell clock),
spatial_status='denver_core' devices of every form factor — dwell is a
statement about a *location's* turnover, so bikes and scooters are peers
of each other.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from datetime import datetime

from .pg import connection
from .quality import DwellPeerStats, compute_dwell_peer_stats

# cycle_id (str) -> {vehicle_identifier: DwellPeerStats}. Two entries is
# enough to ride out a cycle rollover without thrashing.
_CACHE_MAX = 2
_cache: OrderedDict[str, dict[str, DwellPeerStats]] = OrderedDict()


def stats_for_cycle(
    cycle_id: uuid.UUID | str, snapshot_time: datetime
) -> dict[str, DwellPeerStats]:
    """Dwell peer stats keyed by vehicle_identifier for one cycle.

    Dwell is measured as of ``snapshot_time`` (a fixed function of the
    cycle), so the result is deterministic per cycle. ``snapshot_time`` is
    only consulted on a cache miss; the cache key is the cycle alone.
    """
    key = str(cycle_id)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.vehicle_identifier, r.h3_9_index,
                       ds.first_observed_at_location
                FROM raw_telemetry_points r
                JOIN device_state ds USING (vehicle_identifier)
                WHERE r.cycle_id = %s
                  AND r.spatial_status = 'denver_core'
                """,
                (key,),
            )
            rows = cur.fetchall()

    entries = [
        (
            vid,
            int(h3_9) if h3_9 is not None else None,
            (snapshot_time - first_obs).total_seconds() / 3600.0 if first_obs is not None else None,
        )
        for vid, h3_9, first_obs in rows
    ]
    stats = compute_dwell_peer_stats(entries)

    _cache[key] = stats
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return stats
