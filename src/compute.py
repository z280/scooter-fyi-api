"""DuckDB spatial join: tagged-device points × boundary layers → core + narrow rows."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg

from .config import BoundaryLayer, load
from .duck import session
from .equity_groups import TRACKED_GROUPS, core_metric_columns
from .ingest import IngestPayload, TaggedDevice
from .pg import connection
from .ranking import compute_range_rankings

log = logging.getLogger(__name__)


@dataclass
class ComputeResult:
    core_row: dict
    regional_rows: list[dict]
    raw_rows: list[dict]


# ---------------------------------------------------------------------------
# Boundary loading
# ---------------------------------------------------------------------------
_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")


def _name_from_props(layer: BoundaryLayer, props: dict, ordinal: int) -> str:
    if layer.name_strategy == "ordinal":
        return f"{layer.name_prefix}{ordinal:03d}"
    if layer.name_strategy == "field":
        val = props.get(layer.name_field) if layer.name_field else None
        if val is None or val == "":
            return f"{layer.name_prefix}{ordinal:03d}"
        return f"{layer.name_prefix}{val}"
    if layer.name_strategy == "field_alnum":
        val = props.get(layer.name_field) if layer.name_field else None
        if not val:
            return f"{layer.name_prefix}{ordinal:03d}"
        cleaned = _ALNUM_RE.sub("", str(val))
        return f"{layer.name_prefix}{cleaned}"
    raise ValueError(f"unknown name_strategy: {layer.name_strategy}")


def _load_boundaries_into_duck(con) -> None:
    """Materialize a single ``boundaries`` table:
       (region_category TEXT, region_type TEXT, region_name TEXT, geom GEOMETRY).

    Uses ST_Read for each layer file, derives region_name in SQL-then-Python
    via a temporary view per layer, then UNION ALL into a single table.
    """
    cfg = load()

    con.execute("DROP TABLE IF EXISTS boundaries;")
    con.execute(
        """
        CREATE TABLE boundaries (
            region_category TEXT NOT NULL,
            region_type TEXT NOT NULL,
            region_name TEXT NOT NULL,
            geom GEOMETRY NOT NULL
        );
        """
    )

    for layer in cfg.boundaries:
        # ST_Read returns one row per feature with property columns + geom
        # We add an ordinal via row_number().
        # Filter rule: drop rows where filter_nonnull_field IS NULL (e.g. CD At-Large).
        view = f"_layer_{layer.region_type}"
        con.execute(f"DROP VIEW IF EXISTS {view};")
        con.execute(
            f"CREATE TEMP VIEW {view} AS "
            f"SELECT row_number() OVER () AS _ordinal, * FROM ST_Read('{layer.file}');"
        )

        # Fetch the property values needed to derive region_name into Python.
        # (DuckDB's spatial reader exposes properties as top-level columns.)
        select_cols = ["_ordinal"]
        if layer.name_field:
            select_cols.append(layer.name_field)
        if layer.filter_nonnull_field and layer.filter_nonnull_field not in select_cols:
            select_cols.append(layer.filter_nonnull_field)
        rows = con.execute(f"SELECT {', '.join(select_cols)} FROM {view}").fetchall()
        col_idx = {name: i for i, name in enumerate(select_cols)}

        # For each feature, INSERT one row into boundaries with a Python-derived name.
        # Use parameterized INSERT to avoid quoting issues.
        for r in rows:
            ordinal = int(r[col_idx["_ordinal"]])
            if layer.filter_nonnull_field:
                fv = r[col_idx[layer.filter_nonnull_field]]
                if fv is None:
                    continue
            props: dict = {}
            if layer.name_field:
                props[layer.name_field] = r[col_idx[layer.name_field]]
            name = _name_from_props(layer, props, ordinal)
            con.execute(
                f"INSERT INTO boundaries (region_category, region_type, region_name, geom) "
                f"SELECT ?, ?, ?, geom FROM {view} WHERE _ordinal = ?",
                [layer.region_category, layer.region_type, name, ordinal],
            )

        con.execute(f"DROP VIEW {view};")

    con.execute("CREATE INDEX IF NOT EXISTS idx_b_geom ON boundaries USING RTREE (geom);")

    # Build the precise Denver city polygon as the union of all neighborhood
    # polygons. NB covers all of Denver and aligns with the city boundary, so
    # this is more accurate than the rough bbox envelope from src/ingest.py
    # (which catches devices in Aurora, Lakewood, the Veo repair shop, etc.).
    # The bbox check stays as a fast first-pass; this is the precise final word.
    con.execute(
        """
        DROP TABLE IF EXISTS denver_city;
        CREATE TABLE denver_city AS
        SELECT ST_Union_Agg(geom) AS geom
        FROM boundaries
        WHERE region_type = 'neighborhood';
        """
    )


def _refine_spatial_status(con) -> int:
    """Re-classify any point currently tagged 'denver_core' (by the bbox
    envelope) that is actually outside the Denver city polygon. These get
    re-tagged 'other_outlier' — they're in the rectangle but not in the
    actual city (typically: Aurora, Lakewood, repair shops outside city
    limits). Returns the number of devices re-tagged."""
    cur = con.execute(
        """
        UPDATE points
        SET spatial_status = 'other_outlier'
        WHERE spatial_status = 'denver_core'
          AND NOT EXISTS (
              SELECT 1 FROM denver_city c WHERE ST_Within(points.geom, c.geom)
          );
        """
    )
    # DuckDB returns affected row count via the cursor's rowcount-like result
    try:
        result = cur.fetchall()
        if result and result[0]:
            return int(result[0][0])
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# Per-cycle compute
# ---------------------------------------------------------------------------
def _load_points_into_duck(con, devices: list[TaggedDevice], snapshot_time: datetime) -> None:
    con.execute(
        """
        DROP TABLE IF EXISTS points;
        CREATE TABLE points (
            device_id     TEXT NOT NULL,
            form_factor   TEXT NOT NULL,
            lat           DOUBLE NOT NULL,
            lon           DOUBLE NOT NULL,
            spatial_status TEXT NOT NULL,
            geom GEOMETRY
        );
        """
    )

    if not devices:
        return

    # Batched executemany
    rows = [
        (d.device_id, d.form_factor, d.lat, d.lon, d.spatial_status)
        for d in devices
    ]
    con.executemany(
        "INSERT INTO points (device_id, form_factor, lat, lon, spatial_status, geom) "
        "VALUES (?, ?, ?, ?, ?, ST_Point(?, ?))",
        [(r[0], r[1], r[2], r[3], r[4], r[3], r[2]) for r in rows],
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_p_geom ON points USING RTREE (geom);")


def _core_summary_sql(groups: tuple[str, ...] = TRACKED_GROUPS) -> str:
    """Generalized over `groups` (today: v1, v2, er1..er6 — see
    src/equity_groups.py). Each group gets its own device-membership CTE
    (a device counts once per group even if it straddles multiple of the
    group's boundary features), three raw totals, and five percentages.
    A group absent from the DuckDB `boundaries` table (e.g. in a test
    fixture that only loads a subset of layers) simply yields zero counts
    — the JOIN just finds no matching boundary rows, not an error.

    The final SELECT list is driven by `core_metric_columns()` — NOT
    hand-ordered here — so its column order is always exactly what
    `run_cycle()` expects when it zips the (positional) result tuple
    against that same column list. Hand-maintaining two independently
    ordered lists is exactly the kind of thing that silently drifts and
    corrupts a metric one column over.
    """
    group_ctes = ",\n    ".join(
        f"""{g}_devices AS (
        SELECT DISTINCT p.device_id, p.form_factor
        FROM d p
        JOIN boundaries b ON b.region_type = '{g}' AND ST_Within(p.geom, b.geom)
    )"""
        for g in groups
    )
    group_total_exprs = ",\n            ".join(
        f"(SELECT COUNT(*) FROM {g}_devices) AS total_devices_{g},\n"
        f"            (SELECT COUNT(*) FROM {g}_devices WHERE form_factor = 'bicycle') AS total_bike_{g},\n"
        f"            (SELECT COUNT(*) FROM {g}_devices WHERE form_factor = 'scooter') AS total_scooter_{g}"
        for g in groups
    )

    # column_name -> SQL expression, for every column core_metric_columns()
    # will ask for. Totals/total_not_in_denver pass straight through from
    # the `totals` CTE; percentages are computed here from it.
    exprs: dict[str, str] = {
        "total_devices_denver": "total_devices_denver",
        "total_bike_denver": "total_bike_denver",
        "total_scooter_denver": "total_scooter_denver",
        "total_not_in_denver": "total_not_in_denver",
        "percent_bikes_denver":
            "ROUND(total_bike_denver::DOUBLE / NULLIF(total_devices_denver,0) * 100, 2)",
        "percent_scooters_denver":
            "ROUND(total_scooter_denver::DOUBLE / NULLIF(total_devices_denver,0) * 100, 2)",
    }
    for g in groups:
        exprs[f"total_devices_{g}"] = f"total_devices_{g}"
        exprs[f"total_bike_{g}"] = f"total_bike_{g}"
        exprs[f"total_scooter_{g}"] = f"total_scooter_{g}"
        exprs[f"percent_all_devices_{g}"] = (
            f"ROUND(total_devices_{g}::DOUBLE / NULLIF(total_devices_denver,0) * 100, 2)"
        )
        exprs[f"percent_all_bikes_{g}"] = (
            f"ROUND(total_bike_{g}::DOUBLE / NULLIF(total_bike_denver,0) * 100, 2)"
        )
        exprs[f"percent_all_scooters_{g}"] = (
            f"ROUND(total_scooter_{g}::DOUBLE / NULLIF(total_scooter_denver,0) * 100, 2)"
        )
        exprs[f"percent_bikes_{g}"] = (
            f"ROUND(total_bike_{g}::DOUBLE / NULLIF(total_devices_{g},0) * 100, 2)"
        )
        exprs[f"percent_scooters_{g}"] = (
            f"ROUND(total_scooter_{g}::DOUBLE / NULLIF(total_devices_{g},0) * 100, 2)"
        )

    select_list = ",\n        ".join(
        f"{exprs[col]} AS {col}" for col in core_metric_columns(groups)
    )

    return f"""
    WITH d AS (
        SELECT * FROM points WHERE spatial_status = 'denver_core'
    ),
    {group_ctes},
    totals AS (
        SELECT
            (SELECT COUNT(*) FROM points)                       AS total_devices_all,
            (SELECT COUNT(*) FROM points WHERE spatial_status <> 'denver_core') AS total_not_in_denver,
            (SELECT COUNT(*) FROM d)                             AS total_devices_denver,
            (SELECT COUNT(*) FROM d WHERE form_factor = 'bicycle') AS total_bike_denver,
            (SELECT COUNT(*) FROM d WHERE form_factor = 'scooter') AS total_scooter_denver,
            {group_total_exprs}
    )
    SELECT
        {select_list}
    FROM totals
    """


def _regional_breakdown_sql() -> str:
    return """
    WITH d AS (
        SELECT * FROM points WHERE spatial_status = 'denver_core'
    )
    SELECT
        b.region_category,
        b.region_type,
        b.region_name,
        COUNT(p.device_id)                                                AS count_total,
        SUM(CASE WHEN p.form_factor = 'bicycle' THEN 1 ELSE 0 END)::INT   AS count_bikes,
        SUM(CASE WHEN p.form_factor = 'scooter' THEN 1 ELSE 0 END)::INT   AS count_scooters
    FROM boundaries b
    LEFT JOIN d p ON ST_Within(p.geom, b.geom)
    GROUP BY b.region_category, b.region_type, b.region_name
    """


def run_cycle(cycle_id: uuid.UUID, ingest: IngestPayload, snapshot_time: datetime) -> ComputeResult:
    """Spatial-join phase. Returns the rows ready to write to Postgres."""
    with session() as con:
        _load_boundaries_into_duck(con)
        _load_points_into_duck(con, ingest.devices, snapshot_time)

        # Refine the bbox-based denver_core tag against the actual city polygon
        # (union of NB features). Devices in the bbox but outside the polygon
        # — e.g. the Veo repair shop, parts of Aurora/Lakewood — get re-tagged
        # other_outlier and excluded from all citywide metrics.
        _refine_spatial_status(con)

        # Pull corrected statuses back so raw_telemetry_points reflects the
        # polygon-precise classification, not just the ingest-time bbox tag.
        corrected_status: dict[str, str] = {
            row[0]: row[1]
            for row in con.execute("SELECT device_id, spatial_status FROM points").fetchall()
        }

        # Fetch core summary
        core = con.execute(_core_summary_sql()).fetchone()
        core_cols = core_metric_columns()
        core_row = {
            "cycle_id": str(cycle_id),
            "snapshot_time": snapshot_time,
            **{c: (None if v is None else (float(v) if "percent" in c else int(v))) for c, v in zip(core_cols, core)},
        }

        # Fetch regional breakdown
        regional_rows = []
        for row in con.execute(_regional_breakdown_sql()).fetchall():
            regional_rows.append({
                "cycle_id": str(cycle_id),
                "snapshot_time": snapshot_time,
                "region_category": row[0],
                "region_type": row[1],
                "region_name": row[2],
                "count_total": int(row[3] or 0),
                "count_bikes": int(row[4] or 0),
                "count_scooters": int(row[5] or 0),
            })

    # Raw rows use the polygon-corrected spatial_status from DuckDB; fall back
    # to the ingest-time tag if the device wasn't loaded into DuckDB (e.g.
    # missing coords stripped during tagging — shouldn't happen but defensive).
    rankings = compute_range_rankings(ingest.devices)
    raw_rows = [
        {
            "cycle_id": str(cycle_id),
            "snapshot_time": snapshot_time,
            "device_id": d.device_id,
            "form_factor": d.form_factor,
            "latitude": d.lat,
            "longitude": d.lon,
            "spatial_status": corrected_status.get(d.device_id, d.spatial_status),
            "vehicle_plate": d.vehicle_plate,
            "vehicle_identifier": d.vehicle_identifier,
            "h3_8_index": d.h3_8_index,
            "h3_9_index": d.h3_9_index,
            "h3_10_index": d.h3_10_index,
            "is_disabled": d.is_disabled,
            "is_reserved": d.is_reserved,
            "current_range_meters": d.current_range_meters,
            "propulsion_type": d.propulsion_type,
            "max_range_meters_for_type": d.max_range_meters_for_type,
            **rankings.get(d.device_id, {}),
        }
        for d in ingest.devices
    ]

    return ComputeResult(core_row=core_row, regional_rows=regional_rows, raw_rows=raw_rows)


# ---------------------------------------------------------------------------
# Postgres writes (one transaction)
# ---------------------------------------------------------------------------
def write_to_postgres(result: ComputeResult) -> None:
    core = result.core_row
    with connection() as conn:
        with conn.cursor() as cur:
            # Built from `core`'s own keys (cycle_id, snapshot_time, plus
            # every core_metric_columns() name) rather than a hand-listed
            # column/placeholder pair — the two lists drifting apart is
            # exactly how a metric silently lands in the wrong column.
            insert_cols = list(core.keys())
            placeholders = ", ".join(f"%({c})s" for c in insert_cols)
            cur.execute(
                f"""
                INSERT INTO snapshot_metadata_core ({", ".join(insert_cols)})
                VALUES ({placeholders})
                """,
                core,
            )

            if result.regional_rows:
                cur.executemany(
                    """
                    INSERT INTO regional_metrics_narrow (
                        cycle_id, snapshot_time, region_category, region_type,
                        region_name, count_total, count_bikes, count_scooters
                    ) VALUES (
                        %(cycle_id)s, %(snapshot_time)s, %(region_category)s,
                        %(region_type)s, %(region_name)s, %(count_total)s,
                        %(count_bikes)s, %(count_scooters)s
                    )
                    ON CONFLICT (cycle_id, region_type, region_name) DO NOTHING
                    """,
                    result.regional_rows,
                )

            if result.raw_rows:
                with cur.copy(
                    "COPY raw_telemetry_points "
                    "(cycle_id, snapshot_time, device_id, form_factor, "
                    " latitude, longitude, spatial_status, vehicle_plate, "
                    " vehicle_identifier, is_disabled, is_reserved, "
                    " current_range_meters, propulsion_type, "
                    " max_range_meters_for_type, "
                    " h3_8_index, h3_9_index, h3_10_index, "
                    " range_percentile_by_type, range_rank_unique_by_type, "
                    " range_rank_all_by_type, range_rank_all_devices, "
                    " range_rank_h3_8_peers, range_rank_h3_9_peers, "
                    " range_rank_h3_10_peers) FROM STDIN"
                ) as copy:
                    for r in result.raw_rows:
                        copy.write_row([
                            r["cycle_id"], r["snapshot_time"], r["device_id"],
                            r["form_factor"], r["latitude"], r["longitude"],
                            r["spatial_status"], r["vehicle_plate"],
                            r["vehicle_identifier"], r["is_disabled"],
                            r["is_reserved"], r["current_range_meters"],
                            r["propulsion_type"],
                            r["max_range_meters_for_type"],
                            r["h3_8_index"], r["h3_9_index"], r["h3_10_index"],
                            r.get("range_percentile_by_type"),
                            r.get("range_rank_unique_by_type"),
                            r.get("range_rank_all_by_type"),
                            r.get("range_rank_all_devices"),
                            r.get("range_rank_h3_8_peers"),
                            r.get("range_rank_h3_9_peers"),
                            r.get("range_rank_h3_10_peers"),
                        ])
        conn.commit()
