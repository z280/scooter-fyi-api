"""POST/GET /api/v1/devices/{vehicle_identifier}/photos. store_device_photo
is monkeypatched directly (not boto3) — simpler, and no existing precedent
in this codebase mocks boto3 directly."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_device_photos
from src.accounts import SessionUser, require_session
from src.device_photos import DevicePhotoError

_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    supporter=False, expires_at=datetime.now(timezone.utc),
    sliding=True, method="google", token_sha256="x",
)
_VID = "aaaa000000000000"
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
    app.include_router(api_device_photos.router)
    app.dependency_overrides[require_session] = lambda: _USER
    return app


def _client(monkeypatch, fetches, bucket="devbucket"):
    conn = _FakeConn(fetches)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_device_photos, "connection", _fake_connection)
    monkeypatch.setattr(api_device_photos, "enforce", lambda cur, **kw: None)
    monkeypatch.setattr(api_device_photos, "device_photos_bucket", lambda: bucket)
    monkeypatch.setattr(api_device_photos, "store_device_photo", lambda aid, data: "device-photos/1/x.jpg")
    monkeypatch.setattr(api_device_photos, "public_photo_url", lambda key: f"https://cdn.example/{key}")
    return TestClient(_app()), conn


def _upload(client):
    return client.post(f"/api/v1/devices/{_VID}/photos", files={"photo": ("t.jpg", b"fake", "image/jpeg")})


def test_upload_success(monkeypatch):
    client, _ = _client(monkeypatch, fetches=[(0,), (5, _NOW)])
    r = _upload(client)
    assert r.status_code == 200, r.text
    assert r.json()["photo_url"] == "https://cdn.example/device-photos/1/x.jpg"


def test_upload_rejected_at_cap(monkeypatch):
    client, _ = _client(monkeypatch, fetches=[(3,)])  # already at MAX_PHOTOS_PER_DEVICE
    r = _upload(client)
    assert r.status_code == 409


def test_upload_503_when_storage_unconfigured(monkeypatch):
    client, _ = _client(monkeypatch, fetches=[], bucket=None)
    r = _upload(client)
    assert r.status_code == 503


def test_upload_400_on_device_photo_error(monkeypatch):
    client, _ = _client(monkeypatch, fetches=[(0,)])

    def _boom(aid, data):
        raise DevicePhotoError("upload is not a readable image")

    monkeypatch.setattr(api_device_photos, "store_device_photo", _boom)
    r = _upload(client)
    assert r.status_code == 400


def test_upload_requires_a_photo_field():
    r = TestClient(_app()).post(f"/api/v1/devices/{_VID}/photos", data={"not_photo": "x"})
    assert r.status_code == 422


def test_list_attributes_photo_to_uploaders_public_username(monkeypatch):
    rows = [(1, "device-photos/1/a.jpg", _NOW, "brave🦉")]
    client, _ = _client(monkeypatch, fetches=[rows])
    r = client.get(f"/api/v1/devices/{_VID}/photos")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["photos"][0]["uploaded_by"] == "brave🦉"


def test_list_requires_signed_in_rider():
    app = FastAPI()
    app.include_router(api_device_photos.router)
    r = TestClient(app).get(f"/api/v1/devices/{_VID}/photos")
    assert r.status_code == 401
