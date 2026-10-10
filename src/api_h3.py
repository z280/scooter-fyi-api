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

import logging
from datetime import datetime
from typing import Any

import h3
from fastapi import APIRouter, HTTPException, Query, Request, Response

from . import fleet_reports, payload_cache
from .api_public import _if_none_match_hit
from .dwell_stats import stats_for_cycle
from .pg import connection
from .quality import (
    compute_battery_percent,
    compute_quality_designation,
    compute_reliability_tier,
    recent_rentals_no_go,
)

log = logging.getLogger(__name__)

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


def _negative_states_at(cycle_id, snapshot_time):
    """The single report-state pass for /h3, on its OWN connection (a failure
    must not abort the main read's transaction) and guarded: None on failure,
    which the caller turns into "unknown" for every vehicle rather than a 500,
    the same posture as /devices/current's _negative_states."""
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                return fleet_reports.negative_states(
                    cur, cycle_id, snapshot_time=snapshot_time,
                    where="AND r.spatial_status = 'denver_core'")
    except Exception:  # noqa: BLE001
        log.warning("h3: negative-report states unavailable — every vehicle reads unknown this request")
        return None


_RESOLUTIONS = (8, 9, 10)


def _latest_cycle() -> tuple[Any, datetime]:
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
    return row[0], row[1]


def _build_payloads(cycle_id, snapshot_time) -> dict[int, dict[str, Any]]:
    """The /h3 payload for EVERY resolution from one pass: the device read,
    the report-state pass, dwell stats and the per-vehicle reliability tier
    do not depend on the resolution — only the cell a vehicle lands in does —
    so building all three together costs about what one used to."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.h3_8_index, r.h3_9_index, r.h3_10_index,
                       r.vehicle_identifier,
                       r.is_disabled, r.is_reserved,
                       r.current_range_meters, r.max_range_meters_for_type,
                       ds.number_failed_starts, ds.first_observed_at_location,
                       ds.recent_no_go_mask
                FROM raw_telemetry_points r
                LEFT JOIN device_state ds USING (vehicle_identifier)
                WHERE r.cycle_id = %(cycle)s
                  AND r.spatial_status = 'denver_core'
                """,
                {"cycle": cycle_id},
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
    negative_by = _negative_states_at(cycle_id, snapshot_time)

    cells: dict[int, dict[str, _CellAccum]] = {res: {} for res in _RESOLUTIONS}

    def _cell(res: int, key: str) -> _CellAccum:
        acc = cells[res].get(key)
        if acc is None:
            acc = cells[res][key] = _CellAccum()
        return acc

    for (h3_8, h3_9, h3_10, vid, is_disabled, is_reserved, range_m, max_range_m,
         failed_starts, first_obs, recent_mask) in device_rows:
        if negative_by is None:
            # The pass failed: we cannot say any vehicle is unreported, and a
            # reported scooter must never read likely-rideable (owner rule).
            has_neg = "unknown"
        else:
            entry = negative_by.get(vid)
            has_neg = (None if not entry else
                       "high" if entry.get("risk") == fleet_reports.RISK_HIGH else "unknown")

        fs = int(failed_starts) if failed_starts is not None else None
        dstat = dwell_stats.get(vid)
        is_outlier = bool(dstat and dstat.is_outlier)
        quality = compute_quality_designation(
            current_range_meters=range_m,
            is_disabled=is_disabled,
            is_reserved=is_reserved,
            number_failed_starts=fs,
            first_observed_at_location=first_obs,
            has_negative_report=has_neg == "high",
            is_dwell_outlier=is_outlier,
            now=snapshot_time,
        )
        battery = compute_battery_percent(range_m)
        tier = compute_reliability_tier(
            number_failed_starts=fs,
            first_observed_at_location=first_obs,
            quality_designation=quality,
            has_negative_report=has_neg == "high",
            has_faded_negative_report=has_neg == "unknown",
            is_dwell_outlier=is_outlier,
            peer_median_dwell_hours=dstat.peer_median_hours if dstat else None,
            battery_percent=battery,
            now=snapshot_time,
            recent_rentals_no_go=recent_rentals_no_go(recent_mask),
        )
        dwell_h = ((snapshot_time - first_obs).total_seconds() / 3600.0
                   if first_obs is not None else None)

        for res, h3_idx in zip(_RESOLUTIONS, (h3_8, h3_9, h3_10)):
            if h3_idx is None:
                continue
            acc = _cell(res, h3.int_to_str(int(h3_idx)))
            acc.devices += 1
            if tier == "high_risk":
                acc.high_risk += 1
            if battery is not None:
                acc.battery_sum += battery
                acc.battery_n += 1
            if dwell_h is not None:
                acc.dwell_sum += dwell_h
                acc.dwell_n += 1

    for detected_at, from_lat, from_lon in trip_rows:
        hour = detected_at.replace(minute=0, second=0, microsecond=0)
        for res in _RESOLUTIONS:
            acc = _cell(res, h3.latlng_to_cell(float(from_lat), float(from_lon), res))
            acc.trips += 1
            acc.hourly[hour] = acc.hourly.get(hour, 0) + 1

    return {
        res: {
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
                for key, acc in cells[res].items()
            },
        }
        for res in _RESOLUTIONS
    }


def _key(res: int) -> str:
    return f"h3|{res}"


def _ensure_cycle(cycle_id, snapshot_time, want: int) -> payload_cache.Entry:
    """The cached entry for `want` at this cycle. A build makes all three
    resolutions, so the other two are stored alongside rather than rebuilt
    when a rider switches cell size."""
    built: dict[int, payload_cache.Entry] = {}

    def _build_all() -> None:
        for res, body in _build_payloads(cycle_id, snapshot_time).items():
            built[res] = payload_cache.make_entry(
                _key(res), cycle_id, "", payload_cache.dumps(body))

    def _build(res: int):
        def _inner() -> payload_cache.Entry:
            if not built:
                _build_all()
            return built[res]
        return _inner

    entry = payload_cache.get_or_build(_key(want), cycle_id, "", _build(want),
                                       lock_key="h3")
    if built:
        for res in _RESOLUTIONS:
            if res != want:
                payload_cache.get_or_build(_key(res), cycle_id, "", _build(res),
                                           lock_key="h3")
    return entry


def warm() -> None:
    """Warmer hook (payload_cache.register_warmer): keep all three
    resolutions current. Two indexed queries when they already are."""
    cycle_id, snapshot_time = _latest_cycle()
    entry = payload_cache.peek(_key(9))
    if entry is not None and entry.fresh_for(cycle_id, ""):
        return
    _ensure_cycle(cycle_id, snapshot_time, 9)


payload_cache.register_warmer(warm)


@router.get("/api/v1/h3/aggregates")
def h3_aggregates(
    request: Request,
    response: Response,
    res: int = Query(..., ge=8, le=10, description="H3 resolution: 8, 9, or 10"),
) -> Any:
    """Per-cell aggregates at the requested H3 resolution.

    Served from the precomputed cache (src/payload_cache.py): built once per
    cycle, gzip-compressed once, so a request is a lookup, not a rebuild."""
    cycle_id, snapshot_time = _latest_cycle()

    etag = f'W/"h3agg:{res}:{cycle_id}"'
    if _if_none_match_hit(request, etag):
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": _CACHE_HEADER},
        )

    entry = _ensure_cycle(cycle_id, snapshot_time, res)
    # A build in progress may hand back the previous cycle's entry; its tag
    # then names that cycle, so the next poll fetches the new one.
    etag = f'W/"h3agg:{res}:{entry.cycle_id}"'
    body, enc_headers = payload_cache.gzip_response_body(
        payload_cache.assemble(entry), request.headers.get("accept-encoding"))
    return Response(
        content=body,
        media_type="application/json",
        headers={"ETag": etag, "Cache-Control": _CACHE_HEADER, **enc_headers},
    )
