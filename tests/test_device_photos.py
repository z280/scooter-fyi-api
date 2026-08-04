"""POST/GET /api/v1/devices/{vehicle_identifier}/photos. store_device_photo
is monkeypatched directly (not boto3) — simpler, and no existing precedent
in this codebase mocks boto3 directly."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_device_photos, points
from src.accounts import SessionUser, require_session
from src.device_photos import DevicePhotoError

_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    expires_at=datetime.now(timezone.utc),
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


def _upload(client, **data):
    return client.post(
        f"/api/v1/devices/{_VID}/photos",
        files={"photo": ("t.jpg", b"fake", "image/jpeg")},
        data=data,
    )


# Fetch sequence for a successful upload that also credits points:
#   count -> insert RETURNING -> [device_state coords] -> points INSERT RETURNING
# The coords fetch is skipped when the client sent lat/lng itself.
_COORDS = (39.7392, -104.9876)
_CREDITED = (77, _NOW)


def test_upload_success(monkeypatch):
    client, _ = _client(monkeypatch, fetches=[(0,), (5, _NOW), _COORDS, _CREDITED])
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


# ---------------------------------------------------------------------------
# Points (sql/056). One credit per accepted upload, POINTS_DEVICE_PHOTO each.
# The award needs a real lat/lng for its ledger row, so these cover where that
# location comes from — and what happens when there isn't one.
# ---------------------------------------------------------------------------


def _points_insert(conn):
    """The INSERT INTO user_points the upload made, or None."""
    for sql, params in conn.cur.executed:
        if sql.startswith("INSERT INTO user_points"):
            return params
    return None


def test_upload_credits_points_using_the_riders_own_coordinates(monkeypatch):
    # lat/lng supplied -> no device_state lookup, so no coords fetch queued.
    client, conn = _client(monkeypatch, fetches=[(0,), (5, _NOW), _CREDITED])
    r = _upload(client, lat="39.7392", lng="-104.9876")
    assert r.status_code == 200, r.text
    assert r.json()["points_awarded"] == points.POINTS_DEVICE_PHOTO

    params = _points_insert(conn)
    assert params is not None
    account_id, action, awarded, lat, lng = params[0], params[1], params[2], params[3], params[4]
    assert (account_id, action, awarded) == (_USER.account_id, "device_photo",
                                             points.POINTS_DEVICE_PHOTO)
    assert (lat, lng) == (39.7392, -104.9876)
    # Attributed to the photo, which is what makes the credit idempotent.
    assert params[-2:] == ("device_photos", "5")
    assert "device_state" not in " ".join(s for s, _ in conn.cur.executed)


def test_upload_falls_back_to_the_vehicles_last_known_position(monkeypatch):
    client, conn = _client(monkeypatch, fetches=[(0,), (5, _NOW), _COORDS, _CREDITED])
    r = _upload(client)  # no coords from the client
    assert r.status_code == 200, r.text
    assert r.json()["points_awarded"] == points.POINTS_DEVICE_PHOTO
    params = _points_insert(conn)
    assert (params[3], params[4]) == _COORDS


def test_upload_still_stores_the_photo_when_no_location_resolves(monkeypatch):
    # Unknown device / never-observed position -> (None, None) -> no award.
    client, conn = _client(monkeypatch, fetches=[(0,), (5, _NOW), None])
    r = _upload(client)
    assert r.status_code == 200, r.text
    assert r.json()["points_awarded"] == 0
    assert r.json()["photo_url"]  # the photo is kept regardless
    assert _points_insert(conn) is None


def test_malformed_or_out_of_range_coords_are_dropped_not_fatal(monkeypatch):
    for bad in ({"lat": "not-a-number", "lng": "-104.9"},
                {"lat": "91", "lng": "-104.9"},      # out of range
                {"lat": "39.7", "lng": "181"},       # out of range
                {"lat": "39.7"}):                    # half a pair
        client, conn = _client(monkeypatch, fetches=[(0,), (5, _NOW), _COORDS, _CREDITED])
        r = _upload(client, **bad)
        # Upload succeeds and falls back to the device's position rather than
        # 422-ing on a field the rider never sees.
        assert r.status_code == 200, (bad, r.text)
        assert (_points_insert(conn)[3], _points_insert(conn)[4]) == _COORDS


def test_the_award_is_even_because_the_ledger_will_not_take_odd(monkeypatch):
    # The owner asked for 5; sql/053's CHECK and credit_points' assert both
    # reject odd awards, which is why this constant is 6.
    assert points.POINTS_DEVICE_PHOTO % 2 == 0


def test_upload_is_never_anonymous(monkeypatch):
    # No session override -> 401 before any storage or ledger work happens.
    app = FastAPI()
    app.include_router(api_device_photos.router)
    r = TestClient(app).post(
        f"/api/v1/devices/{_VID}/photos",
        files={"photo": ("t.jpg", b"fake", "image/jpeg")},
    )
    assert r.status_code == 401
