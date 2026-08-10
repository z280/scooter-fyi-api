"""Fleet size over time (sql/069) — the Tools drawer's "Devices over time".

    GET /api/v1/devices/history/hourly?days=1..14

Public: it is the same aggregate fleet count the map footer and the
compliance gauge already publish, just over time — no per-device data, no
plates, no positions.

Each hour is the LAST cycle observed in that hour (a sample on the hour,
not an average — with ~90s cycles the difference is noise, and a sample
keeps the model breakdown coherent instead of averaging JSON). Hours the
new snapshot table doesn't cover — history from before sql/069 deployed,
or an ingest outage — fall back to snapshot_metadata_core's per-cycle
total (recorded since day one), with the status/model breakdowns null for
those hours: an honest "we know how many, not what state" rather than
zeros that would chart a fleet collapse that never happened.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from .pg import connection

router = APIRouter()

MAX_DAYS = 14


@router.get("/api/v1/devices/history/hourly")
def device_history_hourly(
    days: int = Query(14, ge=1, le=MAX_DAYS),
) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (date_trunc('hour', snapshot_time))
                       date_trunc('hour', snapshot_time) AS hour,
                       total, available, reserved, out_of_service,
                       models_available
                FROM device_status_snapshots
                WHERE snapshot_time >= NOW() - make_interval(days => %s)
                ORDER BY date_trunc('hour', snapshot_time),
                         snapshot_time DESC
                """,
                (days,),
            )
            rows = {
                hour: {
                    "hour": hour.isoformat(),
                    "total": int(total),
                    "available": int(available),
                    "reserved": int(reserved),
                    "out_of_service": int(oos),
                    "models_available": models or {},
                }
                for hour, total, available, reserved, oos, models
                in cur.fetchall()
            }

            # Backfill hours the snapshot table doesn't cover from the
            # core metrics' per-cycle totals (see the module docstring).
            cur.execute(
                """
                SELECT DISTINCT ON (date_trunc('hour', snapshot_time))
                       date_trunc('hour', snapshot_time) AS hour,
                       total_devices_denver
                FROM snapshot_metadata_core
                WHERE snapshot_time >= NOW() - make_interval(days => %s)
                      AND total_devices_denver IS NOT NULL
                ORDER BY date_trunc('hour', snapshot_time),
                         snapshot_time DESC
                """,
                (days,),
            )
            for hour, total in cur.fetchall():
                if hour not in rows:
                    rows[hour] = {
                        "hour": hour.isoformat(),
                        "total": int(total),
                        "available": None,
                        "reserved": None,
                        "out_of_service": None,
                        "models_available": None,
                    }

    return {
        "days": days,
        "hours": [rows[h] for h in sorted(rows)],
    }
