"""Public, unauthenticated REST endpoints (spec §7).

CORS is mounted at the app level in src/main.py.
"""

from __future__ import annotations

import re
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any

import h3
from fastapi import APIRouter, HTTPException, Query, Request, Response

from . import boundaries
from .api_device_features import feature_payload
from .api_frontend_reports import reliability_report_type_sql
from .device_features import STATUS_NEEDS_CONFIRMED as FEATURE_STATUS_NEEDS_CONFIRMED
from .daily_sla import _AVG_FIELDS
from .dwell_stats import stats_for_cycle
from .equity_groups import COMPLIANCE_GROUPS, compliance_pass_column
from .pg import connection
from .quality import (
    compute_battery_percent,
    compute_quality_designation,
    compute_reliability_tier,
)

_COMPLIANCE_PASS_COLUMNS = tuple(compliance_pass_column(g) for g in COMPLIANCE_GROUPS)

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
# Opt-in field groups for /api/v1/devices/current. The default payload is
# deliberately lean (low-end phones re-download it every 90 s); analysis-mode
# extras come back via ?include=.
_INCLUDE_TOKENS = ("ranks", "h3")

# Off the wire by default; restored by ?include=ranks.
_RANK_FIELDS = (
    "range_percentile_by_type",
    "range_rank_unique_by_type",
    "range_rank_all_by_type",
    "range_rank_all_devices",
    "range_rank_h3_8_peers",
    "range_rank_h3_9_peers",
    "range_rank_h3_10_peers",
)

# Conservative client-side cache: the underlying cycle only changes every
# ~10 min, but has_negative_report / dwell drift within a cycle. Pair with
# the cycle-keyed ETag for cheap 304 revalidation on the 90 s poll loop.
_DEVICES_CACHE_HEADER = "public, max-age=30"


def _if_none_match_hit(request: Request, etag: str) -> bool:
    inm = request.headers.get("if-none-match")
    if not inm:
        return False
    if inm.strip() == "*":
        return True
    return etag in (t.strip() for t in inm.split(","))


def _devices_current_impl(
    request: Request,
    response: Response,
    *,
    form_factor: str | None,
    spatial_status: str | None,
    include_outliers: bool,
    bbox: str | None,
    include: str | None,
    include_plate: bool = False,
    resource: str = "devices",
    cache_header: str = _DEVICES_CACHE_HEADER,
    viewed_by: str | None = None,
) -> Any:
    """Shared builder for the public `/api/v1/devices/current` and the
    session-gated `/api/v1/user/devices/current`.

    ``include_plate`` adds the admin-only private fields — raw
    ``vehicle_plate``, ``first_ever_observed_at``, and the observed max
    range — that used to live behind `/api/v1/private/devices/current`. It
    is NEVER derived from a query param (that would let anyone opt in); the
    caller decides it from the authenticated session.
    """
    tokens: set[str] = set()
    if include:
        tokens = {t.strip() for t in include.split(",") if t.strip()}
        unknown = tokens.difference(_INCLUDE_TOKENS)
        if unknown:
            raise HTTPException(
                400,
                detail=(
                    f"unknown include token(s): {', '.join(sorted(unknown))}. "
                    f"Valid: {', '.join(_INCLUDE_TOKENS)}"
                ),
            )
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

            # Validate the bbox up front — before the 304 short-circuit — so
            # a malformed bbox always 400s even when the ETag matches.
            bbox_vals: tuple[float, float, float, float] | None = None
            if bbox:
                parts = bbox.split(",")
                if len(parts) != 4:
                    raise HTTPException(400, detail="bbox must be 4 comma-separated numbers")
                try:
                    bbox_vals = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
                except ValueError as e:
                    raise HTTPException(400, detail=f"bbox parse error: {e}")

            # Weak, cycle-keyed ETag: the 90 s poll loop revalidates for
            # free until a new cycle lands (~every 10 min). Weak because
            # has_negative_report drift within a cycle — a 304 defers that
            # by at most one cycle. The ETag must vary with EVERY input that
            # changes the body: the include tokens AND the filters
            # (form_factor / spatial_status / include_outliers / bbox), or a
            # client reusing a tag across filtered requests gets a 304 for a
            # different representation.
            # include_plate is part of the key so an admin's plate-bearing
            # body can never be handed back to a non-admin via a shared 304
            # (and the /user endpoint is served `private` anyway).
            # `viewed_by` (the authenticated email) is part of the key so an
            # admin's plate-bearing body can never be served to a different
            # user via a shared/conditional cache hit — belt-and-suspenders
            # alongside the per-response Vary: Authorization below.
            filter_key = "|".join((
                "+".join(sorted(tokens)),
                form_factor or "",
                spatial_status or "",
                "1" if include_outliers else "0",
                ",".join(repr(v) for v in bbox_vals) if bbox_vals else "",
                "plate" if include_plate else "",
                viewed_by or "",
            ))
            etag = f'W/"{resource}:{cycle_id}:{filter_key}"'
            # Authenticated responses vary by the bearer and (for admins) can
            # carry raw plates — a private cache must key on Authorization and
            # never silently reuse across tokens within a freshness window.
            extra_headers = {"Vary": "Authorization"} if viewed_by is not None else {}
            if _if_none_match_hit(request, etag):
                return Response(
                    status_code=304,
                    headers={"ETag": etag, "Cache-Control": cache_header, **extra_headers},
                )
            response.headers["ETag"] = etag
            response.headers["Cache-Control"] = cache_header
            for k, v in extra_headers.items():
                response.headers[k] = v

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

            if bbox_vals:
                min_lon, min_lat, max_lon, max_lat = bbox_vals
                where.append(
                    "r.longitude BETWEEN %s AND %s AND r.latitude BETWEEN %s AND %s"
                )
                params.extend([min_lon, max_lon, min_lat, max_lat])

            # has_negative_report: true iff there's a report against THIS
            # vehicle_identifier in the SAME h3_10 cell, ≤24h old — from
            # either report pipeline (map-pin negative_reports or the §3.1
            # rider device_reports). The flag goes "stale" (false here) the
            # moment the scooter moves to a different h3_10, even though the
            # report rows remain queryable from the private endpoints.
            sql = (
                "SELECT r.device_id, r.form_factor, r.latitude, r.longitude, r.spatial_status, "
                "       r.vehicle_identifier, r.is_disabled, r.is_reserved, "
                "       r.current_range_meters, r.propulsion_type, "
                "       r.h3_8_index, r.h3_9_index, r.h3_10_index, "
                "       r.range_percentile_by_type, r.range_rank_unique_by_type, "
                "       r.range_rank_all_by_type, r.range_rank_all_devices, "
                "       r.range_rank_h3_8_peers, r.range_rank_h3_9_peers, "
                "       r.range_rank_h3_10_peers, "
                "       (EXISTS ("
                "           SELECT 1 FROM negative_reports nr "
                "           WHERE nr.vehicle_identifier = r.vehicle_identifier "
                "             AND nr.h3_10_index = r.h3_10_index "
                "             AND nr.reported_at >= NOW() - INTERVAL '24 hours'"
                "       ) OR EXISTS ("
                "           SELECT 1 FROM device_reports dr "
                "           WHERE dr.vehicle_identifier = r.vehicle_identifier "
                "             AND dr.h3_10_index = r.h3_10_index "
                "             AND dr.reported_at >= NOW() - INTERVAL '24 hours'"
                # Parking complaints (improperly_parked) are excluded here:
                # they feed the compliance aggregate, not ride reliability.
                f"             AND {reliability_report_type_sql('dr')} "
                "       )) AS has_negative_report, "
                "       r.max_range_meters_for_type, "
                "       ds.number_failed_starts, ds.first_observed_at_location, "
                "       r.vehicle_use_type, r.vehicle_model_name, "
                "       r.vehicle_plate, ds.first_ever_observed_at, "
                "       ds.max_observed_range_meters, ds.max_observed_range_at, "
                # Crowdsourced equipment (sql/055). feature_status is on the
                # wire for EVERY device, unconditionally and without an
                # ?include= token: it is what the device card's "☑️ Confirm
                # Features" button reads to decide whether it is offering 12,
                # 14 or 6 points, so a client that has to opt in would be a
                # client that shows the wrong number. It is one short string
                # per device. The four feature columns ride along with it —
                # they are two booleans' worth of payload and are what any
                # equipment filter has to read.
                "       ds.feature_status, ds.has_bell, ds.has_cup_holder, "
                "       ds.has_phone_holder, ds.features_poor_condition "
                "FROM raw_telemetry_points r "
                "LEFT JOIN device_state ds USING (vehicle_identifier) "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY r.device_id"
            )
            cur.execute(sql, params)
            rows = cur.fetchall()

    # Peer-relative dwell stats are computed over the FULL denver_core
    # fleet (own query + per-cycle cache in src/dwell_stats.py), never the
    # filtered subset — a bbox request must not shrink anyone's peer set.
    dwell_stats = stats_for_cycle(cycle_id, snapshot_time)

    # The raw vehicle_plate is emitted ONLY when include_plate is set — i.e.
    # from /api/v1/user/devices/current for an admin session. On the public
    # path (include_plate=False) it stays off the wire, preserving the
    # public/private identifier split in src/identity.py (only the HMAC
    # vehicle_identifier is public). The last four SELECT columns are always
    # fetched but only emitted under include_plate (see the feature loop).
    features = []
    for r in rows:
        number_failed_starts = int(r[22]) if r[22] is not None else None
        dstat = dwell_stats.get(r[5])
        is_dwell_outlier = bool(dstat and dstat.is_outlier)
        quality = compute_quality_designation(
            current_range_meters=r[8],
            is_disabled=r[6],
            is_reserved=r[7],
            number_failed_starts=number_failed_starts,
            first_observed_at_location=r[23],
            has_negative_report=bool(r[20]),
            is_dwell_outlier=is_dwell_outlier,
        )
        reliability = compute_reliability_tier(
            number_failed_starts=number_failed_starts,
            first_observed_at_location=r[23],
            quality_designation=quality,
            has_negative_report=bool(r[20]),
            is_dwell_outlier=is_dwell_outlier,
            peer_median_dwell_hours=dstat.peer_median_hours if dstat else None,
        )
        properties: dict[str, Any] = {
            "device_id": r[0],
            "form_factor": r[1],
            "spatial_status": r[4],
            "vehicle_identifier": r[5],
            "is_disabled": r[6],
            "is_reserved": r[7],
            "current_range_meters": r[8],
            "battery_percent": compute_battery_percent(r[8]),
            "propulsion_type": r[9],
            "has_negative_report": bool(r[20]),
            "quality_designation": quality,
            "number_failed_starts": number_failed_starts,
            "first_observed_at_location": r[23].isoformat() if r[23] else None,
            "reliability_tier": reliability,
            "dwell_percentile_hood": (
                round(dstat.percentile * 100)
                if dstat and dstat.percentile is not None
                else None
            ),
            "dwell_peer_median_hours": (
                round(dstat.peer_median_hours, 1)
                if dstat and dstat.peer_median_hours is not None
                else None
            ),
            "vehicle_use_type": r[24],
            "vehicle_model_name": r[25],
            # sql/055. The LEFT JOIN means a device with no device_state row
            # yet (first cycle it was ever seen) reads NULL here, not the
            # column default — so the fallback is applied in Python too.
            # "Needs features confirmed" is the right answer for a vehicle
            # we have never even recorded state for.
            "feature_status": r[30] or FEATURE_STATUS_NEEDS_CONFIRMED,
            "device_features": feature_payload(r[31], r[32], r[33], r[34]),
        }
        if "h3" in tokens:
            # String-encoded (canonical h3 hex form): the raw 64-bit ints
            # exceed JS MAX_SAFE_INTEGER and silently lose precision in
            # JSON.parse.
            properties["h3_8_index"] = h3.int_to_str(int(r[10])) if r[10] is not None else None
            properties["h3_9_index"] = h3.int_to_str(int(r[11])) if r[11] is not None else None
            properties["h3_10_index"] = h3.int_to_str(int(r[12])) if r[12] is not None else None
        if "ranks" in tokens:
            for name, value in zip(_RANK_FIELDS, r[13:20]):
                properties[name] = value
        if include_plate:
            # Admin-only private fields (retired /private/devices/current).
            properties["vehicle_plate"] = r[26]
            properties["first_ever_observed_at"] = r[27].isoformat() if r[27] else None
            properties["max_observed_range_meters"] = r[28]
            properties["max_observed_range_at"] = r[29].isoformat() if r[29] else None
        features.append({
            "type": "Feature",
            "id": r[0],
            "geometry": {"type": "Point", "coordinates": [float(r[3]), float(r[2])]},
            "properties": properties,
        })

    metadata: dict[str, Any] = {
        "cycle_id": str(cycle_id),
        "snapshot_time": snapshot_time.isoformat(),
        "device_count": len(features),
        "filters": {
            "form_factor": form_factor,
            "spatial_status": spatial_status,
            "include_outliers": include_outliers,
            "bbox": bbox,
        },
        "include": sorted(tokens),
    }
    if viewed_by is not None:
        metadata["viewed_by"] = viewed_by
        metadata["admin"] = include_plate
    return {
        "type": "FeatureCollection",
        "metadata": metadata,
        "features": features,
    }


@router.get("/api/v1/devices/current")
def devices_current(
    request: Request,
    response: Response,
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
    include: str | None = Query(
        None,
        description='Comma-separated opt-in field groups: "ranks" (the seven range_rank_*/range_percentile_by_type fields), "h3" (h3_8/9/10_index, string-encoded).',
    ),
) -> Any:
    """GeoJSON FeatureCollection of every device's current position,
    drawn from the most recent successfully-completed observation cycle.

    Public and unauthenticated — no raw plates. Signed-in callers who want
    the admin-only fields use `/api/v1/user/devices/current` instead.

        const r = await fetch("/api/v1/devices/current");
        const geo = await r.json();
        map.getSource("devices").setData(geo);
    """
    return _devices_current_impl(
        request,
        response,
        form_factor=form_factor,
        spatial_status=spatial_status,
        include_outliers=include_outliers,
        bbox=bbox,
        include=include,
    )


# ---------------------------------------------------------------------------
# Equity estimate (server-side stand-in for the client's point-in-polygon pass)
# ---------------------------------------------------------------------------
@router.get("/api/v1/equity-estimate")
def equity_estimate(
    request: Request,
    response: Response,
    ranks: str = Query(
        ...,
        description="Comma-separated EquityGroupRank tiers to combine, e.g. '1,2' for a rank-≤-2 cutoff. Each in 1..6.",
    ),
) -> Any:
    """Device share inside the selected equity-rank tiers, from the most
    recent snapshot.

    The erN tiers partition the scored area (a device is in at most one),
    so combining ranks is a plain sum of the per-tier snapshot fields —
    the same numbers `/api/v1/snapshots/latest` carries, pre-combined here
    so weak clients don't have to run an 8k-point point-in-polygon pass
    (or download the full devices payload) to draw the cutoff gauge.
    """
    try:
        rank_list = sorted({int(t) for t in ranks.split(",") if t.strip()})
    except ValueError:
        raise HTTPException(400, detail="ranks must be comma-separated integers, e.g. '1,2'")
    if not rank_list or any(n < 1 or n > 6 for n in rank_list):
        raise HTTPException(400, detail="ranks must be within 1..6")

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM snapshot_metadata_core ORDER BY snapshot_time DESC LIMIT 1"
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(503, detail="no snapshots yet")
            snap = _row_to_dict(cur, row)

    # "+"-joined — _if_none_match_hit splits the header on commas, so an
    # ETag must never contain one.
    etag = f'W/"equity:{snap["cycle_id"]}:{"+".join(str(n) for n in rank_list)}"'
    if _if_none_match_hit(request, etag):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=60"})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=60"

    def _sum(prefix: str) -> int:
        return sum(int(snap.get(f"{prefix}_er{n}") or 0) for n in rank_list)

    def _pct(num: int, den: int | None) -> float | None:
        return round(100.0 * num / den, 2) if den else None

    total_devices = _sum("total_devices")
    total_bikes = _sum("total_bike")
    total_scooters = _sum("total_scooter")

    return {
        "cycle_id": str(snap["cycle_id"]),
        "snapshot_time": snap["snapshot_time"],
        "ranks": rank_list,
        "total_devices": total_devices,
        "total_bikes": total_bikes,
        "total_scooters": total_scooters,
        "percent_all_devices": _pct(total_devices, snap.get("total_devices_denver")),
        "percent_all_bikes": _pct(total_bikes, snap.get("total_bike_denver")),
        "percent_all_scooters": _pct(total_scooters, snap.get("total_scooter_denver")),
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
    # booleans should stay booleans (float() above would coerce, e.g.
    # float(True) == 1.0) — one column per compliance group (v1, v2 only;
    # er1..er6 are averages-only and have no stored pass/fail boolean).
    for k in _COMPLIANCE_PASS_COLUMNS:
        if k in d and d[k] is not None:
            d[k] = bool(d[k])
    return d


def _empty_daily_payload() -> dict[str, Any]:
    """The 'pending' shape for /latest before any daily SLA row exists.

    Mirrors the persisted row's keys with null values (snapshot_count 0) so
    the front-end compliance gauge can render a 'pending' state. The
    documented gauge does `v1Pct === null ? "pending" : v1Pct.toFixed(1)`
    (API.md → Common patterns), which only works if the field is present
    and null — a 503 / `{detail}` body leaves it `undefined` and the gauge
    crashes on `.toFixed()`.
    """
    payload: dict[str, Any] = {
        "sla_date": None,
        "window_start_ts": None,
        "window_end_ts": None,
        "snapshot_count": 0,
    }
    for f in _AVG_FIELDS:
        payload[f"avg_{f}"] = None
    for k in _COMPLIANCE_PASS_COLUMNS:
        payload[k] = None
    payload["computed_at"] = None
    return payload


@router.get("/api/v1/compliance/daily/latest")
def daily_compliance_latest() -> dict[str, Any]:
    """Most recent computed daily SLA window.

    Returns a null-filled 'pending' payload (HTTP 200) when no row has been
    computed yet — right after a deploy, or before the 9:00 AM Denver job
    first runs — so the front-end gauge degrades to a 'pending' state
    instead of erroring on a 503 it doesn't expect.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM daily_sla_compliance ORDER BY sla_date DESC LIMIT 1"
            )
            row = cur.fetchone()
            if not row:
                return _empty_daily_payload()
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
