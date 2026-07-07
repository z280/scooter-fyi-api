"""GET /api/v1/private/trips/daily.

Uses a fake cursor/connection (same pattern as test_api_rides_validation.py
and test_api_private_lookup_batch.py) to exercise real FastAPI request
handling without a live Postgres.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_private
from src.accounts import SessionUser, require_admin

SUMMARY_ROW = (42, 30, datetime(2026, 6, 16, 15, 0, tzinfo=timezone.utc))
VEHICLE_ROWS = [
    ("id1", "1026903", "bicycle", "sitting", "Apollo", 5, 1),
    ("id2", "1014532", "bicycle", "sitting", "Cosmo", 3, 2),
]


class _FakeCursor:
    def __init__(self, summary, vehicles):
        self._summary = summary
        self._vehicles = vehicles
        self._last = None

    def execute(self, sql, params=None):
        self._last = "summary" if "daily_trip_summary" in sql else "vehicles"

    def fetchone(self):
        return self._summary

    def fetchall(self):
        return self._vehicles

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, summary, vehicles):
        self._summary = summary
        self._vehicles = vehicles

    def cursor(self):
        return _FakeCursor(self._summary, self._vehicles)


def _client(monkeypatch, summary=SUMMARY_ROW, vehicles=VEHICLE_ROWS):
    @contextmanager
    def fake_connection():
        yield _FakeConn(summary, vehicles)

    monkeypatch.setattr(api_private, "connection", fake_connection)
    app = FastAPI()
    app.include_router(api_private.router)
    app.dependency_overrides[require_admin] = lambda: SessionUser(
        account_id=1, email="admin@example.com", scopes=("rider", "admin"),
        supporter=False, expires_at=datetime.now(timezone.utc),
        sliding=False, method="google", token_sha256="x",
    )
    return TestClient(app)


def test_daily_trips_returns_summary_and_ranked_vehicles(monkeypatch):
    client = _client(monkeypatch)
    r = client.get("/api/v1/private/trips/daily", params={"date": "2026-06-15"})
    assert r.status_code == 200
    body = r.json()
    assert body["total_trips"] == 42
    assert body["distinct_vehicles_tripped"] == 30
    assert body["trip_date"] == "2026-06-15"
    assert [v["vehicle_plate"] for v in body["vehicles"]] == ["1026903", "1014532"]
    assert body["vehicles"][0]["popularity_rank"] == 1
    assert body["vehicles"][0]["vehicle_model_name"] == "Apollo"


def test_daily_trips_404_when_no_rollup_exists(monkeypatch):
    client = _client(monkeypatch, summary=None, vehicles=[])
    r = client.get("/api/v1/private/trips/daily", params={"date": "2026-06-15"})
    assert r.status_code == 404


def test_daily_trips_rejects_bad_date(monkeypatch):
    client = _client(monkeypatch)
    r = client.get("/api/v1/private/trips/daily", params={"date": "not-a-date"})
    assert r.status_code == 400
