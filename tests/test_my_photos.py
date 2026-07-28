"""GET /api/v1/photos/mine — combines device photos and ride transaction
screenshots into one response (requirement #17)."""

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

    def fetchall(self):
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


def _client(monkeypatch, photo_rows, screenshot_rows):
    conn = _FakeConn([photo_rows, screenshot_rows])

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_device_photos, "connection", _fake_connection)
    monkeypatch.setattr(api_device_photos, "public_photo_url", lambda key: f"https://cdn/{key}")
    monkeypatch.setattr(api_device_photos, "presigned_screenshot_url", lambda key, **kw: f"https://signed/{key}")
    app = FastAPI()
    app.include_router(api_device_photos.router)
    app.dependency_overrides[require_session] = lambda: _USER
    return TestClient(app)


def test_combines_both_photo_types(monkeypatch):
    photo_rows = [(1, "aaaa000000000000", "device-photos/1/a.jpg", _NOW, "visible")]
    screenshot_rows = [(2, "ride-1", "overview", "ride-screenshots/1/b.jpg", _NOW)]
    c = _client(monkeypatch, photo_rows, screenshot_rows)
    r = c.get("/api/v1/photos/mine")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["device_photos"]) == 1
    assert len(body["ride_transaction_screenshots"]) == 1
    assert body["device_photos"][0]["photo_url"] == "https://cdn/device-photos/1/a.jpg"
    assert body["ride_transaction_screenshots"][0]["url"] == "https://signed/ride-screenshots/1/b.jpg"


def test_empty_when_nothing_uploaded(monkeypatch):
    c = _client(monkeypatch, [], [])
    r = c.get("/api/v1/photos/mine")
    assert r.status_code == 200
    assert r.json() == {"device_photos": [], "ride_transaction_screenshots": []}


def test_requires_signed_in_rider():
    app = FastAPI()
    app.include_router(api_device_photos.router)
    r = TestClient(app).get("/api/v1/photos/mine")
    assert r.status_code == 401
