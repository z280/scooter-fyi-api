"""POST /api/v1/devices/{vehicle_identifier}/recommend."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_device_recommendations
from src.accounts import SessionUser, require_session

_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    supporter=False, expires_at=datetime.now(timezone.utc),
    sliding=True, method="google", token_sha256="x",
)
_VID = "aaaa000000000000"


class _FakeCursor:
    def __init__(self, fetches):
        self._fetches = list(fetches)
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._fetches.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, fetches):
        self.cur = _FakeCursor(fetches)

    def cursor(self):
        return self.cur

    def commit(self):
        pass


@pytest.fixture
def client(monkeypatch):
    def _make(fetches):
        conn = _FakeConn(fetches)

        @contextmanager
        def _fake_connection():
            yield conn

        monkeypatch.setattr(api_device_recommendations, "connection", _fake_connection)
        monkeypatch.setattr(api_device_recommendations, "enforce", lambda cur, **kw: None)
        app = FastAPI()
        app.include_router(api_device_recommendations.router)
        app.dependency_overrides[require_session] = lambda: _USER
        return TestClient(app), conn
    return _make


def test_recommend_rejected_without_a_qualifying_ride(client):
    c, _ = client([None])
    r = c.post(f"/api/v1/devices/{_VID}/recommend", json={"recommend": True})
    assert r.status_code == 403


def test_recommend_accepted_with_a_qualifying_ride(client):
    c, conn = client([(1,), (5, datetime.now(timezone.utc), datetime.now(timezone.utc))])
    r = c.post(f"/api/v1/devices/{_VID}/recommend", json={"recommend": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recommend"] is True
    upsert_sql, _ = next(c for c in conn.cur.executed if "INSERT INTO device_recommendations" in c[0])
    assert "ON CONFLICT (account_id, vehicle_identifier) DO UPDATE" in upsert_sql


def test_recommend_rejects_bad_vehicle_identifier_shape():
    app = FastAPI()
    app.include_router(api_device_recommendations.router)
    app.dependency_overrides[require_session] = lambda: _USER
    r = TestClient(app).post("/api/v1/devices/not-16-hex/recommend", json={"recommend": True})
    assert r.status_code == 422


def test_recommend_requires_signed_in_rider():
    app = FastAPI()
    app.include_router(api_device_recommendations.router)
    r = TestClient(app).post(f"/api/v1/devices/{_VID}/recommend", json={"recommend": True})
    assert r.status_code == 401
