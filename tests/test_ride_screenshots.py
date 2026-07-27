"""POST/GET /api/v1/tracked-rides/{ride_id}/screenshots."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_ride_screenshots
from src.accounts import SessionUser, require_session
from src.ride_screenshots import RideScreenshotError

_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    supporter=False, expires_at=datetime.now(timezone.utc),
    sliding=True, method="google", token_sha256="x",
)
_RIDE_ID = uuid.uuid4()
_NOW = datetime.now(timezone.utc)


class _FakeCursor:
    def __init__(self, fetches):
        self._fetches = list(fetches)
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._fetches.pop(0)

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


def _app():
    app = FastAPI()
    app.include_router(api_ride_screenshots.router)
    app.dependency_overrides[require_session] = lambda: _USER
    return app


def _client(monkeypatch, fetches, bucket="receiptbucket"):
    conn = _FakeConn(fetches)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_ride_screenshots, "connection", _fake_connection)
    monkeypatch.setattr(api_ride_screenshots, "enforce", lambda cur, **kw: None)
    monkeypatch.setattr(api_ride_screenshots, "screenshots_bucket", lambda: bucket)
    monkeypatch.setattr(api_ride_screenshots, "store_screenshot", lambda aid, data: "ride-screenshots/1/new.jpg")
    monkeypatch.setattr(api_ride_screenshots, "presigned_screenshot_url", lambda key, **kw: f"https://signed/{key}")
    return TestClient(_app()), conn


def _upload(client, screenshot_type="overview"):
    return client.post(
        f"/api/v1/tracked-rides/{_RIDE_ID}/screenshots",
        params={"screenshot_type": screenshot_type},
        files={"screenshot": ("s.jpg", b"fake", "image/jpeg")},
    )


def test_upload_first_screenshot(monkeypatch):
    client, _ = _client(monkeypatch, fetches=[(1,), None, (1, _NOW, _NOW)])
    r = _upload(client)
    assert r.status_code == 200, r.text
    assert r.json()["replaced_previous"] is False


def test_upload_overwrites_and_deletes_superseded_object(monkeypatch):
    deleted = []
    client, _ = _client(monkeypatch, fetches=[(1,), ("ride-screenshots/1/old.jpg",), (1, _NOW, _NOW)])
    monkeypatch.setattr(api_ride_screenshots, "delete_screenshot", lambda key: deleted.append(key))
    r = _upload(client)
    assert r.status_code == 200, r.text
    assert r.json()["replaced_previous"] is True
    assert deleted == ["ride-screenshots/1/old.jpg"]


def test_upload_404_when_not_your_ride(monkeypatch):
    client, _ = _client(monkeypatch, fetches=[None])
    r = _upload(client)
    assert r.status_code == 404


def test_upload_503_when_storage_unconfigured(monkeypatch):
    client, _ = _client(monkeypatch, fetches=[], bucket=None)
    r = _upload(client)
    assert r.status_code == 503


def test_upload_rejects_bad_screenshot_type():
    r = TestClient(_app()).post(
        f"/api/v1/tracked-rides/{_RIDE_ID}/screenshots",
        params={"screenshot_type": "not-a-real-type"},
        files={"screenshot": ("s.jpg", b"fake", "image/jpeg")},
    )
    assert r.status_code == 422


def test_upload_rejects_bad_ride_id():
    r = TestClient(_app()).post(
        "/api/v1/tracked-rides/not-a-uuid/screenshots",
        params={"screenshot_type": "overview"},
        files={"screenshot": ("s.jpg", b"fake", "image/jpeg")},
    )
    assert r.status_code == 400


def test_list_screenshots(monkeypatch):
    rows = [(1, "overview", "key1", _NOW, _NOW), (2, "receipt", "key2", _NOW, _NOW)]
    client, _ = _client(monkeypatch, fetches=[(1,), rows])
    r = client.get(f"/api/v1/tracked-rides/{_RIDE_ID}/screenshots")
    assert r.status_code == 200, r.text
    assert len(r.json()["screenshots"]) == 2


def test_list_404_when_not_your_ride(monkeypatch):
    client, _ = _client(monkeypatch, fetches=[None])
    r = client.get(f"/api/v1/tracked-rides/{_RIDE_ID}/screenshots")
    assert r.status_code == 404
