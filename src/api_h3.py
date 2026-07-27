"""Per-cell H3 aggregate layers for analysis-mode choropleths.

GET /api/v1/h3/aggregates?res=8|9|10

The frontend wants hex layers colored by per-cell attributes without
aggregating 8k device points client-side on every refresh. Everything
here is derived from the most recent completed cycle (plus the trailing
24h of trip_events), so the response only changes when a new cycle lands
— it carries a cycle-keyed ETag and a ~10-minute CDN cache header.

Cell keys are canonical h3 STRINGS (e.g. "8928308280fffff"), never raw
64-bit integers: the ints exceed JS MAX_SAFE_INTEGER and silently lose
precision in JSON.parse.

Per-cell attributes:
    device_count         devices (denver_core) currently parked in the cell
    trips_started_24h    trip_events whose FROM-position falls in the cell,
                         trailing 24h ending at snapshot_time. A "start" is
                         the state tracker observing a device leave its spot
                         (the same MOVED transition that resets dwell);
                         failed starts are tracked separately.
    starts_per_hour_peak max trips started in any single UTC clock hour
                         within that window (usage heat)
    avg_battery_percent  mean battery_percent of devices in the cell that
                         have one; null when none do
    risk_share           fraction of the cell's devices with
                         reliability_tier == "high_risk" (same formula as
                         /api/v1/devices/current, dwell outliers included);
                         null for trip-only cells with no parked devices
    avg_dwell_hours      mean dwell of the cell's state-tracked devices;
                         null when none are tracked
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import h3
from fastapi import APIRouter, HTTPException, Query, Request, Response

from .api_frontend_reports import reliability_report_type_sql
from .api_public import _if_none_match_hit
from .dwell_stats import stats_for_cycle
from .pg import connection
from .quality import (
    compute_battery_percent,
    compute_quality_designation,
    compute_reliability_tier,
)

router = APIRouter()

_CACHE_HEADER = "public, max-age=600"


class _CellAccum:
    __slots__ = ("devices", "high_risk", "battery_sum", "battery_n",
                 "dwell_sum", "dwell_n", "trips", "hourly")

    def __init__(self) -> None:
        self.devices = 0
        self.high_risk = 0
        self.battery_sum = 0
        self.battery_n = 0
        self.dwell_sum = 0.0
        self.dwell_n = 0
        self.trips = 0
        self.hourly: dict[datetime, int] = {}


@router.get("/api/v1/h3/aggregates")
def h3_aggregates(
    request: Request,
    response: Response,
    res: int = Query(..., ge=8, le=10, description="H3 resolution: 8, 9, or 10"),
) -> Any:
    """Per-cell aggregates at the requested H3 resolution."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cycle_id, snapshot_time
                FROM observation_cycles oc
                JOIN snapshot_metadata_core USING (cycle_id)
                WHERE oc.job_status = 'complete'
                ORDER BY snapshot_time DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(503, detail="no completed cycles yet")
            cycle_id, snapshot_time = row[0], row[1]

            etag = f'W/"h3agg:{res}:{cycle_id}"'
            if _if_none_match_hit(request, etag):
                return Response(
                    status_code=304,
                    headers={"ETag": etag, "Cache-Control": _CACHE_HEADER},
                )
            response.headers["ETag"] = etag
            response.headers["Cache-Control"] = _CACHE_HEADER

            # res is validated to 8..10 above, so the interpolated column
            # name is one of h3_8_index / h3_9_index / h3_10_index.
            cur.execute(
                f"""
                SELECT r.h3_{res}_index, r.vehicle_identifier,
                       r.is_disabled, r.is_reserved,
                       r.current_range_meters, r.max_range_meters_for_type,
                       ds.number_failed_starts, ds.first_observed_at_location,
                       (EXISTS (
                           SELECT 1 FROM negative_reports nr
                           WHERE nr.vehicle_identifier = r.vehicle_identifier
                             AND nr.h3_10_index = r.h3_10_index
                             AND nr.reported_at > %(snap)s - INTERVAL '24 hours'
                             AND nr.reported_at <= %(snap)s
                       ) OR EXISTS (
                           SELECT 1 FROM device_reports dr
                           WHERE dr.vehicle_identifier = r.vehicle_identifier
                             AND dr.h3_10_index = r.h3_10_index
                             AND dr.reported_at > %(snap)s - INTERVAL '24 hours'
                             AND dr.reported_at <= %(snap)s
                             AND """ + reliability_report_type_sql("dr") + """
                       )) AS has_negative_report
                FROM raw_telemetry_points r
                LEFT JOIN device_state ds USING (vehicle_identifier)
                WHERE r.cycle_id = %(cycle)s
                  AND r.spatial_status = 'denver_core'
                """,
                {"cycle": cycle_id, "snap": snapshot_time},
            )
            device_rows = cur.fetchall()

            # Trailing-24h trip starts, anchored at snapshot_time so the
            # payload is fully determined by the cycle (ETag-safe).
            cur.execute(
                """
                SELECT detected_at, from_lat, from_lon
                FROM trip_events
                WHERE detected_at > %s - INTERVAL '24 hours'
                  AND detected_at <= %s
                  AND from_lat IS NOT NULL
                  AND from_lon IS NOT NULL
                """,
                (snapshot_time, snapshot_time),
            )
            trip_rows = cur.fetchall()

    # Everything below is anchored to snapshot_time (reports, dwell,
    # reliability), so the whole payload is a pure function of the cycle —
    # which is what the cycle-keyed ETag promises. Nothing here reads the
    # wall clock.
    dwell_stats = stats_for_cycle(cycle_id, snapshot_time)

    cells: dict[str, _CellAccum] = {}

    def _cell(key: str) -> _CellAccum:
        acc = cells.get(key)
        if acc is None:
            acc = cells[key] = _CellAccum()
        return acc

    for (h3_idx, vid, is_disabled, is_reserved, range_m, max_range_m,
         failed_starts, first_obs, has_neg) in device_rows:
        if h3_idx is None:
            continue
        acc = _cell(h3.int_to_str(int(h3_idx)))
        acc.devices += 1

        fs = int(failed_starts) if failed_starts is not None else None
        dstat = dwell_stats.get(vid)
        is_outlier = bool(dstat and dstat.is_outlier)
        quality = compute_quality_designation(
            current_range_meters=range_m,
            is_disabled=is_disabled,
            is_reserved=is_reserved,
            number_failed_starts=fs,
            first_observed_at_location=first_obs,
            has_negative_report=bool(has_neg),
            is_dwell_outlier=is_outlier,
            now=snapshot_time,
        )
        tier = compute_reliability_tier(
            number_failed_starts=fs,
            first_observed_at_location=first_obs,
            quality_designation=quality,
            has_negative_report=bool(has_neg),
            is_dwell_outlier=is_outlier,
            peer_median_dwell_hours=dstat.peer_median_hours if dstat else None,
            now=snapshot_time,
        )
        if tier == "high_risk":
            acc.high_risk += 1

        battery = compute_battery_percent(range_m)
        if battery is not None:
            acc.battery_sum += battery
            acc.battery_n += 1

        if first_obs is not None:
            acc.dwell_sum += (snapshot_time - first_obs).total_seconds() / 3600.0
            acc.dwell_n += 1

    for detected_at, from_lat, from_lon in trip_rows:
        acc = _cell(h3.latlng_to_cell(float(from_lat), float(from_lon), res))
        acc.trips += 1
        hour = detected_at.replace(minute=0, second=0, microsecond=0)
        acc.hourly[hour] = acc.hourly.get(hour, 0) + 1

    return {
        "res": res,
        "cycle_id": str(cycle_id),
        "snapshot_time": snapshot_time.isoformat(),
        "cells": {
            key: {
                "device_count": acc.devices,
                "trips_started_24h": acc.trips,
                "starts_per_hour_peak": max(acc.hourly.values(), default=0),
                "avg_battery_percent": (
                    round(acc.battery_sum / acc.battery_n) if acc.battery_n else None
                ),
                "risk_share": (
                    round(acc.high_risk / acc.devices, 2) if acc.devices else None
                ),
                "avg_dwell_hours": (
                    round(acc.dwell_sum / acc.dwell_n, 1) if acc.dwell_n else None
                ),
            }
            for key, acc in cells.items()
        },
    }
