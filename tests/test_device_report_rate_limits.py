"""Rate-limit wiring for POST /api/v1/reports/device.

Anonymous callers are capped at 3/hour per IP; authenticated callers at
10/hour per account. This pins both the limit *and* which bucket each path
uses, so a swap (anon<->auth) or a window change can't slip through. It
records the enforce() call rather than driving a real limiter, so no
Postgres is needed.
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


class _FakeCursor:
    def __init__(self):
        # dedup SELECT -> no prior report; INSERT RETURNING -> (id, ts)
        self._fetch = [None, (1, datetime(2026, 7, 5, tzinfo=timezone.utc))]

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self._fetch.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def commit(self):
        pass


@pytest.fixture
def recorded(monkeypatch):
    calls: list[dict] = []

    def fake_enforce(cur, *, bucket, key, limit, window_seconds):
        calls.append({"bucket": bucket, "key": key, "limit": limit,
                      "window_seconds": window_seconds})

    @contextmanager
    def fake_connection():
        yield _FakeConn()

    monkeypatch.setattr(api_frontend_reports, "enforce", fake_enforce)
    monkeypatch.setattr(api_frontend_reports, "connection", fake_connection)
    return calls


def _app():
    app = FastAPI()
    app.include_router(api_frontend_reports.router)
    return app


def test_anonymous_report_is_limited_to_3_per_hour_per_ip(recorded):
    client = TestClient(_app())
    # lat/lng supplied so the handler doesn't take the device_state h3 lookup.
    r = client.post("/api/v1/reports/device", json={
        "vehicle_identifier": _VID, "report_type": "failed_unlock",
        "lat": 39.7392, "lng": -104.9876,
    })
    assert r.status_code == 200
    assert len(recorded) == 1
    call = recorded[0]
    assert call["bucket"] == "device_report_ip"
    assert call["limit"] == 3
    assert call["window_seconds"] == 3600


def test_authenticated_report_is_limited_to_10_per_hour_per_account(recorded):
    app = _app()
    app.dependency_overrides[optional_session] = lambda: SessionUser(
        account_id=42, email="rider@example.com", scopes=("rider",),
        supporter=False, expires_at=datetime.now(timezone.utc),
        sliding=True, method="google", token_sha256="x",
    )
    client = TestClient(app)
    r = client.post("/api/v1/reports/device", json={
        "vehicle_identifier": _VID, "report_type": "dead_battery",
        "lat": 39.7392, "lng": -104.9876,
    })
    assert r.status_code == 200
    assert len(recorded) == 1
    call = recorded[0]
    assert call["bucket"] == "device_report_account"
    assert call["key"] == "42"
    assert call["limit"] == 10
    assert call["window_seconds"] == 3600
