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
from .ingest import IngestPayload, TaggedDevice
from .pg import connection

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


def _core_summary_sql() -> str:
    return """
    WITH d AS (
        SELECT * FROM points WHERE spatial_status = 'denver_core'
    ),
    v1_devices AS (
        SELECT DISTINCT p.device_id, p.form_factor
        FROM d p
        JOIN boundaries b ON b.region_type = 'v1' AND ST_Within(p.geom, b.geom)
    ),
    v2_devices AS (
        SELECT DISTINCT p.device_id, p.form_factor
        FROM d p
        JOIN boundaries b ON b.region_type = 'v2' AND ST_Within(p.geom, b.geom)
    ),
    totals AS (
        SELECT
            (SELECT COUNT(*) FROM points)                       AS total_devices_all,
            (SELECT COUNT(*) FROM points WHERE spatial_status <> 'denver_core') AS total_not_in_denver,
            (SELECT COUNT(*) FROM d)                             AS total_devices_denver,
            (SELECT COUNT(*) FROM d WHERE form_factor = 'bicycle') AS total_bike_denver,
            (SELECT COUNT(*) FROM d WHERE form_factor = 'scooter') AS total_scooter_denver,
            (SELECT COUNT(*) FROM v1_devices)                    AS total_devices_v1,
            (SELECT COUNT(*) FROM v1_devices WHERE form_factor = 'bicycle') AS total_bike_v1,
            (SELECT COUNT(*) FROM v1_devices WHERE form_factor = 'scooter') AS total_scooter_v1,
            (SELECT COUNT(*) FROM v2_devices)                    AS total_devices_v2,
            (SELECT COUNT(*) FROM v2_devices WHERE form_factor = 'bicycle') AS total_bike_v2,
            (SELECT COUNT(*) FROM v2_devices WHERE form_factor = 'scooter') AS total_scooter_v2
    )
    SELECT
        total_devices_denver,
        total_devices_v1,
        total_devices_v2,
        total_bike_denver,
        total_bike_v1,
        total_bike_v2,
        total_scooter_denver,
        total_scooter_v1,
        total_scooter_v2,
        total_not_in_denver,
        ROUND(total_devices_v1::DOUBLE / NULLIF(total_devices_denver,0) * 100, 2)  AS percent_all_devices_v1,
        ROUND(total_devices_v2::DOUBLE / NULLIF(total_devices_denver,0) * 100, 2)  AS percent_all_devices_v2,
        ROUND(total_bike_v1::DOUBLE / NULLIF(total_bike_denver,0) * 100, 2)        AS percent_all_bikes_v1,
        ROUND(total_bike_v2::DOUBLE / NULLIF(total_bike_denver,0) * 100, 2)        AS percent_all_bikes_v2,
        ROUND(total_scooter_v1::DOUBLE / NULLIF(total_scooter_denver,0) * 100, 2)  AS percent_all_scooters_v1,
        ROUND(total_scooter_v2::DOUBLE / NULLIF(total_scooter_denver,0) * 100, 2)  AS percent_all_scooters_v2,
        ROUND(total_bike_denver::DOUBLE / NULLIF(total_devices_denver,0) * 100, 2) AS percent_bikes_denver,
        ROUND(total_scooter_denver::DOUBLE / NULLIF(total_devices_denver,0) * 100, 2) AS percent_scooters_denver,
        ROUND(total_bike_v1::DOUBLE / NULLIF(total_devices_v1,0) * 100, 2)         AS percent_bikes_v1,
        ROUND(total_scooter_v1::DOUBLE / NULLIF(total_devices_v1,0) * 100, 2)      AS percent_scooters_v1,
        ROUND(total_bike_v2::DOUBLE / NULLIF(total_devices_v2,0) * 100, 2)         AS percent_bikes_v2,
        ROUND(total_scooter_v2::DOUBLE / NULLIF(total_devices_v2,0) * 100, 2)      AS percent_scooters_v2
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

        # Fetch core summary
        core = con.execute(_core_summary_sql()).fetchone()
        core_cols = [
            "total_devices_denver", "total_devices_v1", "total_devices_v2",
            "total_bike_denver", "total_bike_v1", "total_bike_v2",
            "total_scooter_denver", "total_scooter_v1", "total_scooter_v2",
            "total_not_in_denver",
            "percent_all_devices_v1", "percent_all_devices_v2",
            "percent_all_bikes_v1", "percent_all_bikes_v2",
            "percent_all_scooters_v1", "percent_all_scooters_v2",
            "percent_bikes_denver", "percent_scooters_denver",
            "percent_bikes_v1", "percent_scooters_v1",
            "percent_bikes_v2", "percent_scooters_v2",
        ]
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

    # Raw rows (no spatial join needed — copy-through)
    raw_rows = [
        {
            "cycle_id": str(cycle_id),
            "snapshot_time": snapshot_time,
            "device_id": d.device_id,
            "form_factor": d.form_factor,
            "latitude": d.lat,
            "longitude": d.lon,
            "spatial_status": d.spatial_status,
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
            cur.execute(
                """
                INSERT INTO snapshot_metadata_core (
                    cycle_id, snapshot_time,
                    total_devices_denver, total_devices_v1, total_devices_v2,
                    total_bike_denver, total_bike_v1, total_bike_v2,
                    total_scooter_denver, total_scooter_v1, total_scooter_v2,
                    total_not_in_denver,
                    percent_all_devices_v1, percent_all_devices_v2,
                    percent_all_bikes_v1, percent_all_bikes_v2,
                    percent_all_scooters_v1, percent_all_scooters_v2,
                    percent_bikes_denver, percent_scooters_denver,
                    percent_bikes_v1, percent_scooters_v1,
                    percent_bikes_v2, percent_scooters_v2
                ) VALUES (
                    %(cycle_id)s, %(snapshot_time)s,
                    %(total_devices_denver)s, %(total_devices_v1)s, %(total_devices_v2)s,
                    %(total_bike_denver)s, %(total_bike_v1)s, %(total_bike_v2)s,
                    %(total_scooter_denver)s, %(total_scooter_v1)s, %(total_scooter_v2)s,
                    %(total_not_in_denver)s,
                    %(percent_all_devices_v1)s, %(percent_all_devices_v2)s,
                    %(percent_all_bikes_v1)s, %(percent_all_bikes_v2)s,
                    %(percent_all_scooters_v1)s, %(percent_all_scooters_v2)s,
                    %(percent_bikes_denver)s, %(percent_scooters_denver)s,
                    %(percent_bikes_v1)s, %(percent_scooters_v1)s,
                    %(percent_bikes_v2)s, %(percent_scooters_v2)s
                )
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
                    " latitude, longitude, spatial_status) FROM STDIN"
                ) as copy:
                    for r in result.raw_rows:
                        copy.write_row([
                            r["cycle_id"], r["snapshot_time"], r["device_id"],
                            r["form_factor"], r["latitude"], r["longitude"],
                            r["spatial_status"],
                        ])
        conn.commit()
