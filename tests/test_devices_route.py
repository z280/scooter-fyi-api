"""Router-level guards for /api/v1/devices/current.

The unit tests call the handler functions directly, which missed a
regression where the shared helper `_devices_current_impl` was also
decorated as `GET /api/v1/devices/current` — shadowing the public wrapper,
422-ing normal requests, and exposing `include_plate`/`resource` as query
params. These drive the actual router with TestClient.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_public

_CYCLE_ID = uuid.UUID("8f3a2d10-1234-4abc-8def-0123456789ab")
_SNAP = datetime(2026, 7, 6, 14, 30, tzinfo=timezone.utc)

# 35-column row (r[0]..r[34]) in the exact order _devices_current_impl's
# SELECT produces. The public path never EMITS r[26:29] (the admin-only
# plate fields) but it does read r[30:34] (sql/055's feature columns)
# unconditionally, so the fixture has to carry the full width.
_ROW = (
    "dev1", "scooter", 39.7392, -104.9876, "denver_core",
    "8c4a1f0d2e9b7a35", False, False, 45293, "electric",
    111, 222, 333,
    "75", "40/52", "3100/4100", "3100/6000", "12/40", "3/8", "1/1",
    False, 52800, 0, None, "standing", "Astro",
    None, None, None, None,       # 26-29 admin-only private fields
    "needs_features_confirmed",   # 30 feature_status
    None, None, None,             # 31-33 has_bell/cup_holder/phone_holder
    None,                         # 34 features_poor_condition
    None,                         # 35 has_basket
)


class _FakeCursor:
    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return (_CYCLE_ID, _SNAP)

    def fetchall(self):
        return [_ROW]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def client(monkeypatch):
    @contextmanager
    def _conn():
        yield _FakeConn()

    monkeypatch.setattr(api_public, "connection", _conn)
    monkeypatch.setattr(api_public, "stats_for_cycle", lambda cycle_id, snapshot_time: {})
    app = FastAPI()
    app.include_router(api_public.router)
    return TestClient(app)


def test_single_route_bound_to_public_wrapper():
    routes = [
        r for r in api_public.router.routes
        if getattr(r, "path", None) == "/api/v1/devices/current"
    ]
    assert len(routes) == 1, "duplicate/shadow route registered"
    assert routes[0].endpoint.__name__ == "devices_current"


def test_public_get_needs_no_params(client):
    """No query params → 200 (not 422 from the helper's required kwargs)."""
    r = client.get("/api/v1/devices/current")
    assert r.status_code == 200
    assert r.json()["features"][0]["properties"]["device_id"] == "dev1"


def test_include_plate_query_param_cannot_leak_plates(client):
    """?include_plate=true is just an unknown query param on the public
    route — it must NOT surface the admin-only fields."""
    r = client.get("/api/v1/devices/current?include_plate=true")
    assert r.status_code == 200
    props = r.json()["features"][0]["properties"]
    assert "vehicle_plate" not in props
    assert "first_ever_observed_at" not in props


def test_bad_bbox_is_400_not_422(client):
    r = client.get("/api/v1/devices/current?bbox=nope")
    assert r.status_code == 400
