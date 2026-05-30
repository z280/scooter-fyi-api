"""Public, unauthenticated REST endpoints (spec §7).

CORS is mounted at the app level in src/main.py.
"""

from __future__ import annotations

import re
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .pg import connection

router = APIRouter()


def _row_to_dict(cur, row) -> dict[str, Any]:
    if row is None:
        return {}
    return {desc.name: _norm(v) for desc, v in zip(cur.description, row)}


def _norm(v):
    if isinstance(v, datetime):
        return v.isoformat()
    return v


@router.get("/health")
def health() -> dict[str, Any]:
    sql_ingest = """
        SELECT cycle_id, snapshot_time
        FROM snapshot_metadata_core
        ORDER BY snapshot_time DESC LIMIT 1
    """
    sql_archive = "SELECT value FROM system_state WHERE key = 'last_archive_ts'"

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_ingest)
            row = cur.fetchone()
            last_ingest_ts = row[1].isoformat() if row else None
            last_cycle_id = str(row[0]) if row else None

            cur.execute(sql_archive)
            r2 = cur.fetchone()
            last_upload_ts = r2[0] if r2 else None

    return {
        "last_data_ingest_ts": last_ingest_ts,
        "last_data_upload_ts": last_upload_ts,
        "last_cycle_id": last_cycle_id,
        "last_retrieval_ts": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/api/v1/snapshots/latest")
def latest_snapshot() -> dict[str, Any]:
    sql = "SELECT * FROM snapshot_metadata_core ORDER BY snapshot_time DESC LIMIT 1"
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            if not row:
                raise HTTPException(503, detail="no snapshots yet")
            return _row_to_dict(cur, row)


@router.get("/api/v1/spatial-snapshot")
def spatial_snapshot(
    layer: str = Query(..., description="region_type to filter, e.g. v1, neighborhood"),
    time: str | None = Query(None, description="ISO timestamp; defaults to latest"),
) -> dict[str, Any]:
    """Return {region_name: {total, bikes, scooters}} for the requested layer.

    If ``time`` is provided, snaps to the nearest snapshot at or before it.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            if time:
                try:
                    target = datetime.fromisoformat(time.replace("Z", "+00:00"))
                except ValueError as e:
                    raise HTTPException(400, detail=f"bad time format: {e}")
                cur.execute(
                    """
                    SELECT snapshot_time FROM regional_metrics_narrow
                    WHERE region_type = %s AND snapshot_time <= %s
                    ORDER BY snapshot_time DESC LIMIT 1
                    """,
                    (layer, target),
                )
            else:
                cur.execute(
                    """
                    SELECT snapshot_time FROM regional_metrics_narrow
                    WHERE region_type = %s
                    ORDER BY snapshot_time DESC LIMIT 1
                    """,
                    (layer,),
                )
            snap = cur.fetchone()
            if not snap:
                raise HTTPException(404, detail=f"no data for layer={layer}")

            cur.execute(
                """
                SELECT region_name, count_total, count_bikes, count_scooters
                FROM regional_metrics_narrow
                WHERE region_type = %s AND snapshot_time = %s
                """,
                (layer, snap[0]),
            )
            rows = cur.fetchall()

    return {
        "snapshot_time": snap[0].isoformat(),
        "layer": layer,
        "regions": {
            r[0]: {"total": int(r[1] or 0), "bikes": int(r[2] or 0), "scooters": int(r[3] or 0)}
            for r in rows
        },
    }


_RANGE_RE = re.compile(r"^(\d+)([dh])$")


@router.get("/api/v1/analytics/trend")
def analytics_trend(
    layer: str = Query(...),
    name: str = Query(...),
    range: str = Query("7d"),
) -> dict[str, Any]:
    """Return a time-series for the given region."""
    m = _RANGE_RE.match(range)
    if not m:
        raise HTTPException(400, detail="range must look like '7d' or '24h'")
    n, unit = int(m.group(1)), m.group(2)
    delta = timedelta(days=n) if unit == "d" else timedelta(hours=n)
    since = datetime.now(timezone.utc) - delta

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT snapshot_time, count_total, count_bikes, count_scooters
                FROM regional_metrics_narrow
                WHERE region_type = %s AND region_name = %s AND snapshot_time >= %s
                ORDER BY snapshot_time ASC
                """,
                (layer, name, since),
            )
            rows = cur.fetchall()

    return {
        "layer": layer,
        "region_name": name,
        "range": range,
        "points": [
            {
                "snapshot_time": r[0].isoformat(),
                "count_total": int(r[1] or 0),
                "count_bikes": int(r[2] or 0),
                "count_scooters": int(r[3] or 0),
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Daily SLA compliance (6 AM-9 AM Denver window)
# ---------------------------------------------------------------------------
def _daily_row_to_dict(cur, row) -> dict[str, Any]:
    if row is None:
        return {}
    d = {}
    for desc, v in zip(cur.description, row):
        if isinstance(v, datetime):
            d[desc.name] = v.isoformat()
        elif isinstance(v, date_cls):
            d[desc.name] = v.isoformat()
        elif v is None:
            d[desc.name] = None
        else:
            # NUMERIC comes back as Decimal — make it JSON-safe
            try:
                d[desc.name] = float(v)
            except (TypeError, ValueError):
                d[desc.name] = v
    # snapshot_count should stay an int
    if "snapshot_count" in d and d["snapshot_count"] is not None:
        d["snapshot_count"] = int(d["snapshot_count"])
    # booleans should stay booleans (float() above would coerce)
    for k in ("compliance_v1_pass", "compliance_v2_pass"):
        if k in d and d[k] is not None:
            d[k] = bool(d[k])
    return d


@router.get("/api/v1/compliance/daily/latest")
def daily_compliance_latest() -> dict[str, Any]:
    """Most recent computed daily SLA window."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM daily_sla_compliance ORDER BY sla_date DESC LIMIT 1"
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(503, detail="no daily SLA rows computed yet")
            return _daily_row_to_dict(cur, row)


@router.get("/api/v1/compliance/daily")
def daily_compliance_one(
    date: str = Query(..., description="Denver-local date, YYYY-MM-DD"),
) -> dict[str, Any]:
    """SLA window for a single Denver-local date."""
    try:
        d = date_cls.fromisoformat(date)
    except ValueError as e:
        raise HTTPException(400, detail=f"bad date format: {e}")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM daily_sla_compliance WHERE sla_date = %s",
                (d,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, detail=f"no SLA row for {date}")
            return _daily_row_to_dict(cur, row)


@router.get("/api/v1/compliance/daily/range")
def daily_compliance_range(
    start: str = Query(..., description="inclusive start date YYYY-MM-DD"),
    end: str | None = Query(None, description="inclusive end date; default today"),
    limit: int = Query(366, ge=1, le=1000),
) -> dict[str, Any]:
    """Range of daily SLA rows, ascending."""
    try:
        d_start = date_cls.fromisoformat(start)
        d_end = date_cls.fromisoformat(end) if end else date_cls.today()
    except ValueError as e:
        raise HTTPException(400, detail=f"bad date format: {e}")
    if d_end < d_start:
        raise HTTPException(400, detail="end < start")

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM daily_sla_compliance
                WHERE sla_date >= %s AND sla_date <= %s
                ORDER BY sla_date ASC
                LIMIT %s
                """,
                (d_start, d_end, limit),
            )
            rows = [_daily_row_to_dict(cur, r) for r in cur.fetchall()]
    return {
        "start": d_start.isoformat(),
        "end": d_end.isoformat(),
        "count": len(rows),
        "rows": rows,
    }
