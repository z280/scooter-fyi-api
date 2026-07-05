"""GET /api/v1/rides `before` parameter validation.

Uses a fake cursor/connection so the test exercises real FastAPI request
handling (query parsing, dependency injection, HTTPException status) without
a live Postgres — the naive-datetime guard runs entirely before any query.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_rides
from src.accounts import SessionUser, require_session


class _FakeCursor:
    def execute(self, *a, **k):
        pass

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


@contextmanager
def _fake_connection():
    yield _FakeConn()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api_rides, "connection", _fake_connection)
    app = FastAPI()
    app.include_router(api_rides.router)
    app.dependency_overrides[require_session] = lambda: SessionUser(
        account_id=1, email="rider@example.com", scopes=("rider",),
        supporter=False, expires_at=datetime.now(timezone.utc),
        sliding=True, method="google", token_sha256="x",
    )
    return TestClient(app)


def test_before_without_timezone_is_rejected(client):
    r = client.get("/api/v1/rides", params={"before": "2026-06-01T00:00:00"})
    assert r.status_code == 400
    assert "timezone" in r.json()["detail"]


def test_before_with_z_suffix_is_accepted(client):
    r = client.get("/api/v1/rides", params={"before": "2026-06-01T00:00:00Z"})
    assert r.status_code == 200


def test_before_with_explicit_offset_is_accepted(client):
    r = client.get("/api/v1/rides", params={"before": "2026-06-01T00:00:00+00:00"})
    assert r.status_code == 200


def test_before_omitted_is_fine(client):
    r = client.get("/api/v1/rides")
    assert r.status_code == 200
