"""Precomputed, pre-compressed map payloads (owner, 2026-10-10).

/api/v1/devices/current and /api/v1/h3/aggregates only change when an ingest
cycle completes (every 2 min) or a negative report changes. Before this
module every request rebuilt them: ~1.3 s of SQL and per-vehicle scoring,
then JSON-encoding and gzipping ~9.4 MB, for a ~2.2 s time to first byte.
Now each payload is built ONCE per (cycle, reports stamp), compressed once,
and every request after that is a memory lookup plus a few hundred bytes of
compression.

Where an entry lives:
  * in this process (`_memo`), the copy requests are served from;
  * in the UNLOGGED `payload_cache` table (sql/103), one row per variant,
    so a restarted worker serves warm bytes on its first request instead of
    rebuilding. UNLOGGED: losing it on a Postgres crash costs one rebuild,
    and it skips WAL for megabytes rewritten every cycle.

Who builds: a background warmer thread (`start_warmer`, started from the app
lifespan) polls for a new cycle every few seconds and rebuilds the variants
the frontend actually requests, so normally no rider ever waits on a build.
A request that misses builds the entry itself (one builder per key; others
wait for it, or are served the previous entry if one exists — see
`get_or_build`).

Why entries are "open" gzip: /devices/current's metadata differs per caller
(`viewed_by` on the signed-in endpoint) while ~all of the bytes — the
features — are shared. An entry therefore stores the shared prefix as a raw
DEFLATE stream ended with Z_SYNC_FLUSH (byte-aligned, no final block) plus
the CRC-32 and length of its uncompressed bytes. `assemble` compresses the
small per-request suffix with a fresh compressor, appends it, and writes the
gzip trailer from the running CRC — a valid single-member gzip stream, no
recompression of the prefix. The h3 payload has no per-caller part; its
suffix is empty.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID

from .pg import connection

log = logging.getLogger(__name__)

# The test suite turns this off (tests/conftest.py) so fake-DB fixtures that
# reuse one cycle_id never see each other's payloads; with it off every call
# builds fresh and nothing touches the table.
ENABLED = os.environ.get("PAYLOAD_CACHE", "1") != "0"

# How old a previous entry may be and still be served while its replacement
# is being built. Two ingest cycles: a rider never sees data older than the
# cycle before last, and a hung build cannot pin an old payload forever.
MAX_STALE_SECONDS = 300.0

# zlib level for the prefix. 6 is within ~3% of 9's size on this JSON at
# under half the CPU (GZipMiddleware uses 9, per request).
_LEVEL = 6
_GZIP_HEADER = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"


@dataclass
class Entry:
    key: str
    cycle_id: str
    stamp: str
    body: bytes          # raw DEFLATE, sync-flushed, no final block
    crc: int             # CRC-32 of the uncompressed prefix
    size: int            # length of the uncompressed prefix
    meta: dict[str, Any] = field(default_factory=dict)
    built_at: float = field(default_factory=time.time)

    def fresh_for(self, cycle_id: Any, stamp: str) -> bool:
        return self.cycle_id == str(cycle_id) and self.stamp == stamp


def _json_default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def dumps(obj: Any) -> bytes:
    """JSON the way Starlette's JSONResponse writes it (compact separators,
    UTF-8, no NaN), so a cached body matches what the route used to send."""
    return json.dumps(obj, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), default=_json_default).encode("utf-8")


def compress_prefix(raw: bytes) -> tuple[bytes, int, int]:
    c = zlib.compressobj(_LEVEL, zlib.DEFLATED, -15)
    return c.compress(raw) + c.flush(zlib.Z_SYNC_FLUSH), zlib.crc32(raw), len(raw)


def make_entry(key: str, cycle_id: Any, stamp: str, prefix: bytes,
               meta: dict[str, Any] | None = None) -> Entry:
    body, crc, size = compress_prefix(prefix)
    return Entry(key=key, cycle_id=str(cycle_id), stamp=stamp, body=body,
                 crc=crc, size=size, meta=meta or {})


def assemble(entry: Entry, suffix: bytes = b"") -> bytes:
    """The entry's prefix + `suffix` as one complete gzip stream."""
    c = zlib.compressobj(_LEVEL, zlib.DEFLATED, -15)
    tail = c.compress(suffix) + c.flush(zlib.Z_FINISH)
    crc = zlib.crc32(suffix, entry.crc)
    size = (entry.size + len(suffix)) & 0xFFFFFFFF
    return _GZIP_HEADER + entry.body + tail + struct.pack("<II", crc, size)


def gzip_response_body(gz: bytes, accept_encoding: str | None) -> tuple[bytes, dict[str, str]]:
    """(body, headers) for a client: the gzip bytes as-is when it accepts
    gzip (every browser, and Cloudflare in front of us), else decompressed."""
    if "gzip" in (accept_encoding or "").lower():
        return gz, {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
    return gzip.decompress(gz), {"Vary": "Accept-Encoding"}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
_memo: dict[str, Entry] = {}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def _load(key: str) -> Entry | None:
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cycle_id, stamp, body, crc, size, meta, "
                    "       EXTRACT(EPOCH FROM built_at) "
                    "FROM payload_cache WHERE cache_key = %s", (key,))
                row = cur.fetchone()
    except Exception:  # noqa: BLE001 — the table is an optimisation
        log.warning("payload_cache: read of %s failed; building instead", key)
        return None
    if not row:
        return None
    meta = row[5] if isinstance(row[5], dict) else json.loads(row[5] or "{}")
    return Entry(key=key, cycle_id=row[0], stamp=row[1], body=bytes(row[2]),
                 crc=int(row[3]), size=int(row[4]), meta=meta,
                 built_at=float(row[6]))


def _store(entry: Entry) -> None:
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO payload_cache "
                    "  (cache_key, cycle_id, stamp, body, crc, size, meta, built_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, to_timestamp(%s)) "
                    "ON CONFLICT (cache_key) DO UPDATE SET "
                    "  cycle_id = EXCLUDED.cycle_id, stamp = EXCLUDED.stamp, "
                    "  body = EXCLUDED.body, crc = EXCLUDED.crc, "
                    "  size = EXCLUDED.size, meta = EXCLUDED.meta, "
                    "  built_at = EXCLUDED.built_at",
                    (entry.key, entry.cycle_id, entry.stamp, entry.body,
                     entry.crc, entry.size, json.dumps(entry.meta, default=_json_default),
                     entry.built_at))
            conn.commit()
    except Exception:  # noqa: BLE001
        log.warning("payload_cache: write of %s failed; serving from memory only", entry.key)


def peek(key: str) -> Entry | None:
    return _memo.get(key)


def clear() -> None:
    """Forget every in-process entry (tests)."""
    _memo.clear()


def get_or_build(key: str, cycle_id: Any, stamp: str,
                 build: Callable[[], Entry], *, lock_key: str | None = None) -> Entry:
    """The entry for `key` at (cycle_id, stamp), building it if needed.

    `build` returns a complete Entry (see make_entry) and is called at most
    once at a time per key. While one caller builds, the others are served
    the previous entry for that key if it is under MAX_STALE_SECONDS old —
    its own cycle and stamp travel with it, so the caller's ETag and
    metadata describe exactly the data sent — else they wait for the build.

    `lock_key` lets several keys share one builder (the three /h3
    resolutions come from one build), so a request for one of them during
    that build is served stale instead of starting a second build.
    """
    if not ENABLED:
        return build()
    hit = _memo.get(key)
    if hit is not None and hit.fresh_for(cycle_id, stamp):
        return hit
    lock = _lock_for(lock_key or key)
    if not lock.acquire(blocking=False):
        stale = _memo.get(key)
        if stale is not None and time.time() - stale.built_at < MAX_STALE_SECONDS:
            return stale
        lock.acquire()
    try:
        hit = _memo.get(key)
        if hit is not None and hit.fresh_for(cycle_id, stamp):
            return hit
        stored = _load(key)
        if stored is not None and stored.fresh_for(cycle_id, stamp):
            _memo[key] = stored
            return stored
        t0 = time.perf_counter()
        entry = build()
        _memo[key] = entry
        _store(entry)
        log.info("payload_cache: built %s for cycle %s in %.2fs (%d KB gz)",
                 key, entry.cycle_id, time.perf_counter() - t0, len(entry.body) // 1024)
        return entry
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Warmer
# ---------------------------------------------------------------------------
WARM_INTERVAL_SECONDS = float(os.environ.get("PAYLOAD_CACHE_WARM_INTERVAL", "3"))
_warmers: list[Callable[[], None]] = []


def register_warmer(fn: Callable[[], None]) -> None:
    """A callable that brings its endpoint's hot variants up to date — cheap
    when they already are (get_or_build's memo check)."""
    _warmers.append(fn)


def warm_once() -> None:
    for fn in list(_warmers):
        try:
            fn()
        except Exception:  # noqa: BLE001 — the next tick retries
            log.warning("payload_cache: warmer %s failed", getattr(fn, "__name__", fn),
                        exc_info=True)


def start_warmer() -> threading.Event | None:
    """Start the warmer thread; returns the Event that stops it. A no-op
    (None) when the cache is off or PAYLOAD_CACHE_WARM=0."""
    if not ENABLED or os.environ.get("PAYLOAD_CACHE_WARM", "1") == "0":
        return None
    stop = threading.Event()

    def _loop() -> None:
        warm_once()
        while not stop.wait(WARM_INTERVAL_SECONDS):
            warm_once()

    threading.Thread(target=_loop, name="payload-cache-warmer", daemon=True).start()
    return stop
