"""First-party user telemetry ingest — POST /api/v1/telemetry/events.

Receives small batches of behavioral events from the frontend
(denver-scooter-fyi src/telemetry.ts — the event-name allowlist below is
mirrored there by hand; a comment in each file points at the other).

Everything about this endpoint is shaped by the privacy stance documented
in sql/061_telemetry.sql:

  * Unknown event names are silently DROPPED, not rejected — a visitor
    running a stale cached bundle must not error-spam, and a 4xx here
    would surface retry noise for data we don't even want.
  * Props are truncated to caps rather than rejected, and only flat
    string/number/boolean values survive.
  * The only identity computed is visitor_hash = sha256(salt || ip || ua)
    with a per-day salt that cleanup_telemetry destroys after two days.
    Neither the IP nor the user-agent is stored.
  * No bearer-token resolution happens here; the client self-reports a
    boolean `auth` flag. Reading real account state would re-link identity
    to the pipeline, which is exactly what this design refuses to do.
  * The `cmp` page field (utm_campaign code from a link we published) is
    resolved against the campaigns registry before storage — unknown or
    malformed values collapse to 'other', absent to 'none' — so the
    stored campaign dimension is a bounded vocabulary, never free text
    (src/campaigns.py, sql/075_campaigns.sql).

Limits are enforced in code, not DDL, per house convention (sql/043):
they are product limits and will move.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request, Response

from . import campaigns
from .client_ip import real_client_ip
from .pg import connection
from .ratelimit import enforce

log = logging.getLogger(__name__)

router = APIRouter()

# Mirror of TELEMETRY_EVENTS in denver-scooter-fyi/src/telemetry.ts —
# update both together.
ALLOWED_EVENTS: frozenset[str] = frozenset(
    {
        # lifecycle
        "page_load",
        "page_hide",
        "install_prompt",
        # navigation
        "mode_switch",
        "drawer_open",
        "account_tab",
        "theme_change",
        # features
        "control_change",
        "filter_preset",
        # My Scooters (sql/081). favorite_added carries WHICH entry point —
        # the Tools panel's button or the device popup's star — and whether
        # the scooter was already kept; the ratio of those answers is what
        # says whether the popup star is pulling its weight. Never a
        # vehicle_identifier: attaching a device to a session is the one
        # thing this system is built not to do.
        "favorite_added",
        "favorite_removed",
        "favorite_notify",
        "area_filter",
        "geocode_search",
        "hex_tool",
        "cluster_tool",
        # device popup
        "popup_open",
        "popup_action",
        # ride wizard funnel
        "ride_open",
        "ride_screen",
        "ride_complete",
        "ride_abandon",
        # auth funnel
        "auth_start",
        "auth_success",
        "auth_error",
        # health
        "api_error",
    }
)

_DEVICE_CLASSES = frozenset({"mobile", "tablet", "desktop"})
_OS_FAMILIES = frozenset({"ios", "android", "mac", "windows", "linux"})
_VIEWPORTS = frozenset({"xs", "sm", "md", "lg", "xl"})

MAX_BATCH_EVENTS = 50
MAX_BODY_BYTES = 32 * 1024
MAX_PROP_KEYS = 12
MAX_PROP_VALUE_CHARS = 120
MAX_SID_CHARS = 16
_CLOCK_SKEW = timedelta(hours=1)

_RATE_BUCKET = "telemetry_ip"
_RATE_LIMIT = 120
_RATE_WINDOW_S = 3600


def _vocab(value: object, allowed: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "other"


def _clean_props(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key, value in raw.items():
        if len(out) >= MAX_PROP_KEYS:
            break
        if not isinstance(key, str) or not key:
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            out[key[:MAX_PROP_VALUE_CHARS]] = value
        elif isinstance(value, str):
            out[key[:MAX_PROP_VALUE_CHARS]] = value[:MAX_PROP_VALUE_CHARS]
        # non-scalar values are dropped
    return out


def _referrer_host(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "direct"
    # The client already sends a bare registrable host; defensively strip
    # anything path-like and cap it.
    host = value.split("/", 1)[0].split("?", 1)[0].strip().lower()
    return host[:MAX_PROP_VALUE_CHARS] or "direct"


def _visitor_hash(cur, ip: str, user_agent: str) -> str:
    """sha256(daily_salt || ip || ua), creating today's salt if needed."""
    today = datetime.now(timezone.utc).date()
    cur.execute(
        "INSERT INTO telemetry_salt (day, salt) VALUES (%s, %s) "
        "ON CONFLICT (day) DO NOTHING",
        (today, secrets.token_hex(16)),
    )
    cur.execute("SELECT salt FROM telemetry_salt WHERE day = %s", (today,))
    row = cur.fetchone()
    salt = row[0]
    digest = hashlib.sha256(f"{salt}|{ip}|{user_agent}".encode()).hexdigest()
    return digest


@router.post(
    "/api/v1/telemetry/events", status_code=204, include_in_schema=False
)
async def ingest_events(request: Request) -> Response:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return Response(status_code=204)  # oversized: drop, don't argue
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return Response(status_code=204)
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return Response(status_code=204)

    events = payload.get("events")
    if not isinstance(events, list) or not events:
        return Response(status_code=204)
    events = events[:MAX_BATCH_EVENTS]

    page = payload.get("page")
    if not isinstance(page, dict):
        page = {}
    device_class = _vocab(page.get("dc"), _DEVICE_CLASSES)
    os_family = _vocab(page.get("os"), _OS_FAMILIES)
    viewport = _vocab(page.get("vp"), _VIEWPORTS)
    referrer_host = _referrer_host(page.get("ref"))
    is_authenticated = page.get("auth") is True
    campaign_raw = page.get("cmp")

    ip = real_client_ip(request) or "?"
    user_agent = request.headers.get("user-agent", "")

    now = datetime.now(timezone.utc)
    rows = []
    dropped = 0
    for event in events:
        if not isinstance(event, dict):
            dropped += 1
            continue
        name = event.get("n")
        if not isinstance(name, str) or name not in ALLOWED_EVENTS:
            dropped += 1
            continue
        sid = event.get("sid")
        sid = sid[:MAX_SID_CHARS] if isinstance(sid, str) and sid else "?"
        received_at = now
        t = event.get("t")
        if isinstance(t, (int, float)):
            claimed = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
            if now - _CLOCK_SKEW <= claimed <= now + _CLOCK_SKEW:
                received_at = claimed
        rows.append((name, sid, received_at, _clean_props(event.get("p"))))
    if dropped:
        log.info("telemetry: dropped %d event(s) from one batch", dropped)
    if not rows:
        return Response(status_code=204)

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(
                cur,
                bucket=_RATE_BUCKET,
                key=ip,
                limit=_RATE_LIMIT,
                window_seconds=_RATE_WINDOW_S,
            )
            visitor = _visitor_hash(cur, ip, user_agent)
            # Client-sent utm_campaign code, collapsed to the bounded
            # vocabulary ('none' / 'other' / a live code) — see
            # src/campaigns.py for the privacy rationale.
            campaign = campaigns.resolve(cur, campaign_raw)
            cur.executemany(
                """
                INSERT INTO telemetry_events
                    (received_at, name, session_id, visitor_hash,
                     device_class, os_family, viewport, referrer_host,
                     is_authenticated, props, campaign)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        received_at,
                        name,
                        sid,
                        visitor,
                        device_class,
                        os_family,
                        viewport,
                        referrer_host,
                        is_authenticated,
                        json.dumps(props),
                        campaign,
                    )
                    for (name, sid, received_at, props) in rows
                ],
            )
    return Response(status_code=204)
