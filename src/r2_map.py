"""Pull the routing assets denver-map-prep publishes to R2 into a local volume.

Valhalla's container entrypoint fetches PBFs over plain HTTP via ``tile_urls``
and cannot sign S3 requests, so it can't read a private R2 bucket directly.
Rather than making the bucket public, this module runs as a one-shot sidecar
(``python -m src.cli fetch_map_pbf``) that downloads with SigV4 into the volume
Valhalla builds from.

Two objects are synced:

* ``denver_scooter_custom.pbf``     — the routing graph input.
* ``denver_canopy_coverage.csv.gz`` — way_id -> tree-canopy fraction, which the
  ``shade`` routing profile scores alternates against (Valhalla has no
  request-tunable shade lever, so this happens in the API).

Downloads are ETag-gated: an unchanged object is skipped, so the sidecar is
cheap to run on every container start and the caller can tell whether a Valhalla
tile rebuild is actually warranted.
"""

from __future__ import annotations

import csv
import gzip
import logging
from pathlib import Path

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from .config import load, r2_map_credentials

log = logging.getLogger(__name__)


def _client(creds: dict[str, str]):
    cfg = load().r2
    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint_url(creds["account_id"]),
        aws_access_key_id=creds["access_key_id"],
        aws_secret_access_key=creds["secret_access_key"],
        config=BotoConfig(
            signature_version="s3v4",
            connect_timeout=15,
            read_timeout=120,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
        region_name="auto",
    )


def _etag_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".etag")


def _download_if_changed(client, bucket: str, key: str, target: Path) -> bool:
    """Download ``key`` to ``target`` unless the stored ETag still matches.

    Returns True when the local file was (re)written.
    """
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            log.warning("r2://%s/%s does not exist — skipping", bucket, key)
            return False
        raise

    remote_etag = (head.get("ETag") or "").strip('"')
    etag_file = _etag_path(target)
    if target.exists() and etag_file.exists():
        if etag_file.read_text().strip() == remote_etag and remote_etag:
            log.info("%s unchanged (etag %s) — skipping download", key, remote_etag[:12])
            return False

    target.parent.mkdir(parents=True, exist_ok=True)
    # Download to a temp name and rename, so a crashed transfer can never leave
    # Valhalla building tiles from a truncated .pbf.
    tmp = target.with_suffix(target.suffix + ".part")
    log.info("Downloading r2://%s/%s -> %s", bucket, key, target)
    client.download_file(bucket, key, str(tmp))
    tmp.replace(target)
    etag_file.write_text(remote_etag)
    log.info("Downloaded %s (%.1f MB)", target.name, target.stat().st_size / 1e6)
    return True


def sync_map_assets() -> dict:
    """Sync the routing .pbf and canopy sidecar into the Valhalla volume.

    Returns ``{"pbf_changed": bool, "canopy_changed": bool, "dir": str}``.
    ``pbf_changed`` is what should drive a Valhalla tile rebuild.
    """
    creds = r2_map_credentials()
    if creds is None:
        log.warning("R2 map credentials absent (need R2_ACCOUNT_ID/R2_ACCESS_KEY_ID/"
                    "R2_SECRET_ACCESS_KEY/R2_MAP_BUCKET) — skipping map sync")
        return {"pbf_changed": False, "canopy_changed": False, "dir": None}

    vcfg = load().valhalla
    dest_dir = Path(vcfg.custom_files_dir)
    client = _client(creds)

    pbf_changed = _download_if_changed(
        client, creds["bucket"], vcfg.map_object_key, dest_dir / vcfg.map_object_key)
    canopy_changed = _download_if_changed(
        client, creds["bucket"], vcfg.canopy_object_key, dest_dir / vcfg.canopy_object_key)

    result = {
        "pbf_changed": pbf_changed,
        "canopy_changed": canopy_changed,
        "dir": str(dest_dir),
    }
    log.info("map sync: %r", result)
    return result


def load_canopy_coverage() -> dict[int, float]:
    """Load the way_id -> canopy coverage table the shade profile scores against.

    Reads the file the sidecar dropped in the shared volume. Returns an empty
    mapping if it isn't there — shade routing then degrades to returning
    Valhalla's first alternate rather than failing the request.
    """
    vcfg = load().valhalla
    path = Path(vcfg.custom_files_dir) / vcfg.canopy_object_key
    if not path.exists():
        log.warning("Canopy coverage sidecar missing at %s — shade re-ranking "
                    "disabled until the next map sync", path)
        return {}

    coverage: dict[int, float] = {}
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                coverage[int(row["way_id"])] = float(row["coverage"])
            except (KeyError, ValueError):
                continue
    log.info("Loaded canopy coverage for %d ways from %s", len(coverage), path.name)
    return coverage


def map_asset_path() -> Path:
    vcfg = load().valhalla
    return Path(vcfg.custom_files_dir) / vcfg.map_object_key
