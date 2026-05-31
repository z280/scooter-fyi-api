"""Public, unauthenticated REST endpoints (spec §7).

CORS is mounted at the app level in src/main.py.
"""

from __future__ import annotations

import re
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response

from . import boundaries
from .pg import connection
from .quality import compute_quality_designation

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
# Boundary geometries (static GeoJSON for map overlays)
# ---------------------------------------------------------------------------
# Cache-Control for static boundary responses. Updated only when the
# baked-in boundary files in data/ change (rebuild + redeploy).
_BOUNDARIES_CACHE_HEADER = "public, max-age=86400, stale-while-revalidate=604800"


@router.get("/api/v1/boundaries")
def boundaries_list(response: Response) -> dict[str, Any]:
    """List every available boundary layer with its feature count,
    bbox, and the URL where its GeoJSON lives."""
    response.headers["Cache-Control"] = "public, max-age=3600"
    return {"layers": boundaries.list_layers()}


@router.get("/api/v1/boundaries/{layer}")
def boundaries_geojson(layer: str, response: Response) -> dict[str, Any]:
    """Full GeoJSON FeatureCollection for one boundary layer.

    Direct ingestion into Mapbox/MapLibre/Leaflet/OpenLayers:

        map.addSource("nb", { type: "geojson", data: "/api/v1/boundaries/neighborhood" });

    Layer values: v1, v2, neighborhood, council_district, community_network.
    """
    fc = boundaries.get_layer(layer)
    if fc is None:
        raise HTTPException(
            404,
            detail=(
                f"unknown layer '{layer}'. Available: "
                f"{', '.join(l['region_type'] for l in boundaries.list_layers())}"
            ),
        )
    response.headers["Cache-Control"] = _BOUNDARIES_CACHE_HEADER
    return fc


# ---------------------------------------------------------------------------
# Current device locations (for map rendering)
# ---------------------------------------------------------------------------
@router.get("/api/v1/devices/current")
def devices_current(
    form_factor: str | None = Query(
        None,
        description='Filter by form_factor. One of "bicycle", "scooter". Default: no filter.',
    ),
    spatial_status: str | None = Query(
        None,
        description='Filter by spatial_status. One of "denver_core", "china_glitch", "other_outlier". Default behavior depends on include_outliers.',
    ),
    include_outliers: bool = Query(
        False,
        description="If false (default), only return devices with spatial_status='denver_core'. Set true to include China-factory glitches and other outliers.",
    ),
    bbox: str | None = Query(
        None,
        description='Comma-separated "min_lon,min_lat,max_lon,max_lat" bounding box filter (WGS84).',
    ),
) -> dict[str, Any]:
    """GeoJSON FeatureCollection of every device's current position,
    drawn from the most recent successfully-completed observation cycle.

    Suitable for direct ingestion into Mapbox/Leaflet/MapLibre:

        const r = await fetch("/api/v1/devices/current");
        const geo = await r.json();
        map.getSource("devices").setData(geo);
    """
    # Resolve which cycle to use
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

            # Build the filter. All predicates prefix `r.` so they compose
            # with the EXISTS subquery on negative_reports below.
            where = ["r.cycle_id = %s"]
            params: list[Any] = [cycle_id]

            # Outlier handling: explicit spatial_status overrides include_outliers
            if spatial_status:
                where.append("r.spatial_status = %s")
                params.append(spatial_status)
            elif not include_outliers:
                where.append("r.spatial_status = 'denver_core'")

            if form_factor:
                where.append("r.form_factor = %s")
                params.append(form_factor)

            if bbox:
                parts = bbox.split(",")
                if len(parts) != 4:
                    raise HTTPException(400, detail="bbox must be 4 comma-separated numbers")
                try:
                    min_lon, min_lat, max_lon, max_lat = [float(p) for p in parts]
                except ValueError as e:
                    raise HTTPException(400, detail=f"bbox parse error: {e}")
                where.append(
                    "r.longitude BETWEEN %s AND %s AND r.latitude BETWEEN %s AND %s"
                )
                params.extend([min_lon, max_lon, min_lat, max_lat])

            # has_negative_report: true iff there's a report against THIS
            # vehicle_identifier in the SAME h3_10 cell, ≤24h old. The
            # row becomes "stale" (false here) the moment the scooter
            # moves to a different h3_10, even though the report row
            # remains queryable from /api/v1/private/reports.
            sql = (
                "SELECT r.device_id, r.form_factor, r.latitude, r.longitude, r.spatial_status, "
                "       r.vehicle_identifier, r.is_disabled, r.is_reserved, "
                "       r.current_range_meters, r.propulsion_type, "
                "       r.h3_8_index, r.h3_9_index, r.h3_10_index, "
                "       r.range_percentile_by_type, r.range_rank_unique_by_type, "
                "       r.range_rank_all_by_type, r.range_rank_all_devices, "
                "       r.range_rank_h3_8_peers, r.range_rank_h3_9_peers, "
                "       r.range_rank_h3_10_peers, "
                "       EXISTS ("
                "           SELECT 1 FROM negative_reports nr "
                "           WHERE nr.vehicle_identifier = r.vehicle_identifier "
                "             AND nr.h3_10_index = r.h3_10_index "
                "             AND nr.reported_at >= NOW() - INTERVAL '24 hours'"
                "       ) AS has_negative_report, "
                "       r.max_range_meters_for_type, "
                "       ds.number_failed_starts, ds.first_observed_at_location "
                "FROM raw_telemetry_points r "
                "LEFT JOIN device_state ds USING (vehicle_identifier) "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY r.device_id"
            )
            cur.execute(sql, params)
            rows = cur.fetchall()

    # vehicle_plate is deliberately not selected — see src/identity.py for the
    # public/private identifier split. Only vehicle_identifier (the hash) goes
    # over an unauthenticated wire.
    features = []
    for r in rows:
        quality = compute_quality_designation(
            current_range_meters=r[8],
            max_range_meters_for_type=r[21],
            is_disabled=r[6],
            is_reserved=r[7],
            number_failed_starts=r[22],
            first_observed_at_location=r[23],
            has_negative_report=bool(r[20]),
        )
        features.append({
            "type": "Feature",
            "id": r[0],
            "geometry": {"type": "Point", "coordinates": [float(r[3]), float(r[2])]},
            "properties": {
                "device_id": r[0],
                "form_factor": r[1],
                "spatial_status": r[4],
                "vehicle_identifier": r[5],
                "is_disabled": r[6],
                "is_reserved": r[7],
                "current_range_meters": r[8],
                "propulsion_type": r[9],
                "h3_8_index": int(r[10]) if r[10] is not None else None,
                "h3_9_index": int(r[11]) if r[11] is not None else None,
                "h3_10_index": int(r[12]) if r[12] is not None else None,
                "range_percentile_by_type": r[13],
                "range_rank_unique_by_type": r[14],
                "range_rank_all_by_type": r[15],
                "range_rank_all_devices": r[16],
                "range_rank_h3_8_peers": r[17],
                "range_rank_h3_9_peers": r[18],
                "range_rank_h3_10_peers": r[19],
                "has_negative_report": bool(r[20]),
                "quality_designation": quality,
            },
        })

    return {
        "type": "FeatureCollection",
        "metadata": {
            "cycle_id": str(cycle_id),
            "snapshot_time": snapshot_time.isoformat(),
            "device_count": len(features),
            "filters": {
                "form_factor": form_factor,
                "spatial_status": spatial_status,
                "include_outliers": include_outliers,
                "bbox": bbox,
            },
        },
        "features": features,
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
