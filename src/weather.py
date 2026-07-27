"""Denver temperature, for the battery-burn regression's third term.

The GBFS feed carries no temperature, so it is backfilled from Open-Meteo's
archive (ERA5 reanalysis, no API key for non-commercial use). ERA5 lags roughly
5 days, which is why the training job only considers trips older than
:data:`ARCHIVE_LAG_DAYS`.

Hourly values are cached in Postgres so a refit doesn't re-fetch a month of
history, and one request covers an arbitrary date range.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import httpx

from .pg import connection

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Denver city centre — the whole routing graph is ~15 km across, well inside a
# single ERA5 cell, so one point is plenty.
DENVER_LAT = 39.72
DENVER_LON = -104.97

# ERA5 reanalysis publication lag. Trips newer than this have no archive
# temperature and are excluded from training rather than silently defaulted.
ARCHIVE_LAG_DAYS = 5

_TIMEOUT = 30.0


def backfill_hourly(start: date, end: date) -> int:
    """Fetch hourly temperatures for [start, end] and upsert them. Returns rows written."""
    params = {
        "latitude": DENVER_LAT,
        "longitude": DENVER_LON,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": "temperature_2m",
        "timezone": "UTC",
    }
    log.info("Fetching Open-Meteo archive %s..%s", start, end)
    resp = httpx.get(ARCHIVE_URL, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return _upsert(resp.json().get("hourly", {}))


def _upsert(hourly: dict) -> int:
    """Upsert an Open-Meteo hourly block. Returns rows written."""
    times = hourly.get("time", []) or []
    temps = hourly.get("temperature_2m", []) or []

    rows = [
        (datetime.fromisoformat(t).replace(tzinfo=timezone.utc), float(c))
        for t, c in zip(times, temps)
        if c is not None
    ]
    if not rows:
        log.warning("Open-Meteo returned no usable temperatures")
        return 0

    with connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO hourly_temperature (observed_hour, temperature_c)
                VALUES (%s, %s)
                ON CONFLICT (observed_hour) DO UPDATE
                  SET temperature_c = EXCLUDED.temperature_c
                """,
                rows,
            )
        conn.commit()
    log.info("Backfilled %d hourly temperatures", len(rows))
    return len(rows)


def backfill_recent_hourly(past_days: int = 7) -> int:
    """Fetch the last ``past_days`` of hourly temperature from the forecast API.

    The archive (ERA5 reanalysis) lags ~5 days, but extraction runs against the
    last 26 hours of telemetry — so every fresh observation would land with a
    NULL temperature and be discarded at fit time. The forecast endpoint serves
    recent history with no lag via ``past_days``. It is a forecast model rather
    than reanalysis, so the two sources differ slightly; for a single
    degrees-Celsius regressor that is well inside the noise, and rows are upsert
    keyed by hour so a later archive fetch supersedes a forecast value.
    """
    params = {
        "latitude": DENVER_LAT,
        "longitude": DENVER_LON,
        "hourly": "temperature_2m",
        "past_days": min(max(past_days, 1), 92),
        "forecast_days": 1,
        "timezone": "UTC",
    }
    log.info("Fetching Open-Meteo recent hours (past_days=%s)", params["past_days"])
    resp = httpx.get(FORECAST_URL, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return _upsert(resp.json().get("hourly", {}))


def _hours_missing(start: date, end: date) -> int:
    """How many hourly readings the cache lacks over the inclusive date range.

    Counting beats comparing MIN/MAX: an envelope check only detects data
    missing OUTSIDE the cached range, so a partial backfill (a rate-limited or
    truncated response) leaves an interior hole that the envelope reports as
    fully covered. Trips in that hole then get no temperature at all, while the
    log claims the cache "already covers" the window.
    """
    expected = ((end - start).days + 1) * 24
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM hourly_temperature
                WHERE observed_hour >= %s AND observed_hour < %s
                """,
                (start, end + timedelta(days=1)),
            )
            have = cur.fetchone()[0]
    # Open-Meteo can legitimately omit the final partial hour, so a single
    # missing reading is not worth a refetch.
    missing = max(expected - have, 0)
    if missing:
        log.info("Temperature cache: %d of %d hours missing for %s..%s",
                 missing, expected, start, end)
    return 0 if missing <= 1 else missing


def ensure_coverage(start: date, end: date) -> int:
    """Make sure [start, end] is cached, choosing the right source per range.

    Anything older than the reanalysis lag comes from the archive; the recent
    tail comes from the forecast endpoint, which has no lag.
    """
    today = datetime.now(timezone.utc).date()
    archive_cutoff = today - timedelta(days=ARCHIVE_LAG_DAYS)

    written = 0
    # Older portion: ERA5 archive, refetched whenever the range is incomplete.
    if start < archive_cutoff:
        archive_end = min(end, archive_cutoff - timedelta(days=1))
        if _hours_missing(start, archive_end):
            written += backfill_hourly(start, archive_end)

    # Recent tail: forecast endpoint. Always refetched — it is one cheap request
    # and it lets a provisional value be corrected as the model settles.
    if end >= archive_cutoff:
        written += backfill_recent_hourly(
            past_days=max((today - max(start, archive_cutoff)).days + 1, 1))

    if written == 0:
        log.info("Temperature cache already covers %s..%s", start, end)
    return written


def current_temperature_c() -> float | None:
    """Current Denver temperature, for live battery estimates on /api/v1/route.

    Best-effort: on any failure the caller falls back to the model's mean
    training temperature rather than refusing to serve a route.
    """
    try:
        resp = httpx.get(
            FORECAST_URL,
            params={
                "latitude": DENVER_LAT,
                "longitude": DENVER_LON,
                "current": "temperature_2m",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        value = resp.json().get("current", {}).get("temperature_2m")
        return float(value) if value is not None else None
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        log.warning("current temperature lookup failed: %s", exc)
        return None
