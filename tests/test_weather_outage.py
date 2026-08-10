"""src/weather.ensure_coverage — an Open-Meteo outage must not fail the caller.

2026-08-09, from the job_runs ledger: a transient `503 Service Unavailable` from
the forecast endpoint aborted the whole of `extract_battery_trips` 497 ms in. No
trips were extracted that day because a third party was briefly down, for one
regressor of four.

Warming the temperature cache is not the caller's purpose. Every consumer
already treats a hole as ordinary: `battery_model._temperature_at` returns None
and the trip is counted in `rejected_no_temperature`, exactly as it is for an
hour Open-Meteo has never published. These pin that an outage costs temperature
values rather than the run — and, just as importantly, that OUR OWN faults still
raise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src import weather


def test_a_503_from_the_forecast_endpoint_does_not_raise(monkeypatch):
    """The exact failure that was observed in production."""
    def boom(*a, **kw):
        raise httpx.HTTPStatusError(
            "Server error '503 Service Unavailable'",
            request=httpx.Request("GET", "https://api.open-meteo.com/v1/forecast"),
            response=httpx.Response(503))

    monkeypatch.setattr(weather, "backfill_recent_hourly", boom)
    monkeypatch.setattr(weather, "_hours_missing", lambda *a, **kw: 0)
    today = datetime.now(timezone.utc).date()
    assert weather.ensure_coverage(today, today) == 0


def test_a_connect_error_on_the_archive_does_not_raise(monkeypatch):
    """Transport failures count too, not just HTTP status codes."""
    def boom(*a, **kw):
        raise httpx.ConnectError("name resolution failed")

    monkeypatch.setattr(weather, "backfill_hourly", boom)
    monkeypatch.setattr(weather, "backfill_recent_hourly", lambda *a, **kw: 0)
    monkeypatch.setattr(weather, "_hours_missing", lambda *a, **kw: 24)
    old = datetime.now(timezone.utc).date() - timedelta(days=30)
    assert weather.ensure_coverage(old, old) == 0


def test_one_source_failing_still_returns_the_others_rows(monkeypatch):
    """A partial outage must not discard the half that worked.

    ensure_coverage draws on two endpoints — the ERA5 archive for anything past
    the reanalysis lag, the forecast endpoint for the recent tail. They fail
    independently, so the return value has to reflect what actually landed.
    """
    monkeypatch.setattr(weather, "_hours_missing", lambda *a, **kw: 24)
    monkeypatch.setattr(weather, "backfill_hourly", lambda *a, **kw: 17)

    def boom(*a, **kw):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(weather, "backfill_recent_hourly", boom)
    today = datetime.now(timezone.utc).date()
    assert weather.ensure_coverage(today - timedelta(days=30), today) == 17


def test_a_bug_in_our_own_upsert_still_raises(monkeypatch):
    """Only httpx failures are swallowed.

    A malformed payload or a database error in `_upsert` is our fault, not a
    third party's, and must still fail the run loudly — the same line
    src/job_runs.py draws between bookkeeping that may never fail a job and
    work that must.
    """
    def boom(*a, **kw):
        raise ValueError("malformed hourly block")

    monkeypatch.setattr(weather, "backfill_recent_hourly", boom)
    monkeypatch.setattr(weather, "_hours_missing", lambda *a, **kw: 0)
    today = datetime.now(timezone.utc).date()
    with pytest.raises(ValueError):
        weather.ensure_coverage(today, today)


def test_a_healthy_fetch_is_unaffected(monkeypatch):
    """The happy path still returns the row count it always did."""
    monkeypatch.setattr(weather, "_hours_missing", lambda *a, **kw: 0)
    monkeypatch.setattr(weather, "backfill_recent_hourly", lambda *a, **kw: 25)
    today = datetime.now(timezone.utc).date()
    assert weather.ensure_coverage(today, today) == 25
