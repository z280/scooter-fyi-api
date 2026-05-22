"""Fan out the cycle's core summary to all configured downstream endpoints."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import httpx

from .config import load
from .pg import connection

log = logging.getLogger(__name__)


def _record_attempt(
    cycle_id: uuid.UUID,
    name: str,
    url: str,
    method: str,
    path: str,
    status: int | None,
    error: str | None,
) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO transmission_attempts
                    (cycle_id, endpoint_name, url, method, path, http_status_code, error_details)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (str(cycle_id), name, url, method, path, status, error),
            )
        conn.commit()


def fanout(cycle_id: uuid.UUID, core_row: dict[str, Any]) -> str:
    """POST the core summary to each configured endpoint.

    Returns the aggregate transmission_status: 'complete' | 'partial_failure' | 'failure'.
    Empty endpoint list → 'complete' (nothing to fail).
    """
    endpoints = load().transmission_endpoints
    if not endpoints:
        return "complete"

    # Build a JSON-safe view (datetimes → isoformat)
    payload = {
        k: (v.isoformat() if hasattr(v, "isoformat") else v)
        for k, v in core_row.items()
    }
    headers_base = {"X-Cycle-Id": str(cycle_id), "User-Agent": "veo-audit/3.2"}

    successes = 0
    failures = 0

    for ep in endpoints:
        full_url = ep.url.rstrip("/") + (ep.path or "")
        headers = dict(headers_base)
        if ep.auth_env:
            token = os.environ.get(ep.auth_env)
            if token:
                headers["Authorization"] = f"Bearer {token}"

        try:
            with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
                resp = client.request(
                    ep.method.upper(),
                    full_url,
                    json=payload,
                    headers=headers,
                )
            status = resp.status_code
            err = None if status < 400 else resp.text[:1000]
            if status < 400:
                successes += 1
            else:
                failures += 1
            _record_attempt(cycle_id, ep.name, ep.url, ep.method, ep.path, status, err)
        except Exception as e:  # noqa: BLE001
            failures += 1
            _record_attempt(cycle_id, ep.name, ep.url, ep.method, ep.path, None, str(e)[:1000])
            log.warning("transmission to %s failed: %s", ep.name, e)

    if failures == 0:
        return "complete"
    if successes == 0:
        return "failure"
    return "partial_failure"
