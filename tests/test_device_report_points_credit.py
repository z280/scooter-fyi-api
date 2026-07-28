"""Points integration for POST /api/v1/reports/device (requirement #10) —
the actually-new behavior added alongside the pre-existing rate-limit
tests in tests/test_device_report_rate_limits.py, which this file mirrors
the fake-cursor idiom of."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_frontend_reports
from src.accounts import SessionUser, optional_session

_VID = "8c4a1f0d2e9b7a35"
_TS = datetime(2026, 7, 5, tzinfo=timezone.utc)
_BODY = {"vehicle_identifier": _VID, "lat": 39.7392, "lng": -104.9876}
_USER = SessionUser(
    account_id=42, email="rider@example.com", scopes=("rider",),
    expires_at=datetime.now(timezone.utc),
    sliding=True, method="google", token_sha256="x",
)


class _FakeCursor:
    def __init__(self, fetch):
        self._fetch = list(fetch)
        self.execute_count = 0

    def execute(self, *a, **k):
        self.execute_count += 1

    def fetchone(self):
        return self._fetch.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, fetch):
        self.cur = _FakeCursor(fetch)

    def cursor(self):
        return self.cur

    def commit(self):
        pass


def _app_authenticated():
    app = FastAPI()
    app.include_router(api_frontend_reports.router)
    app.dependency_overrides[optional_session] = lambda: _USER
    return app


def _client(monkeypatch, fetch):
    conn = _FakeConn(fetch)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_frontend_reports, "connection", _fake_connection)
    monkeypatch.setattr(api_frontend_reports, "enforce", lambda cur, **kw: None)
    return TestClient(_app_authenticated()), conn


def test_authenticated_points_eligible_report_awards_points(monkeypatch):
    # dedup SELECT -> None, device_reports INSERT -> (id, ts),
    # user_points INSERT (inside credit_points) -> (points_id, points_ts).
    fetch = [None, (1, _TS), (99, _TS)]
    client, conn = _client(monkeypatch, fetch)
    r = client.post("/api/v1/reports/device", json={**_BODY, "report_type": "not_rideable"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["points_awarded"] == 10  # POINTS_REPORT_NOT_RIDEABLE
    assert body["deduped"] is False


def test_authenticated_dead_battery_report_awards_no_points(monkeypatch):
    """dead_battery is absent from the points list — only 2 fetches are
    consumed (dedup + insert), confirming credit_report_points never
    reaches a second INSERT for this type."""
    fetch = [None, (1, _TS)]
    client, conn = _client(monkeypatch, fetch)
    r = client.post("/api/v1/reports/device", json={**_BODY, "report_type": "dead_battery"})
    assert r.status_code == 200, r.text
    assert r.json()["points_awarded"] == 0
    assert conn.cur._fetch == []  # exactly 2 fetches consumed, none left over


def test_anonymous_report_never_enters_the_points_path(monkeypatch):
    fetch = [None, (1, _TS)]
    conn = _FakeConn(fetch)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_frontend_reports, "connection", _fake_connection)
    monkeypatch.setattr(api_frontend_reports, "enforce", lambda cur, **kw: None)
    app = FastAPI()
    app.include_router(api_frontend_reports.router)
    r = TestClient(app).post(
        "/api/v1/reports/device", json={**_BODY, "report_type": "not_rideable"})
    assert r.status_code == 200, r.text
    assert r.json()["points_awarded"] == 0


def test_deduped_resubmission_reports_zero_points_awarded(monkeypatch):
    client, _ = _client(monkeypatch, [(7, _TS)])
    r = client.post("/api/v1/reports/device", json={**_BODY, "report_type": "not_rideable"})
    assert r.status_code == 200
    body = r.json()
    assert body["deduped"] is True
    assert body["points_awarded"] == 0
