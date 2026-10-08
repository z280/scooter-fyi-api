"""Fleet analytics endpoints (docs/implemented/PLAN_FLEET_ANALYTICS.md).

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

from zoneinfo import ZoneInfo

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
#: The failed-start counter was under-reported from here until the counting
#: fix of COMPARABLE_SINCE (dc292b6, deployed 2026-10-06 01:36 UTC).
FAILED_STARTS_UNDERCOUNT_SINCE = "2026-08-10"

#: How trip_events, device_history stops and failed starts were COUNTED changed
#: twice. Figures from different eras measure different things, so every series
#: built on them carries these dates and the API says where the current method
#: begins; a chart must not present a counting change as a change in Denver.
#: (Measured on production: before Oct 6 the median trip was 28-49 m of GPS
#: drift at ~4,000 an hour; after it, ~800 m at ~1,000 an hour.)
COUNTING_CHANGES = (
    {
        "at": "2026-08-10T04:15:00+00:00",
        "commit": "8a51d4d",
        "affects": ["rides", "dwell"],
        "summary": "One rental, one trip. Before this, a rented scooter moving in the feed "
                   "logged a trip at every 2-minute sample (about a 6x over-count), and its "
                   "stops were split into 2-minute pieces.",
    },
    {
        "at": "2026-10-06T01:36:00+00:00",
        "commit": "dc292b6",
        "affects": ["rides", "dwell", "failed_starts"],
        "summary": "GPS drift is no longer a trip, and a rental released where it started "
                   "counts as a failed start. Before this, about 2 of every 3 trips were "
                   "drift, drift restarted dwell, and failed starts were under-counted "
                   "(from 2026-08-10).",
    },
)
#: Where the current counting method begins: compare figures only after this.
COMPARABLE_SINCE = COUNTING_CHANGES[-1]["at"]


def _eras(kind: str) -> dict[str, Any]:
    changes = [c for c in COUNTING_CHANGES if kind in c["affects"]]
    return {"counting_changes": changes, "comparable_since": changes[-1]["at"] if changes else None}
CACHE_SECONDS = 300
#: device_status_snapshots keeps 30 days (compute.py prunes it).
FLEET_STATUS_RETENTION_DAYS = 30

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


DEN = ZoneInfo(TZ)


def _window(days: int, granularity: str, cap: int | None = None) -> tuple[datetime, datetime]:
    """[start, end): end is the top of the current UTC hour. For day, week and
    month the start is pulled back to a whole Denver-local bucket boundary,
    so the first bar is never a partial one that reads as a dip."""
    if granularity not in GRANULARITIES:
        raise HTTPException(400, f"granularity must be one of {list(GRANULARITIES)}")
    limit = min(MAX_DAYS[granularity], cap or MAX_DAYS[granularity])
    if not 1 <= days <= limit:
        raise HTTPException(400, f"days must be 1-{limit} for granularity '{granularity}'")
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    if granularity != "hour":
        local = start.astimezone(DEN)
        d = local.date()
        if granularity == "week":
            d = d - timedelta(days=d.weekday())        # ISO weeks start Monday
        elif granularity == "month":
            d = d.replace(day=1)
        start = datetime(d.year, d.month, d.day, tzinfo=DEN).astimezone(timezone.utc)
    return start, end


def _bucket_sql(col: str, granularity: str) -> tuple[str, tuple]:
    """SQL for a bucket start. HOURS are grouped in UTC: on the fall-back
    night Denver's 01:00 happens twice, and grouping on local time would
    merge two real hours into one bar."""
    if granularity == "hour":
        return f"date_trunc('hour', {col})", ()
    return f"date_trunc(%s, {col} AT TIME ZONE %s)", (granularity, TZ)


def _bucket_start(v: datetime, granularity: str) -> datetime:
    """An aware bucket start. Hour buckets come back aware (UTC); day/week/
    month come back as naive Denver-local midnights, which are never
    ambiguous in Denver (DST changes at 02:00)."""
    if granularity == "hour":
        return v.astimezone(DEN)
    return v.replace(tzinfo=DEN)


def _bucket_end(b: datetime, granularity: str) -> datetime:
    if granularity == "hour":
        return b + timedelta(hours=1)
    if granularity == "day":
        n = b.date() + timedelta(days=1)
    elif granularity == "week":
        n = b.date() + timedelta(days=7)
    else:
        n = (b.date().replace(day=28) + timedelta(days=4)).replace(day=1)
    return datetime(n.year, n.month, n.day, tzinfo=DEN)


def _bucket_fields(v: datetime, granularity: str, through: datetime) -> dict[str, Any]:
    """{"bucket": ISO start with offset, "partial": True only if the bucket
    runs past what the data covers}: a partial last bar is labelled, never
    passed off as a drop."""
    b = _bucket_start(v, granularity)
    out: dict[str, Any] = {"bucket": b.isoformat()}
    if _bucket_end(b, granularity) > through:
        out["partial"] = True
    return out


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


def _rollup_series(table: str, value_cols: tuple[str, ...], rt: str, rn: str,
                   start: datetime, end: datetime, granularity: str,
                   through: datetime) -> tuple[list, list, dict]:
    sums = ", ".join(f"SUM({c})" for c in value_cols)
    bexpr, bparams = _bucket_sql("hour", granularity)
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {bexpr} AS b, model, {sums}
            FROM {table}
            WHERE region_type = %s AND region_name = %s AND hour >= %s AND hour < %s
            GROUP BY b, model ORDER BY b
            """,
            (*bparams, rt, rn, start, min(end, through)),
        )
        rows = cur.fetchall()
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
            **_bucket_fields(b, granularity, min(end, through)),
            "by_model": {m: v[0] for m, v in sorted(per.items())},
            "total": sum(v[0] for v in per.values()),
            **({value_cols[1]: sum(v[1] for v in per.values())} if len(value_cols) > 1 else {}),
        })
    return series, sorted(models), {"totals": dict(zip(value_cols, totals))}


def _rides_through() -> datetime | None:
    """Rides are folded every cycle: through = the top of the hour after the
    newest folded ride's hour, never later than now."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT MAX(hour) FROM analytics_rides_hourly WHERE region_type = 'city'")
        h = cur.fetchone()[0]
    return h + timedelta(hours=1) if h else None


def _stops_through() -> datetime | None:
    """How far failed starts and dwell are complete: closes settle for
    DEPARTURE_LAG before they are folded, and the legacy sweep may not have
    reached the cutover yet."""
    from .analytics_rollups import DEPARTURE_LAG
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, watermark_time FROM analytics_rollup_state "
                    "WHERE name IN ('stops', 'stops_cutover')")
        st = dict(cur.fetchall())
    lag_line = datetime.now(timezone.utc) - DEPARTURE_LAG
    cutover, swept = st.get("stops_cutover"), st.get("stops")
    if cutover is None:
        return swept
    if swept is None or swept < cutover:
        return swept
    return lag_line


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
        through = min(end, _rides_through() or start)
        series, models, extra = _rollup_series(
            "analytics_rides_hourly", ("rides",), rt, rn, start, end, granularity, through)
        return {
            **_meta(start, end, granularity, region={"type": rt, "name": rn}),
            "models": models, "series": series, "rides": extra["totals"]["rides"],
            "data_through": through.isoformat(),
            "definition": "A ride is a vehicle that moved from one stop to another (trip_events), "
                          "placed by where it started and counted when the move was detected.",
            # Shown to readers as is: plain words, no field names.
            "caveat": "How rides are counted changed on Aug 10 and Oct 6, 2026. Earlier "
                      "figures are inflated (GPS drift and repeated samples were counted as "
                      "rides) and cannot be compared with later ones.",
            **_eras("rides"),
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
        through = min(end, _stops_through() or start)
        series, models, extra = _rollup_series(
            "analytics_failed_starts_hourly", ("failed_starts", "stops_with_failures"),
            rt, rn, start, end, granularity, through)
        return {
            **_meta(start, end, granularity, region={"type": rt, "name": rn}),
            "models": models, "series": series,
            "failed_starts": extra["totals"]["failed_starts"],
            "stops_with_failures": extra["totals"]["stops_with_failures"],
            "data_through": through.isoformat(),
            "definition": "A failed start is a rental that ended where it began, counted when the "
                          "vehicle's stop closes, at that stop. Closes are folded in after a 6-hour "
                          "settle, so the series ends at data_through.",
            "caveat": f"Failed starts were under-reported from {FAILED_STARTS_UNDERCOUNT_SINCE} until "
                      "the counting fix of 2026-10-06; the drop in August and the rise in October "
                      "are counting changes, not changes in Denver.",
            "undercount_since": FAILED_STARTS_UNDERCOUNT_SINCE,
            "undercount_until": COMPARABLE_SINCE,
            **_eras("failed_starts"),
        }

    response.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
    return _cached(("failed", days, granularity, rt, rn, end), build)


def _region_now(region_type: str) -> tuple[datetime | None, dict[str, int]]:
    """The latest cycle's per-region counts. Found via snapshot_metadata_core
    (indexed on snapshot_time) and read by cycle_id (regional_metrics_narrow's
    key): MAX(snapshot_time) on regional_metrics_narrow itself scanned ~5 GB."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT cycle_id, snapshot_time FROM snapshot_metadata_core "
                    "ORDER BY snapshot_time DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None, {}
        cur.execute("SELECT region_name, count_total FROM regional_metrics_narrow "
                    "WHERE cycle_id = %s AND region_type = %s", (row[0], region_type))
        return row[1], {r[0]: int(r[1] or 0) for r in cur.fetchall()}


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
        latest, now_counts = _cached(("bynow", region_type, end), lambda: _region_now(region_type))
        with connection() as conn, conn.cursor() as cur:
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
                bexpr, bparams = _bucket_sql("hour", granularity)
                cur.execute(
                    f"""
                    SELECT {bexpr} AS b, SUM(devices_sum), SUM(cycles)
                    FROM analytics_region_devices_hourly
                    WHERE region_type = %s AND region_name = %s AND hour >= %s AND hour < %s
                    GROUP BY b ORDER BY b
                    """,
                    (*bparams, region_type, region_name, start, end),
                )
                series = [{**_bucket_fields(b, granularity, end),
                           "average": round(int(s_) / int(n), 1), "cycles": int(n)}
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
        bexpr, bparams = _bucket_sql("snapshot_time", granularity)
        with connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {bexpr} AS b, AVG(percent_all_devices_equity), COUNT(*)
                FROM snapshot_metadata_core
                WHERE snapshot_time >= %s AND snapshot_time < %s
                  AND percent_all_devices_equity IS NOT NULL
                GROUP BY b ORDER BY b
                """,
                (*bparams, start, end),
            )
            rows = cur.fetchall()
        series = [{**_bucket_fields(b, granularity, end), "percent": round(float(p), 2), "cycles": int(n),
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
    start = datetime.combine(end.astimezone(DEN).date() - timedelta(days=days - 1),
                             datetime.min.time(), tzinfo=DEN).astimezone(timezone.utc)

    def build():
        # Exactly `days` Denver days ending today (whole days: the rollup is
        # by day), not a partial extra day at the front.
        first_day = end.astimezone(DEN).date() - timedelta(days=days - 1)
        with connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT region_name, model, SUM(dwells), SUM(dwell_seconds)
                FROM analytics_dwell_daily
                WHERE region_type = %s AND day >= %s
                GROUP BY region_name, model
                """,
                (region_type, first_day),
            )
            rows = cur.fetchall()
        through = _stops_through()
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
            "data_through": through.isoformat() if through else None,
            **_eras("dwell"),
            "caveat": "How stops are counted changed on 2026-08-10 (stops were split into "
                      "2-minute pieces before) and 2026-10-06 (GPS drift restarted dwell before); "
                      "a window spanning those dates averages different methods.",
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
    # device_status_snapshots is pruned to 30 days (compute.py), so a longer
    # window would only ever show 30 days under a bigger label.
    start, end = _window(days, granularity, cap=FLEET_STATUS_RETENTION_DAYS)

    def build():
        bexpr, bparams = _bucket_sql("snapshot_time", granularity)
        with connection() as conn, conn.cursor() as cur:
            if model:
                cur.execute(
                    f"""
                    SELECT {bexpr} AS b,
                           AVG((models -> %s ->> 'available')::numeric),
                           AVG((models -> %s ->> 'reserved')::numeric),
                           AVG((models -> %s ->> 'out_of_service')::numeric),
                           NULL, COUNT(*)
                    FROM device_status_snapshots
                    WHERE snapshot_time >= %s AND snapshot_time < %s AND models ? %s
                    GROUP BY b ORDER BY b
                    """,
                    (*bparams, model, model, model, start, end, model),
                )
            else:
                cur.execute(
                    f"""
                    SELECT {bexpr} AS b,
                           AVG(available), AVG(reserved), AVG(out_of_service), AVG(off_map), COUNT(*)
                    FROM device_status_snapshots
                    WHERE snapshot_time >= %s AND snapshot_time < %s
                    GROUP BY b ORDER BY b
                    """,
                    (*bparams, start, end),
                )
            rows = cur.fetchall()
        r1 = lambda v: None if v is None else round(float(v), 1)  # noqa: E731
        return {
            **_meta(start, end, granularity, model=model),
            "series": [{**_bucket_fields(b, granularity, end), "available": r1(a), "in_use": r1(u),
                        "out_of_service": r1(o), "off_map": r1(off), "cycles": int(n)}
                       for b, a, u, o, off, n in rows],
            "definition": "Averages per bucket of the feed's own status counts at each 2-minute "
                          "cycle. In use = reserved (Veo keeps rented vehicles in the feed). "
                          "Off-map = Denver vehicles seen in the last 7 days but absent from the "
                          "feed; recorded from 2026-10-07, null before (not zero), and not split "
                          "by model. History is the last 30 days (the source is pruned at 30).",
            "retention_days": FLEET_STATUS_RETENTION_DAYS,
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
            "definition": "Visible now: every vehicle in Denver (the buffered city polygon) in "
                          "the latest feed cycle, any status. Ever seen: every distinct vehicle "
                          "(by plate) since tracking began, anywhere in the feed, by the model it "
                          "last reported ('Unknown' when the feed never named it).",
        }

    response.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
    return _cached(("counts", datetime.now(timezone.utc).replace(second=0, microsecond=0, minute=0)), build)
