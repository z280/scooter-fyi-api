"""48-hour cold-storage flush: raw_telemetry_points → Parquet → Cloudflare R2 → TRUNCATE."""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.client import Config as BotoConfig

from .config import load, r2_credentials
from .duck import session
from .pg import connection
from .sentry import capture_exception

log = logging.getLogger(__name__)


def _set_system_state(key: str, value: str) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE
                  SET value = EXCLUDED.value, updated_at = NOW()
                """,
                (key, value),
            )
        conn.commit()


def _log_failure(detail: str) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO api_failures (failure_type, error_details)
                VALUES ('archive_upload_failed', %s)
                """,
                (detail[:8000],),
            )
        conn.commit()


def _export_to_parquet(target: Path) -> int:
    """Use DuckDB to pull the raw table into a Parquet file. Returns row count."""
    pg = {
        "host": os.environ.get("POSTGRES_HOST", "denver_spatial_db"),
        "port": os.environ.get("POSTGRES_PORT", "5432"),
        "user": os.environ["POSTGRES_USER"],
        "password": os.environ["POSTGRES_PASSWORD"],
        "db": os.environ["POSTGRES_DB"],
    }
    dsn = (
        f"host={pg['host']} port={pg['port']} user={pg['user']} "
        f"password={pg['password']} dbname={pg['db']}"
    )
    with session() as con:
        # DuckDB budgets ~80% of HOST RAM, not the container's cgroup limit —
        # uncapped, this COPY was OOM-killed by the kernel on a large table
        # (the 38M-row backlog incident, f34e4e2). Cap it well under the
        # scheduler's 1 GiB and let the export spill to disk instead. Row
        # order in the archive is irrelevant, so insertion order is off.
        con.execute("SET memory_limit='600MB';")
        con.execute("SET threads=2;")
        con.execute("SET temp_directory='/tmp/duck_spill';")
        con.execute("SET preserve_insertion_order=false;")
        con.execute("INSTALL postgres; LOAD postgres;")
        con.execute(f"ATTACH '{dsn}' AS pgsrc (TYPE POSTGRES, READ_ONLY);")
        count = con.execute(
            "SELECT COUNT(*) FROM pgsrc.public.raw_telemetry_points"
        ).fetchone()[0]
        con.execute(
            f"COPY (SELECT * FROM pgsrc.public.raw_telemetry_points) "
            f"TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD);"
        )
    return int(count or 0)


def _upload_to_r2(local: Path, key: str) -> bool:
    creds = r2_credentials()
    if not creds:
        log.warning("R2 credentials absent — skipping upload")
        return False
    cfg = load().r2
    endpoint = cfg.endpoint_template.format(account_id=creds["account_id"])
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=creds["access_key_id"],
        aws_secret_access_key=creds["secret_access_key"],
        config=BotoConfig(signature_version="s3v4"),
        region_name="auto",
    )
    client.upload_file(str(local), creds["bucket"], key)
    return True


def run_archive() -> dict:
    """Top-level entry for the 48-hour APScheduler job."""
    started = datetime.now(timezone.utc)
    job_id = str(uuid.uuid4())
    log.info("archive job %s starting", job_id)

    try:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / f"raw_{started:%Y%m%dT%H%M%SZ}.parquet"
            count = _export_to_parquet(local)
            if count == 0:
                log.info("archive: nothing to upload (raw table empty)")
                _set_system_state(
                    "last_archive_attempt",
                    f"{started.isoformat()}|empty|0",
                )
                return {"job_id": job_id, "rows": 0, "uploaded": False}

            key = f"raw/{started:%Y/%m/%d/}{local.name}"
            ok = _upload_to_r2(local, key)
            if not ok:
                _log_failure("R2 credentials absent or upload rejected")
                return {"job_id": job_id, "rows": count, "uploaded": False}

        # Only truncate after successful upload
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE raw_telemetry_points;")
            conn.commit()

        _set_system_state(
            "last_archive_ts",
            datetime.now(timezone.utc).isoformat(),
        )
        log.info("archive job %s complete: %d rows uploaded → %s", job_id, count, key)
        return {"job_id": job_id, "rows": count, "uploaded": True, "key": key}

    except Exception as e:  # noqa: BLE001
        capture_exception(e)
        _log_failure(f"archive job {job_id} failed: {e}")
        log.exception("archive job %s failed", job_id)
        return {"job_id": job_id, "error": str(e)}
