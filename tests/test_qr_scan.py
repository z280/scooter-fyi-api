"""src/qr.py pure helpers + POST /api/v1/devices/qr-scan.

hash_plate() needs VEHICLE_IDENTIFIER_SALT, which tests/conftest.py
already sets for every test in the suite.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_qr
from src.accounts import SessionUser, require_session
from src.identity import hash_plate
from src.qr import QrValidationError, extract_plate, validate_scan

_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    expires_at=datetime.now(timezone.utc),
    sliding=True, method="google", token_sha256="x",
)
_NOW = datetime.now(timezone.utc)


# ---------- pure helpers ------------------------------------------------------

def test_extract_plate_from_rental_uri_shape():
    assert extract_plate("https://gmjc.adj.st/?adj_t=abc&number=1231234") == "1231234"


def test_extract_plate_falls_back_to_raw_trimmed_value():
    assert extract_plate("  1231234  ") == "1231234"


def test_extract_plate_none_for_blank():
    assert extract_plate("   ") is None


def test_validate_scan_accepts_matching_plate():
    vid = hash_plate("1231234")
    assert validate_scan("https://gmjc.adj.st/?number=1231234", vid) == "1231234"


def test_validate_scan_rejects_mismatched_plate():
    vid = hash_plate("1231234")
    with pytest.raises(QrValidationError, match="does not match"):
        validate_scan("https://gmjc.adj.st/?number=9999999", vid)


def test_validate_scan_rejects_unreadable_payload():
    with pytest.raises(QrValidationError, match="could not read"):
        validate_scan("", "aaaa000000000000")


# ---------- POST /api/v1/devices/qr-scan --------------------------------------

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


def _app():
    app = FastAPI()
    app.include_router(api_qr.router)
    app.dependency_overrides[require_session] = lambda: _USER
    return app


def _client(monkeypatch, fetches):
    conn = _FakeConn(fetches)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_qr, "connection", _fake_connection)
    monkeypatch.setattr(api_qr, "enforce", lambda cur, **kw: None)
    return TestClient(_app()), conn


def _vid():
    return hash_plate("1231234")


def test_scan_mismatch_returns_400_before_touching_the_db(monkeypatch):
    client, conn = _client(monkeypatch, fetches=[])
    r = client.post("/api/v1/devices/qr-scan", json={
        "vehicle_identifier": _vid(), "qr_raw_value": "?number=9999999",
        "lat": 39.74, "lng": -104.99,
    })
    assert r.status_code == 400
    assert conn.cur.executed == []


def test_scan_unknown_device_returns_400(monkeypatch):
    client, _ = _client(monkeypatch, fetches=[None])
    r = client.post("/api/v1/devices/qr-scan", json={
        "vehicle_identifier": _vid(), "qr_raw_value": "?number=1231234",
        "lat": 39.74, "lng": -104.99,
    })
    assert r.status_code == 400


def test_scan_success_awards_points_on_first_scan(monkeypatch):
    # device_state lookup, device_qr_codes upsert, then credit_qr_scan_points'
    # own [lock(no fetch), not-yet-scanned check, credit_points INSERT].
    fetches = [(1,), (1, _NOW), None, (77, _NOW)]
    client, _ = _client(monkeypatch, fetches)
    r = client.post("/api/v1/devices/qr-scan", json={
        "vehicle_identifier": _vid(), "qr_raw_value": "?number=1231234",
        "lat": 39.74, "lng": -104.99,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["points_awarded"] == 100
    assert body["already_scanned_by_you"] is False


def test_scan_success_no_points_on_repeat_scan(monkeypatch):
    fetches = [(1,), (2, _NOW), (1,)]  # already-scanned check finds a row
    client, _ = _client(monkeypatch, fetches)
    r = client.post("/api/v1/devices/qr-scan", json={
        "vehicle_identifier": _vid(), "qr_raw_value": "?number=1231234",
        "lat": 39.74, "lng": -104.99,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["points_awarded"] == 0
    assert body["already_scanned_by_you"] is True
