"""Postgres-backed fixed-window rate limiting (API_REQUIREMENTS.md §5).

One row per counted event in rate_limit_events; a request is allowed when
COUNT(bucket, key, window) < limit. Deliberately simple — at this system's
traffic, a COUNT over an indexed (bucket, key, at) prefix is microseconds,
and sharing the Postgres instance means no new infrastructure and no
per-process state (the API can scale to N workers without coordination).

Usage in a handler:

    enforce(cur, bucket="magic_link_ip", key=ip, limit=10, window_seconds=3600)

`enforce` raises HTTPException(429) with a Retry-After header when the
window is full, and otherwise RECORDS the event — check-and-record is one
call so callers can't forget the record half. Pass the caller's open
cursor: the recorded event commits/rolls back atomically with the caller's
own writes (a rolled-back request doesn't consume quota).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

log = logging.getLogger(__name__)

# Events older than this are useless for any window we run and get pruned
# opportunistically (probability-free: every enforce() call deletes stale
# rows for its own bucket/key — bounded work, no scheduled job needed).
_PRUNE_AFTER = timedelta(days=2)


def enforce(
    cur,
    *,
    bucket: str,
    key: str,
    limit: int,
    window_seconds: int,
) -> None:
    """Allow-and-record, or raise 429 with Retry-After.

    `cur` is an open psycopg cursor; the event insert joins the caller's
    transaction.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(seconds=window_seconds)

    # Serialize check+insert for this (bucket, key): without this,
    # concurrent requests can both observe count < limit and both insert,
    # temporarily exceeding the configured cap. pg_advisory_xact_lock
    # auto-releases at COMMIT/ROLLBACK — no separate unlock call needed —
    # and only ever blocks two callers sharing the same bucket/key, so it
    # doesn't serialize unrelated rate-limit checks against each other.
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"{bucket}:{key}",),
    )

    cur.execute(
        "DELETE FROM rate_limit_events WHERE bucket = %s AND key = %s AND at < %s",
        (bucket, key, now - _PRUNE_AFTER),
    )
    cur.execute(
        """
        SELECT COUNT(*), MIN(at) FROM rate_limit_events
        WHERE bucket = %s AND key = %s AND at >= %s
        """,
        (bucket, key, window_start),
    )
    count, oldest = cur.fetchone()

    if count >= limit:
        # The window frees up when its oldest event ages out.
        retry_after = max(1, int((oldest + timedelta(seconds=window_seconds) - now).total_seconds()))
        log.info("rate limit hit: bucket=%s key=%s (%d/%d)", bucket, key, count, limit)
        raise HTTPException(
            429,
            detail="rate limit exceeded — try again later",
            headers={"Retry-After": str(retry_after)},
        )

    cur.execute(
        "INSERT INTO rate_limit_events (bucket, key, at) VALUES (%s, %s, %s)",
        (bucket, key, now),
    )
