"""Rate-limit wiring for POST /api/v1/reports/device.

Anonymous callers are capped at 3/hour per IP; authenticated callers at
10/hour per account. These tests pin (a) the limit and bucket each path
uses, and (b) that a deduped no-op submission does NOT consume quota.
enforce() is recorded rather than driven, so no Postgres is needed.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_frontend_reports
from src.accounts import SessionUser, optional_session

_VID = "8c4a1f0d2e9b7a35"
_TS = datetime(2026, 7, 5, tzinfo=timezone.utc)


class _FakeCursor:
    def __init__(self, fetch):
        self._fetch = list(fetch)

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self._fetch.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, fetch):
        self._fetch = fetch

    def cursor(self):
        return _FakeCursor(self._fetch)

    def commit(self):
        pass


def _app():
    app = FastAPI()
    app.include_router(api_frontend_reports.router)
    return app


def _install(monkeypatch, fetch):
    """Wire a recording enforce() + a fake connection whose cursor returns
    `fetch` from successive fetchone() calls. Returns the recorded list."""
    calls: list[dict] = []

    def fake_enforce(cur, **kw):
        calls.append(kw)

    @contextmanager
    def fake_connection():
        yield _FakeConn(fetch)

    monkeypatch.setattr(api_frontend_reports, "enforce", fake_enforce)
    monkeypatch.setattr(api_frontend_reports, "connection", fake_connection)
    return calls


# A fresh (non-dup) submit: dedup SELECT -> None, INSERT RETURNING -> (id, ts).
_FRESH = [None, (1, _TS)]
_BODY = {"vehicle_identifier": _VID, "lat": 39.7392, "lng": -104.9876}


def test_anonymous_report_is_limited_to_3_per_hour_per_ip(monkeypatch):
    calls = _install(monkeypatch, _FRESH)
    r = TestClient(_app()).post(
        "/api/v1/reports/device", json={**_BODY, "report_type": "not_rideable"})
    assert r.status_code == 200
    assert len(calls) == 1
    assert calls[0]["bucket"] == "device_report_ip"
    assert calls[0]["limit"] == 3
    assert calls[0]["window_seconds"] == 3600


def test_authenticated_report_is_limited_to_10_per_hour_per_account(monkeypatch):
    calls = _install(monkeypatch, _FRESH)
    app = _app()
    app.dependency_overrides[optional_session] = lambda: SessionUser(
        account_id=42, email="rider@example.com", scopes=("rider",),
        expires_at=datetime.now(timezone.utc),
        sliding=True, method="google", token_sha256="x",
    )
    r = TestClient(app).post(
        "/api/v1/reports/device", json={**_BODY, "report_type": "dead_battery"})
    assert r.status_code == 200
    assert len(calls) == 1
    assert calls[0]["bucket"] == "device_report_account"
    assert calls[0]["key"] == "42"
    assert calls[0]["limit"] == 10
    assert calls[0]["window_seconds"] == 3600


def test_deduped_resubmission_does_not_consume_rate_limit_quota(monkeypatch):
    """A no-op deduped report must NOT call enforce() — otherwise an
    impatient rider re-tapping one scooter burns their tight anon budget
    and gets 429'd reporting a DIFFERENT broken scooter (the finding that
    moved the dedup probe ahead of the rate-limit check)."""
    # dedup SELECT -> an existing report (id=7); no INSERT should follow.
    calls = _install(monkeypatch, [(7, _TS)])
    r = TestClient(_app()).post(
        "/api/v1/reports/device", json={**_BODY, "report_type": "not_rideable"})
    assert r.status_code == 200
    body = r.json()
    assert body["deduped"] is True
    assert body["id"] == 7
    assert calls == [], "deduped no-op must not consume rate-limit quota"
