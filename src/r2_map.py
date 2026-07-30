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

The same pattern feeds the geocoder: ``sync_photon_index`` pulls
``photon/photon-index-<YYYYMMDD>.tar.zst`` out of the same private bucket (same
scoped token) into the ``photon_files`` volume for the Photon sidecar that
``/api/v1/geocode/search`` fronts. Photon has no fetcher of its own at all — the
index is a directory it memory-maps at startup — so this is the only way in.
"""

from __future__ import annotations

import csv
import gzip
import logging
import re
import shutil
import tarfile
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
    pbf_path = dest_dir / vcfg.map_object_key
    client = _client(creds)

    # Failures are caught per object rather than propagated. This runs as a
    # one-shot sidecar that `valhalla` gates on via
    # service_completed_successfully, and the deploy script runs under `set -e`
    # — so letting a transient R2 error escape here would fail `docker compose
    # up -d` and abort the deploy of every unrelated change in the same push.
    # Routing is optional; the audit API is not.
    errors: list[str] = []

    def _try(key: str, target: Path) -> bool:
        try:
            return _download_if_changed(client, creds["bucket"], key, target)
        except Exception as exc:  # noqa: BLE001
            log.error("map sync: failed to fetch %s: %s", key, exc)
            errors.append(f"{key}: {exc}")
            return False

    pbf_changed = _try(vcfg.map_object_key, pbf_path)
    canopy_changed = _try(vcfg.canopy_object_key, dest_dir / vcfg.canopy_object_key)

    result = {
        "pbf_changed": pbf_changed,
        "canopy_changed": canopy_changed,
        "dir": str(dest_dir),
        "errors": errors,
        # Whether Valhalla has anything to build from, which is what actually
        # matters downstream — a failed refresh with a previously-downloaded
        # .pbf still present is fine.
        "pbf_present": pbf_path.exists(),
    }
    if errors and not result["pbf_present"]:
        log.error("map sync: NO routing graph present and the fetch failed — "
                  "valhalla will have nothing to build; /api/v1/route will 503")
    elif errors:
        log.warning("map sync: fetch failed but an existing .pbf is present; "
                    "valhalla will serve the previous graph")
    log.info("map sync: %r", result)
    return result


# --- Photon geocoding index ---------------------------------------------------
#
# Not in config.json: the plan pins the `"geocode"` config block to
# `{"upstream", "enabled"}` (the proxy's contract), and these are compose-layout
# constants — changing either without also changing the volume mount in
# docker-compose.yml would just break the fetch. `/photon` is where BOTH the
# fetch sidecar and the photon container mount `photon_files`.
PHOTON_INDEX_DIR = "/photon"
# Photon serves whatever it finds in <data-dir>/photon_data; that is also the
# top-level directory inside the tarball (see scripts/build_photon_index.md).
PHOTON_DATA_DIRNAME = "photon_data"
PHOTON_INDEX_PREFIX = "photon/"
# The object name carries its build date, so the newest key is picked by name
# rather than by LastModified (a re-upload of an old index must not win).
PHOTON_INDEX_RE = re.compile(r"^photon-index-(\d{8})\.tar\.zst$")
# Records the (key, etag) whose unpack SUCCEEDED — written after the swap, never
# before, so a truncated download or a failed unpack is retried on the next run
# instead of being remembered as done.
PHOTON_MARKER_NAME = "photon-index.etag"


def photon_index_dir() -> Path:
    return Path(PHOTON_INDEX_DIR)


def photon_data_path() -> Path:
    """The directory Photon actually serves from."""
    return photon_index_dir() / PHOTON_DATA_DIRNAME


def _newest_photon_index(client, bucket: str) -> tuple[str | None, str | None]:
    """Newest ``photon/photon-index-<YYYYMMDD>.tar.zst`` key and its ETag."""
    best: tuple[str, str, str] | None = None  # (date, key, etag)
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=PHOTON_INDEX_PREFIX):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if not key.startswith(PHOTON_INDEX_PREFIX):
                continue
            m = PHOTON_INDEX_RE.match(key[len(PHOTON_INDEX_PREFIX):])
            if not m:
                continue
            etag = (obj.get("ETag") or "").strip('"')
            cand = (m.group(1), key, etag)
            if best is None or cand[:2] > best[:2]:
                best = cand
    if best is None:
        return None, None
    return best[1], best[2]


def _read_photon_marker(dest_dir: Path) -> tuple[str | None, str | None]:
    marker = dest_dir / PHOTON_MARKER_NAME
    try:
        lines = marker.read_text().splitlines()
    except OSError:
        return None, None
    key = lines[0].strip() if lines else ""
    etag = lines[1].strip() if len(lines) > 1 else ""
    return (key or None), (etag or None)


def _unpack_photon_index(archive: Path, dest_dir: Path) -> None:
    """Unpack a ``.tar.zst`` index and swap ``photon_data`` into place.

    stdlib ``tarfile`` reads gz/bz2/xz only and the worker image ships no
    ``zstd`` binary, hence ``zstandard`` (requirements.txt). Imported lazily so
    this module — and everything that imports it, including ``src.cli`` — stays
    importable on an environment that hasn't installed it yet.

    Extracted into a staging directory and renamed over the live one, so Photon
    is never pointed at a half-written index: the swap is two renames on one
    filesystem.
    """
    import zstandard  # noqa: PLC0415 — see above

    staging = dest_dir / ".photon_index_staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    dctx = zstandard.ZstdDecompressor()
    with open(archive, "rb") as fh, dctx.stream_reader(fh) as reader:
        # "r|" = stream mode: the decompressor is not seekable.
        with tarfile.open(fileobj=reader, mode="r|") as tar:
            # filter="data" refuses absolute paths, "..", device nodes and
            # symlinks pointing outside the tree (CVE-2007-4559).
            tar.extractall(path=staging, filter="data")

    unpacked = staging / PHOTON_DATA_DIRNAME
    if not unpacked.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(
            f"photon index archive has no top-level {PHOTON_DATA_DIRNAME}/ "
            f"directory — build it with `tar -c {PHOTON_DATA_DIRNAME}/` "
            f"(scripts/build_photon_index.md)")

    live = dest_dir / PHOTON_DATA_DIRNAME
    previous = dest_dir / f"{PHOTON_DATA_DIRNAME}.old"
    shutil.rmtree(previous, ignore_errors=True)
    if live.exists():
        live.rename(previous)
    unpacked.rename(live)
    shutil.rmtree(previous, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)


def sync_photon_index() -> dict:
    """Sync the newest Photon geocoding index into the ``photon_files`` volume.

    Returns ``{"changed": bool, "key": str|None, "dir": str, "index_present":
    bool, "errors": [...]}``. ``changed`` is what tells an operator the
    ``photon`` container has to be restarted: Photon opens the index at JVM
    startup and never re-reads it, and this container has no Docker socket to
    restart it itself (the same deliberate limitation as
    ``refresh_routing_graph`` and Valhalla's tile rebuild).

    Errors are caught, not raised: this runs as the one-shot ``photon_index_fetch``
    sidecar that ``photon`` gates on via ``service_completed_successfully``, and
    the deploy script runs under ``set -e``. An R2 outage must degrade address
    autocomplete (a clean 503 from /api/v1/geocode/search), not abort the deploy
    of everything else in the push.
    """
    dest_dir = photon_index_dir()
    data_dir = dest_dir / PHOTON_DATA_DIRNAME
    result: dict = {"changed": False, "key": None, "dir": str(dest_dir),
                    "index_present": data_dir.is_dir(), "errors": []}

    creds = r2_map_credentials()
    if creds is None:
        log.warning("R2 map credentials absent (need R2_ACCOUNT_ID/R2_MAP_BUCKET"
                    " + a map or archive key pair) — skipping photon index sync")
        return result

    bucket = creds["bucket"]
    try:
        client = _client(creds)
        key, etag = _newest_photon_index(client, bucket)
    except Exception as exc:  # noqa: BLE001
        log.error("photon index sync: cannot list r2://%s/%s: %s",
                  bucket, PHOTON_INDEX_PREFIX, exc)
        result["errors"].append(str(exc))
        return result

    if key is None:
        log.warning("photon index sync: no %s* object in r2://%s — seed one with "
                    "scripts/build_photon_index.md; /api/v1/geocode/search will "
                    "503 until then", PHOTON_INDEX_PREFIX, bucket)
        return result
    result["key"] = key

    have_key, have_etag = _read_photon_marker(dest_dir)
    if data_dir.is_dir() and have_key == key and have_etag == etag and etag:
        log.info("photon index %s unchanged (etag %s) — skipping download",
                 key, etag[:12])
        return result

    archive = dest_dir / "photon-index.tar.zst.part"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        log.info("Downloading r2://%s/%s -> %s", bucket, key, archive)
        client.download_file(bucket, key, str(archive))
        log.info("Unpacking %s (%.1f MB) into %s",
                 key, archive.stat().st_size / 1e6, dest_dir)
        _unpack_photon_index(archive, dest_dir)
    except Exception as exc:  # noqa: BLE001
        log.error("photon index sync: failed to install %s: %s", key, exc)
        result["errors"].append(f"{key}: {exc}")
        result["index_present"] = data_dir.is_dir()
        return result
    finally:
        # The tarball is a multi-GB transient; the unpacked index is what
        # Photon serves. Never left behind, so a failed run also can't fill the
        # volume and wedge the next one.
        archive.unlink(missing_ok=True)

    (dest_dir / PHOTON_MARKER_NAME).write_text(f"{key}\n{etag}\n")
    result["changed"] = True
    result["index_present"] = data_dir.is_dir()
    log.warning("PHOTON INDEX UPDATED to %s — the photon container must be "
                "RESTARTED to load it (it maps the index at JVM startup): "
                "docker compose restart photon", key)
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
