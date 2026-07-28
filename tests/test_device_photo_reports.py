"""POST /api/v1/photos/{photo_id}/reports."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_device_photos
from src.accounts import SessionUser, require_session

_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    expires_at=datetime.now(timezone.utc),
    sliding=True, method="google", token_sha256="x",
)
_NOW = datetime.now(timezone.utc)


class _FakeCursor:
    def __init__(self, fetches):
        self._fetches = list(fetches)

    def execute(self, *a, **k):
        pass

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


def _client(monkeypatch, fetches):
    conn = _FakeConn(fetches)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_device_photos, "connection", _fake_connection)
    monkeypatch.setattr(api_device_photos, "enforce", lambda cur, **kw: None)
    app = FastAPI()
    app.include_router(api_device_photos.router)
    app.dependency_overrides[require_session] = lambda: _USER
    return TestClient(app)


def test_report_photo_success(monkeypatch):
    c = _client(monkeypatch, fetches=[(1,), (9, "open", _NOW)])
    r = c.post("/api/v1/photos/1/reports", json={"reason": "wrong_device"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deduped"] is False
    assert body["status"] == "open"


def test_report_photo_404_when_photo_missing(monkeypatch):
    c = _client(monkeypatch, fetches=[None])
    r = c.post("/api/v1/photos/999/reports", json={"reason": "other"})
    assert r.status_code == 404


def test_report_photo_dedupes_repeat_report(monkeypatch):
    c = _client(monkeypatch, fetches=[(1,), None])  # INSERT ON CONFLICT DO NOTHING -> no row
    r = c.post("/api/v1/photos/1/reports", json={"reason": "inappropriate"})
    assert r.status_code == 200
    assert r.json()["deduped"] is True


def test_report_photo_rejects_bad_reason():
    app = FastAPI()
    app.include_router(api_device_photos.router)
    app.dependency_overrides[require_session] = lambda: _USER
    r = TestClient(app).post("/api/v1/photos/1/reports", json={"reason": "not-a-real-reason"})
    assert r.status_code == 422
