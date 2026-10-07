"""Fleet analytics endpoints (docs/PLAN_FLEET_ANALYTICS.md).

Public, read-only, cached. Every response states its window, its time zone,
its granularity, its region, its sample and what its numbers mean; a chart
that drops any of those is not reporting, it is asserting.

  GET /api/v1/analytics/rides              rides by bucket x model (rollup)
  GET /api/v1/analytics/failed-starts      failed starts by bucket x model (rollup)
  GET /api/v1/analytics/devices-by-region  vehicles on the map per region
  GET /api/v1/analytics/equity-compliance  % of fleet in Equity Areas by bucket
  GET /api/v1/analytics/dwell              average dwell per region x model (rollup)
  GET /api/v1/analytics/fleet-status       available / in use / out of service / off-map
  GET /api/v1/analytics/fleet-counts       devices visible now; ever seen, by model
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Response

from . import geo
from .analytics_rollups import REGION_LAYERS
from .pg import connection

router = APIRouter()

TZ = "America/Denver"
GRANULARITIES = ("hour", "day", "week", "month")
REGION_TYPES = ("city",) + REGION_LAYERS
#: Hourly buckets are capped so a payload stays a chart, not a dump.
MAX_DAYS = {"hour": 31, "day": 366, "week": 366, "month": 366}
#: Exhibit B — Equity Area Deployment (src/daily_sla.py COMPLIANCE_THRESHOLD).
EQUITY_THRESHOLD_PCT = 30.0
#: An average dwell over fewer stops than this is shown as counts only.
MIN_DWELLS_FOR_AVERAGE = 30
#: The failed-start counter's known under-reporting begins here
#: (in-place releases + GPS jitter; fix in progress).
FAILED_STARTS_UNDERCOUNT_SINCE = "2026-08-10"
CACHE_SECONDS = 300

_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _cached(key: tuple, build: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    value = build()
    with _cache_lock:
        if len(_cache) > 512:
            _cache.clear()
        _cache[key] = (now, value)
    return value


def _window(days: int, granularity: str) -> tuple[datetime, datetime]:
    if granularity not in GRANULARITIES:
        raise HTTPException(400, f"granularity must be one of {list(GRANULARITIES)}")
    if not 1 <= days <= MAX_DAYS[granularity]:
        raise HTTPException(400, f"days must be 1-{MAX_DAYS[granularity]} for granularity '{granularity}'")
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return end - timedelta(days=days), end


def _region(region_type: str, region_name: str | None) -> tuple[str, str]:
    if region_type not in REGION_TYPES:
        raise HTTPException(400, f"region_type must be one of {list(REGION_TYPES)}")
    if region_type == "city":
        return "city", "Denver"
    if not region_name:
        raise HTTPException(400, "region_name is required for a region_type other than city")
    try:
        names = geo.region_names(region_type)
    except KeyError:
        raise HTTPException(404, f"unknown layer '{region_type}'")
    if region_name not in names:
        raise HTTPException(404, f"unknown {region_type} '{region_name}'")
    return region_type, region_name


def _meta(start: datetime, end: datetime, granularity: str | None, **extra: Any) -> dict[str, Any]:
    out = {"window_start": start.isoformat(), "window_end": end.isoformat(), "timezone": TZ}
    if granularity:
        out["granularity"] = granularity
    out.update(extra)
    return out


def _bucket_iso(local_naive: datetime) -> str:
    """A Denver-local bucket start, as an ISO string WITH its offset."""
    from zoneinfo import ZoneInfo
    return local_naive.replace(tzinfo=ZoneInfo(TZ)).isoformat()


def _rollup_series(table: str, value_cols: tuple[str, ...], rt: str, rn: str,
                   start: datetime, end: datetime, granularity: str) -> tuple[list, list, dict]:
    sums = ", ".join(f"SUM({c})" for c in value_cols)
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT date_trunc(%s, hour AT TIME ZONE %s) AS b, model, {sums}
            FROM {table}
            WHERE region_type = %s AND region_name = %s AND hour >= %s AND hour < %s
            GROUP BY b, model ORDER BY b
            """,
            (granularity, TZ, rt, rn, start, end),
        )
        rows = cur.fetchall()
        cur.execute(f"SELECT MAX(hour) FROM {table}")
        through = cur.fetchone()[0]
    buckets: dict[datetime, dict[str, list[int]]] = {}
    models: set[str] = set()
    totals = [0] * len(value_cols)
    for b, model, *vals in rows:
        models.add(model)
        buckets.setdefault(b, {})[model] = [int(v or 0) for v in vals]
        for i, v in enumerate(vals):
            totals[i] += int(v or 0)
    series = []
    for b in sorted(buckets):
        per = buckets[b]
        series.append({
            "bucket": _bucket_iso(b),
            "by_model": {m: v[0] for m, v in sorted(per.items())},
            "total": sum(v[0] for v in per.values()),
            **({value_cols[1]: sum(v[1] for v in per.values())} if len(value_cols) > 1 else {}),
        })
    return series, sorted(models), {
        "totals": dict(zip(value_cols, totals)),
        "data_through": (through + timedelta(hours=1)).isoformat() if through else None,
    }


@router.get("/api/v1/analytics/rides")
def analytics_rides(
    response: Response,
    days: int = Query(30), granularity: str = Query("day"),
    region_type: str = Query("city"), region_name: str | None = Query(None),
) -> dict[str, Any]:
    """Rides per bucket, by device model, for the city or one region."""
    start, end = _window(days, granularity)
    rt, rn = _region(region_type, region_name)

    def build():
        series, models, extra = _rollup_series(
            "analytics_rides_hourly", ("rides",), rt, rn, start, end, granularity)
        return {
            **_meta(start, end, granularity, region={"type": rt, "name": rn}),
            "models": models, "series": series, "rides": extra["totals"]["rides"],
            "data_through": extra["data_through"],
            "definition": "A ride is a vehicle that moved from one stop to another (trip_events), "
                          "placed by where it started and counted when the move was detected.",
        }

    response.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
    return _cached(("rides", days, granularity, rt, rn, end), build)


@router.get("/api/v1/analytics/failed-starts")
def analytics_failed_starts(
    response: Response,
    days: int = Query(30), granularity: str = Query("day"),
    region_type: str = Query("city"), region_name: str | None = Query(None),
) -> dict[str, Any]:
    """Failed starts per bucket, by device model, for the city or one region."""
    start, end = _window(days, granularity)
    rt, rn = _region(region_type, region_name)

    def build():
        series, models, extra = _rollup_series(
            "analytics_failed_starts_hourly", ("failed_starts", "stops_with_failures"),
            rt, rn, start, end, granularity)
        return {
            **_meta(start, end, granularity, region={"type": rt, "name": rn}),
            "models": models, "series": series,
            "failed_starts": extra["totals"]["failed_starts"],
            "stops_with_failures": extra["totals"]["stops_with_failures"],
            "data_through": extra["data_through"],
            "definition": "A failed start is a rental that ended where it began, counted when the "
                          "vehicle's stop closes, at that stop.",
            "caveat": f"Failed starts have been under-reported since {FAILED_STARTS_UNDERCOUNT_SINCE} "
                      "(a counting fix is in progress); a drop after that date is not an improvement.",
            "undercount_since": FAILED_STARTS_UNDERCOUNT_SINCE,
        }

    response.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
    return _cached(("failed", days, granularity, rt, rn, end), build)


@router.get("/api/v1/analytics/devices-by-region")
def analytics_devices_by_region(
    response: Response,
    region_type: str = Query("neighborhood"), days: int = Query(7, ge=1, le=366),
    region_name: str | None = Query(None), granularity: str = Query("hour"),
) -> dict[str, Any]:
    """Vehicles on the map per region: now, and averaged over the window.
    With region_name, also that region's series by granularity."""
    if region_type not in REGION_LAYERS:
        raise HTTPException(400, f"region_type must be one of {list(REGION_LAYERS)}")
    start, end = _window(days, granularity if region_name else "day")
    if region_name:
        _region(region_type, region_name)

    def build():
        with connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT MAX(snapshot_time) FROM regional_metrics_narrow WHERE region_type = %s",
                        (region_type,))
            latest = cur.fetchone()[0]
            now_counts: dict[str, int] = {}
            if latest:
                cur.execute("SELECT region_name, count_total FROM regional_metrics_narrow "
                            "WHERE region_type = %s AND snapshot_time = %s", (region_type, latest))
                now_counts = {r[0]: int(r[1] or 0) for r in cur.fetchall()}
            cur.execute(
                """
                SELECT region_name, SUM(devices_sum), SUM(cycles)
                FROM analytics_region_devices_hourly
                WHERE region_type = %s AND hour >= %s AND hour < %s
                GROUP BY region_name
                """,
                (region_type, start, end),
            )
            avg = {r[0]: (int(r[1]), int(r[2])) for r in cur.fetchall()}
            series = None
            if region_name:
                cur.execute(
                    """
                    SELECT date_trunc(%s, hour AT TIME ZONE %s) AS b, SUM(devices_sum), SUM(cycles)
                    FROM analytics_region_devices_hourly
                    WHERE region_type = %s AND region_name = %s AND hour >= %s AND hour < %s
                    GROUP BY b ORDER BY b
                    """,
                    (granularity, TZ, region_type, region_name, start, end),
                )
                series = [{"bucket": _bucket_iso(b), "average": round(int(s_) / int(n), 1), "cycles": int(n)}
                          for b, s_, n in cur.fetchall() if n]
        names = sorted(set(now_counts) | set(avg))
        regions = [{
            "region": n,
            "now": now_counts.get(n),
            "average": round(avg[n][0] / avg[n][1], 1) if n in avg and avg[n][1] else None,
            "cycles": avg[n][1] if n in avg else 0,
        } for n in names]
        regions.sort(key=lambda r: -(r["average"] or 0))
        out = {
            **_meta(start, end, granularity if region_name else None, region_type=region_type),
            "as_of": latest.isoformat() if latest else None,
            "regions": regions,
            "definition": "Vehicles the feed shows inside the region, whatever their status: Veo "
                          "keeps rented and out-of-service vehicles in the feed, so this is "
                          "vehicles on the map, not strictly available ones. Average = mean over "
                          "every 2-minute cycle in the window.",
        }
        if region_name:
            out["region_name"] = region_name
            out["series"] = series
        return out

    response.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
    return _cached(("byregion", region_type, days, region_name, granularity, end), build)


@router.get("/api/v1/analytics/equity-compliance")
def analytics_equity_compliance(
    response: Response, days: int = Query(7), granularity: str = Query("hour"),
) -> dict[str, Any]:
    """Share of the Denver fleet inside the official Equity Areas, per bucket,
    against Exhibit B's 30%."""
    start, end = _window(days, granularity)

    def build():
        with connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT date_trunc(%s, snapshot_time AT TIME ZONE %s) AS b,
                       AVG(percent_all_devices_equity), COUNT(*)
                FROM snapshot_metadata_core
                WHERE snapshot_time >= %s AND snapshot_time < %s
                  AND percent_all_devices_equity IS NOT NULL
                GROUP BY b ORDER BY b
                """,
                (granularity, TZ, start, end),
            )
            rows = cur.fetchall()
        series = [{"bucket": _bucket_iso(b), "percent": round(float(p), 2), "cycles": int(n),
                   "meets_threshold": float(p) >= EQUITY_THRESHOLD_PCT} for b, p, n in rows]
        met = sum(1 for s in series if s["meets_threshold"])
        return {
            **_meta(start, end, granularity),
            "threshold_percent": EQUITY_THRESHOLD_PCT,
            "series": series,
            "buckets": len(series), "buckets_meeting_threshold": met,
            "definition": "Average, per bucket, of the share of the Denver fleet inside the "
                          "official Equity Areas at each 2-minute cycle. The line is Exhibit B's "
                          "30%. The contract's daily verdict (6-9 AM) is /compliance/daily; this "
                          "is context for it, not a second verdict.",
        }

    response.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
    return _cached(("equity", days, granularity, end), build)


@router.get("/api/v1/analytics/dwell")
def analytics_dwell(
    response: Response, region_type: str = Query("council_district"), days: int = Query(30, ge=1, le=366),
) -> dict[str, Any]:
    """Average dwell (time a vehicle sat at a stop before moving) per region x model."""
    if region_type not in REGION_TYPES:
        raise HTTPException(400, f"region_type must be one of {list(REGION_TYPES)}")
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)

    def build():
        from zoneinfo import ZoneInfo
        start_day = start.astimezone(ZoneInfo(TZ)).date()
        with connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT region_name, model, SUM(dwells), SUM(dwell_seconds)
                FROM analytics_dwell_daily
                WHERE region_type = %s AND day >= %s
                GROUP BY region_name, model
                """,
                (region_type, start_day),
            )
            rows = cur.fetchall()
            cur.execute("SELECT watermark_time FROM analytics_rollup_state WHERE name = 'stops'")
            got = cur.fetchone()
        by_region: dict[str, dict[str, Any]] = {}
        models: set[str] = set()
        for region, model, n, secs in rows:
            n, secs = int(n or 0), int(secs or 0)
            models.add(model)
            by_region.setdefault(region, {})[model] = {
                "dwells": n,
                "average_minutes": round(secs / n / 60, 1) if n >= MIN_DWELLS_FOR_AVERAGE else None,
            }
        return {
            **_meta(start, end, None, region_type=region_type),
            "models": sorted(models),
            "regions": [{"region": r, "by_model": dict(sorted(v.items()))} for r, v in sorted(by_region.items())],
            "min_dwells_for_average": MIN_DWELLS_FOR_AVERAGE,
            "data_through": got[0].isoformat() if got and got[0] else None,
            "definition": "Dwell is how long a vehicle stayed at a stop, from arrival to departure, "
                          "for stops that closed in the window (Denver days); open stops and stops "
                          "over 30 days are not counted. Placed at the stop.",
        }

    response.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
    return _cached(("dwell", region_type, days, end), build)


@router.get("/api/v1/analytics/fleet-status")
def analytics_fleet_status(
    response: Response, days: int = Query(7), granularity: str = Query("hour"),
    model: str | None = Query(None, max_length=40),
) -> dict[str, Any]:
    """Average vehicles available, in use, out of service and off-map per bucket."""
    start, end = _window(days, granularity)

    def build():
        with connection() as conn, conn.cursor() as cur:
            if model:
                cur.execute(
                    """
                    SELECT date_trunc(%s, snapshot_time AT TIME ZONE %s) AS b,
                           AVG((models -> %s ->> 'available')::numeric),
                           AVG((models -> %s ->> 'reserved')::numeric),
                           AVG((models -> %s ->> 'out_of_service')::numeric),
                           NULL, COUNT(*)
                    FROM device_status_snapshots
                    WHERE snapshot_time >= %s AND snapshot_time < %s AND models ? %s
                    GROUP BY b ORDER BY b
                    """,
                    (granularity, TZ, model, model, model, start, end, model),
                )
            else:
                cur.execute(
                    """
                    SELECT date_trunc(%s, snapshot_time AT TIME ZONE %s) AS b,
                           AVG(available), AVG(reserved), AVG(out_of_service), AVG(off_map), COUNT(*)
                    FROM device_status_snapshots
                    WHERE snapshot_time >= %s AND snapshot_time < %s
                    GROUP BY b ORDER BY b
                    """,
                    (granularity, TZ, start, end),
                )
            rows = cur.fetchall()
        r1 = lambda v: None if v is None else round(float(v), 1)  # noqa: E731
        return {
            **_meta(start, end, granularity, model=model),
            "series": [{"bucket": _bucket_iso(b), "available": r1(a), "in_use": r1(u),
                        "out_of_service": r1(o), "off_map": r1(off), "cycles": int(n)}
                       for b, a, u, o, off, n in rows],
            "definition": "Averages per bucket of the feed's own status counts at each 2-minute "
                          "cycle. In use = reserved (Veo keeps rented vehicles in the feed). "
                          "Off-map = seen in the last 7 days but absent from the feed; recorded "
                          "from 2026-10-07, null before (not zero), and not split by model.",
        }

    response.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
    return _cached(("status", days, granularity, model, end), build)


@router.get("/api/v1/analytics/fleet-counts")
def analytics_fleet_counts(response: Response) -> dict[str, Any]:
    """Devices visible now (latest cycle) and every device ever seen, by model."""
    def build():
        with connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT snapshot_time, total, models FROM device_status_snapshots "
                        "ORDER BY snapshot_time DESC LIMIT 1")
            latest = cur.fetchone()
            cur.execute("SELECT COALESCE(NULLIF(TRIM(current_vehicle_model_name), ''), 'Unknown'), "
                        "COUNT(*) FROM device_state GROUP BY 1 ORDER BY 2 DESC")
            ever = [(m, int(n)) for m, n in cur.fetchall()]
            cur.execute("SELECT MIN(first_ever_observed_at) FROM device_state")
            since = cur.fetchone()[0]
        visible_by_model = {}
        if latest and latest[2]:
            visible_by_model = {m: sum(int(v or 0) for v in s.values()) for m, s in latest[2].items()}
        return {
            "as_of": latest[0].isoformat() if latest else None,
            "visible_now": int(latest[1]) if latest else None,
            "visible_now_by_model": dict(sorted(visible_by_model.items(), key=lambda kv: -kv[1])),
            "ever_seen_total": sum(n for _, n in ever),
            "ever_seen_by_model": dict(ever),
            "ever_seen_since": since.isoformat() if since else None,
            "definition": "Visible now: every vehicle in the latest feed cycle, any status. Ever "
                          "seen: every distinct vehicle (by plate) since tracking began, by the "
                          "model it last reported.",
        }

    response.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
    return _cached(("counts", datetime.now(timezone.utc).replace(second=0, microsecond=0, minute=0)), build)
