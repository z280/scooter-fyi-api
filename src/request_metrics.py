"""Per-request API metrics: route template, status, latency, coarse device.

Captured by an HTTP middleware registered in src/main.py and buffered in
memory; a background task flushes batches to the request_metrics table
every few seconds. Buffering exists because a per-request pool checkout +
INSERT would add tail latency to every hot-path call (devices/current) for
data that is only read daily; losing a few seconds of metrics on a crash
is an accepted trade for that.

Privacy shape (see sql/061_telemetry.sql): the route TEMPLATE is stored,
never the raw path; the user-agent is reduced to coarse device/OS buckets
and then discarded; no IP is stored anywhere here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone

from .pg import connection

log = logging.getLogger(__name__)

# Paths never recorded: /health is load-balancer noise, /admin is the
# OAuth-gated HTML panel (operator traffic, not user traffic).
_SKIP_PREFIXES = ("/health", "/admin")

_FLUSH_INTERVAL_S = 5.0
_FLUSH_MAX_ROWS = 200
# Backstop if the flusher dies or the DB is down: drop oldest rather than
# grow without bound.
_BUFFER_CAP = 10_000

_buffer: deque[tuple] = deque(maxlen=_BUFFER_CAP)


def classify_user_agent(ua: str | None) -> tuple[str, str]:
    """Reduce a User-Agent to (device_class, os_family) buckets.

    Deliberately a handful of substring checks, not a UA-parser
    dependency: the analytics only ever read these six-ish buckets, so a
    full regex database buys nothing. Order matters — iPads and Android
    tablets advertise their OS before their form factor.
    """
    if not ua:
        return ("other", "other")
    s = ua.lower()
    if "ipad" in s:
        return ("tablet", "ios")
    if "iphone" in s or "ipod" in s:
        return ("mobile", "ios")
    if "android" in s:
        return ("tablet" if "mobile" not in s else "mobile", "android")
    if "windows" in s:
        return ("desktop", "windows")
    # Real Macs say "Macintosh"; iPads pretending to be Macs were caught
    # above only when honest — desktop-mode iPads are counted as macs,
    # which is what they asked to be.
    if "macintosh" in s or "mac os x" in s:
        return ("desktop", "mac")
    if "linux" in s or "x11" in s:
        return ("desktop", "linux")
    if "mobi" in s:
        return ("mobile", "other")
    return ("other", "other")


def record(
    *,
    route: str,
    method: str,
    status: int,
    duration_ms: int,
    device_class: str,
    os_family: str,
    is_authenticated: bool,
) -> None:
    _buffer.append(
        (
            datetime.now(timezone.utc),
            route,
            method,
            status,
            duration_ms,
            device_class,
            os_family,
            is_authenticated,
        )
    )


def flush_pending() -> int:
    """Write everything currently buffered; returns rows written.

    Runs on a worker thread (asyncio.to_thread) from the flush loop, and
    synchronously at shutdown.
    """
    rows = []
    while _buffer:
        try:
            rows.append(_buffer.popleft())
        except IndexError:  # raced another flusher; fine
            break
    if not rows:
        return 0
    with connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO request_metrics
                    (at, route, method, status, duration_ms,
                     device_class, os_family, is_authenticated)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                rows,
            )
    return len(rows)


async def flush_loop(stop: asyncio.Event) -> None:
    """Background task started from the app lifespan."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_FLUSH_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
        try:
            if _buffer:
                await asyncio.to_thread(flush_pending)
        except Exception:  # noqa: BLE001 — metrics must never take the app down
            log.exception("request_metrics flush failed; will retry")


async def middleware(request, call_next):
    """Time the request and buffer one metrics row. Never raises."""
    start = time.perf_counter()
    response = await call_next(request)
    try:
        path = request.url.path
        if path.startswith(_SKIP_PREFIXES):
            return response
        route_obj = request.scope.get("route")
        route = getattr(route_obj, "path_format", None) or "__unmatched__"
        device_class, os_family = classify_user_agent(
            request.headers.get("user-agent")
        )
        record(
            route=route,
            method=request.method,
            status=response.status_code,
            duration_ms=int((time.perf_counter() - start) * 1000),
            device_class=device_class,
            os_family=os_family,
            is_authenticated=request.headers.get("authorization", "")
            .lower()
            .startswith("bearer "),
        )
        if len(_buffer) >= _FLUSH_MAX_ROWS:
            # Nudge an early flush without blocking the request.
            asyncio.get_running_loop().create_task(
                asyncio.to_thread(_flush_swallowing)
            )
    except Exception:  # noqa: BLE001
        log.exception("request_metrics capture failed for one request")
    return response


def _flush_swallowing() -> None:
    try:
        flush_pending()
    except Exception:  # noqa: BLE001
        log.exception("request_metrics early flush failed")
