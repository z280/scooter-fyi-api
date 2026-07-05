"""GET /api/v1/private/devices/lookup-batch.

Uses a fake cursor/connection (same pattern as test_api_rides_validation.py)
so this exercises real FastAPI request handling — query parsing, dependency
injection, sorting/found-vs-not_found split — without a live Postgres.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_private
from src.identity import hash_plate
from src.map_auth_dep import MapUser, require_map_user

APOLLO_A = "1025861"
APOLLO_B = "1022675"
COSMO_A = "1014532"
UNSEEN = "9999999"


def _row(plate: str, form_factor: str, max_range: int | None,
         use_type: str | None = None, model_name: str | None = None):
    return (hash_plate(plate), plate, form_factor, max_range, None, None, None,
            use_type, model_name)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


@pytest.fixture
def client(monkeypatch):
    rows = [
        _row(APOLLO_A, "bicycle", 67000, "sitting", "Apollo"),
        _row(APOLLO_B, "bicycle", 66500, "sitting", "Apollo"),
        _row(COSMO_A, "bicycle", 45000, "sitting", "Cosmo"),
    ]

    @contextmanager
    def fake_connection():
        yield _FakeConn(rows)

    monkeypatch.setattr(api_private, "connection", fake_connection)
    app = FastAPI()
    app.include_router(api_private.router)
    app.dependency_overrides[require_map_user] = lambda: MapUser(
        login="tester", orgs=("scooter-club",)
    )
    return TestClient(app)


def test_batch_lookup_returns_found_and_not_found(client):
    r = client.get("/api/v1/private/devices/lookup-batch",
                    params={"plates": f"{APOLLO_A},{APOLLO_B},{COSMO_A},{UNSEEN}"})
    assert r.status_code == 200
    body = r.json()
    assert body["requested"] == 4
    assert body["not_found"] == [UNSEEN]
    assert {d["vehicle_plate"] for d in body["found"]} == {APOLLO_A, APOLLO_B, COSMO_A}


def test_batch_lookup_sorts_by_max_range_descending(client):
    r = client.get("/api/v1/private/devices/lookup-batch",
                    params={"plates": f"{COSMO_A},{APOLLO_A},{APOLLO_B}"})
    plates_in_order = [d["vehicle_plate"] for d in r.json()["found"]]
    assert plates_in_order == [APOLLO_A, APOLLO_B, COSMO_A]


def test_batch_lookup_includes_use_type_and_model_name(client):
    r = client.get("/api/v1/private/devices/lookup-batch",
                    params={"plates": f"{APOLLO_A},{COSMO_A}"})
    by_plate = {d["vehicle_plate"]: d for d in r.json()["found"]}
    assert by_plate[APOLLO_A]["vehicle_model_name"] == "Apollo"
    assert by_plate[APOLLO_A]["vehicle_use_type"] == "sitting"
    assert by_plate[COSMO_A]["vehicle_model_name"] == "Cosmo"


def test_batch_lookup_rejects_empty_plates(client):
    r = client.get("/api/v1/private/devices/lookup-batch", params={"plates": " , ,"})
    assert r.status_code == 400


def test_batch_lookup_rejects_too_many_plates(client):
    many = ",".join(str(1000000 + i) for i in range(201))
    r = client.get("/api/v1/private/devices/lookup-batch", params={"plates": many})
    assert r.status_code == 400


def test_batch_lookup_deduplicates_and_strips_whitespace(client):
    r = client.get("/api/v1/private/devices/lookup-batch",
                    params={"plates": f" {APOLLO_A} , {APOLLO_A},{COSMO_A} "})
    assert r.json()["requested"] == 2
