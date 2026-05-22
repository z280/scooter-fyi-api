"""Public, unauthenticated REST endpoints (spec §7).

CORS is mounted at the app level in src/main.py.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
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
