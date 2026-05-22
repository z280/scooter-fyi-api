"""Observation-cycle lifecycle state machine (spec §3 + §5)."""

from __future__ import annotations

import json
import logging
import traceback
import uuid
from datetime import datetime, timezone

import psycopg

from . import compute, ingest, transmit
from .pg import connection
from .sentry import capture_exception, set_cycle_tag

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _start_cycle() -> uuid.UUID:
    cycle_id = uuid.uuid4()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO observation_cycles (cycle_id, start_ts, job_status) "
                "VALUES (%s, %s, 'in_progress')",
                (str(cycle_id), _now()),
            )
        conn.commit()
    set_cycle_tag(str(cycle_id))
    return cycle_id


def _set_status(cycle_id: uuid.UUID, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    params = dict(fields)
    params["cycle_id"] = str(cycle_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE observation_cycles SET {sets} WHERE cycle_id = %(cycle_id)s",
                params,
            )
        conn.commit()


def _log_api_failure(
    cycle_id: uuid.UUID | None,
    failure_type: str,
    http_status: int | None,
    error_details: str,
) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO api_failures
                    (cycle_id, failure_type, http_status_code, error_details)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    str(cycle_id) if cycle_id else None,
                    failure_type,
                    http_status,
                    error_details[:8000] if error_details else None,
                ),
            )
        conn.commit()


def run_once() -> str | None:
    """Drive a single cycle end-to-end. Returns cycle_id on success/abort."""
    cycle_id = _start_cycle()
    snapshot_time = _now()

    try:
        # ----- ingest --------------------------------------------------------
        try:
            payload, vt_map = ingest.fetch_gbfs()
        except ingest.UpstreamError as e:
            log.warning("upstream failure: %s", e)
            _set_status(
                cycle_id,
                job_status="upstream_failure",
                errors=str(e),
            )
            _log_api_failure(cycle_id, e.kind, e.http_status, str(e))
            return str(cycle_id)

        tagged = ingest.tag_envelope(payload, vt_map)
        _set_status(
            cycle_id,
            data_received_ts=_now(),
            gbfs_last_updated=tagged.last_updated,
            gbfs_payload_sha256=tagged.payload_sha256,
        )

        # ----- staleness check ----------------------------------------------
        if ingest.is_stale(tagged.last_updated, tagged.payload_sha256):
            log.info(
                "cycle %s aborted: GBFS payload unchanged from previous cycle",
                cycle_id,
            )
            _set_status(cycle_id, job_status="stale_aborted")
            _log_api_failure(
                cycle_id,
                "stale_data",
                None,
                f"GBFS last_updated={tagged.last_updated} sha={tagged.payload_sha256[:12]} "
                "matched previous cycle — no new data to process.",
            )
            return str(cycle_id)

        # ----- compute -------------------------------------------------------
        result = compute.run_cycle(cycle_id, tagged, snapshot_time)
        _set_status(cycle_id, processing_complete_ts=_now())

        # ----- write ---------------------------------------------------------
        compute.write_to_postgres(result)
        _set_status(
            cycle_id,
            data_storage_complete_ts=_now(),
            data_json_blob=json.dumps(
                {**result.core_row, "snapshot_time": result.core_row["snapshot_time"].isoformat()},
                default=str,
            ),
        )

        # ----- transmit ------------------------------------------------------
        tx_status = transmit.fanout(cycle_id, result.core_row)
        _set_status(
            cycle_id,
            transmission_ts=_now(),
            transmission_status=tx_status,
            job_status="complete",
        )

        log.info(
            "cycle %s complete: denver=%d v1=%d v2=%d",
            cycle_id,
            result.core_row.get("total_devices_denver") or 0,
            result.core_row.get("total_devices_v1") or 0,
            result.core_row.get("total_devices_v2") or 0,
        )
        return str(cycle_id)

    except Exception as e:  # noqa: BLE001 — top-level safety net
        tb = traceback.format_exc()
        log.exception("cycle %s internal failure", cycle_id)
        capture_exception(e)
        _set_status(cycle_id, job_status="internal_failure", errors=tb[:8000])
        return str(cycle_id)
