"""GET /api/v1/points — mirrors tests/test_api_rides_validation.py's
`before` param validation pattern."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_points
from src.accounts import SessionUser, require_session

_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    supporter=False, expires_at=datetime.now(timezone.utc),
    sliding=True, method="google", token_sha256="x",
)


class _FakeCursor:
    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return (42,)

    def fetchall(self):
        return []

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
def client(monkeypatch):
    @contextmanager
    def _fake_connection():
        yield _FakeConn()

    monkeypatch.setattr(api_points, "connection", _fake_connection)
    app = FastAPI()
    app.include_router(api_points.router)
    app.dependency_overrides[require_session] = lambda: _USER
    return TestClient(app)


def test_returns_total_and_empty_entries(client):
    r = client.get("/api/v1/points")
    assert r.status_code == 200
    assert r.json() == {"total_points": 42, "entries": []}


def test_before_without_timezone_is_rejected(client):
    r = client.get("/api/v1/points", params={"before": "2026-06-01T00:00:00"})
    assert r.status_code == 400
    assert "timezone" in r.json()["detail"]


def test_before_with_z_suffix_is_accepted(client):
    r = client.get("/api/v1/points", params={"before": "2026-06-01T00:00:00Z"})
    assert r.status_code == 200


def test_before_omitted_is_fine(client):
    r = client.get("/api/v1/points")
    assert r.status_code == 200


def test_requires_signed_in_rider():
    app = FastAPI()
    app.include_router(api_points.router)
    r = TestClient(app).get("/api/v1/points")
    assert r.status_code == 401
