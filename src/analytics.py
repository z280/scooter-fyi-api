"""User-analytics rollups and retention (sql/061_telemetry.sql tables).

Two cron entry points, both registered in src/cli.py COMMANDS:

  rollup_analytics   — recompute YESTERDAY's telemetry_daily and
                       request_metrics_daily rows (Denver calendar day,
                       matching the daily_trips convention). Idempotent:
                       delete-and-reinsert for the day, so a re-run or a
                       backfill call with an explicit day is safe.
  cleanup_telemetry  — enforce the retention windows documented in
                       src/api_meta.py's _PRIVACY payload and the privacy
                       policy: raw telemetry_events 90 days, raw
                       request_metrics 30 days, telemetry_salt 2 days.
                       The *_daily rollups are aggregate and identity-free
                       and are kept indefinitely.

The 2-day salt window is what makes visitor_hash irreversible: yesterday's
rollup (which counts DISTINCT visitor_hash) runs while yesterday's salt
still exists, and the salt is destroyed on the next cleanup pass.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .pg import connection

log = logging.getLogger(__name__)

_DENVER = ZoneInfo("America/Denver")

TELEMETRY_RAW_RETENTION_DAYS = 90
REQUEST_METRICS_RETENTION_DAYS = 30
SALT_RETENTION_DAYS = 2

# Cap on distinct values kept per prop key in telemetry_daily.prop_summary.
_TOP_K_VALUES = 10


def _yesterday_denver() -> date:
    return (datetime.now(_DENVER) - timedelta(days=1)).date()


def rollup_analytics(day: date | None = None) -> dict:
    """Recompute one day's rollup rows (default: yesterday, Denver time)."""
    target = day or _yesterday_denver()
    with connection() as conn:
        with conn.cursor() as cur:
            # --- telemetry_daily -------------------------------------
            cur.execute("DELETE FROM telemetry_daily WHERE day = %s", (target,))
            cur.execute(
                """
                INSERT INTO telemetry_daily
                    (day, city_id, name, events, visitors, sessions)
                SELECT %s, city_id, name,
                       COUNT(*),
                       COUNT(DISTINCT visitor_hash),
                       COUNT(DISTINCT session_id)
                FROM telemetry_events
                WHERE (received_at AT TIME ZONE 'America/Denver')::date = %s
                GROUP BY city_id, name
                """,
                (target, target),
            )
            events_rows = cur.rowcount

            # Top-k prop-value counts per (name, key), folded into
            # prop_summary so "which drawer / which mode" survives raw-row
            # pruning. Non-string prop values are counted via their text
            # form — everything the client sends is scalar.
            cur.execute(
                """
                SELECT name, city_id, kv.key, kv.value, COUNT(*)
                FROM telemetry_events,
                     LATERAL jsonb_each_text(props) AS kv(key, value)
                WHERE (received_at AT TIME ZONE 'America/Denver')::date = %s
                GROUP BY name, city_id, kv.key, kv.value
                """,
                (target,),
            )
            summaries: dict[tuple, dict[str, dict[str, int]]] = defaultdict(
                lambda: defaultdict(dict)
            )
            for name, city_id, key, value, count in cur.fetchall():
                summaries[(name, city_id)][key][value] = count
            for (name, city_id), by_key in summaries.items():
                trimmed = {
                    key: dict(
                        sorted(vals.items(), key=lambda kv: -kv[1])[:_TOP_K_VALUES]
                    )
                    for key, vals in by_key.items()
                }
                cur.execute(
                    """
                    UPDATE telemetry_daily SET prop_summary = %s
                    WHERE day = %s AND name = %s
                      AND city_id IS NOT DISTINCT FROM %s
                    """,
                    (json.dumps(trimmed), target, name, city_id),
                )

            # --- request_metrics_daily -------------------------------
            cur.execute(
                "DELETE FROM request_metrics_daily WHERE day = %s", (target,)
            )
            cur.execute(
                """
                INSERT INTO request_metrics_daily
                    (day, city_id, route, method, status_class,
                     requests, p50_ms, p95_ms, authed_requests)
                SELECT %s, city_id, route, method,
                       LEFT(status::text, 1) || 'xx',
                       COUNT(*),
                       COALESCE(percentile_cont(0.5)
                           WITHIN GROUP (ORDER BY duration_ms), 0)::int,
                       COALESCE(percentile_cont(0.95)
                           WITHIN GROUP (ORDER BY duration_ms), 0)::int,
                       COUNT(*) FILTER (WHERE is_authenticated)
                FROM request_metrics
                WHERE (at AT TIME ZONE 'America/Denver')::date = %s
                GROUP BY city_id, route, method, LEFT(status::text, 1) || 'xx'
                """,
                (target, target),
            )
            request_rows = cur.rowcount
        conn.commit()
    log.info(
        "rollup_analytics: day=%s telemetry_daily=%d request_metrics_daily=%d",
        target,
        events_rows,
        request_rows,
    )
    return {
        "day": str(target),
        "telemetry_daily": events_rows,
        "request_metrics_daily": request_rows,
    }


def cleanup_telemetry() -> dict:
    """Enforce the published retention windows. Idempotent."""
    now = datetime.now(timezone.utc)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM telemetry_events WHERE received_at < %s",
                (now - timedelta(days=TELEMETRY_RAW_RETENTION_DAYS),),
            )
            events = cur.rowcount
            cur.execute(
                "DELETE FROM request_metrics WHERE at < %s",
                (now - timedelta(days=REQUEST_METRICS_RETENTION_DAYS),),
            )
            requests = cur.rowcount
            cur.execute(
                "DELETE FROM telemetry_salt WHERE day < %s",
                (now.date() - timedelta(days=SALT_RETENTION_DAYS),),
            )
            salts = cur.rowcount
        conn.commit()
    log.info(
        "cleanup_telemetry: events=%d request_metrics=%d salts=%d",
        events,
        requests,
        salts,
    )
    return {"events": events, "request_metrics": requests, "salts": salts}
