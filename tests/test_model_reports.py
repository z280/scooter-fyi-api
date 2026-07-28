"""POST /api/v1/reports/model — the unrecognized-model review queue (sql/038).

The load-bearing test here is the auth boundary: anonymous callers may
submit TEXT, but a photo requires a session. This is the only endpoint in
the project that pairs an upload with an optional session, so it is the
only place that rule isn't enforced structurally by require_session — it
has to be pinned by a test.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_frontend_reports
from src.accounts import SessionUser, optional_session

_VID = "8c4a1f0d2e9b7a35"
_NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
_USER = SessionUser(
    account_id=7, email="rider@example.com", scopes=("rider",),
    expires_at=_NOW, sliding=True, method="google", token_sha256="x",
)


class _FakeCursor:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=()):
        self.sink.append((" ".join(sql.split()), params))

    def fetchone(self):
        return (1, _NOW)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, sink):
        self.sink = sink

    def cursor(self):
        return _FakeCursor(self.sink)

    def commit(self):
        pass


@pytest.fixture
def ctx(monkeypatch):
    """Returns (client_factory, state). state['stored'] records any photo
    that reached storage — it must stay empty for anonymous callers."""
    state: dict = {"sql": [], "stored": [], "limits": []}

    @contextmanager
    def _conn():
        yield _FakeConn(state["sql"])

    monkeypatch.setattr(api_frontend_reports, "connection", _conn)
    monkeypatch.setattr(api_frontend_reports, "enforce",
                        lambda cur, **kw: state["limits"].append(kw))
    monkeypatch.setattr(api_frontend_reports, "receipts_bucket", lambda: "bucket")
    monkeypatch.setattr(
        api_frontend_reports, "store_model_photo",
        lambda account_id, data: state["stored"].append((account_id, data)) or "key.jpg")

    def _client(user=None):
        app = FastAPI()
        app.include_router(api_frontend_reports.router)
        app.dependency_overrides[optional_session] = lambda: user
        return TestClient(app)

    return _client, state


_TEXT = {"device_id": "dev-1", "description": "Looks like a seated Apollo"}


def test_anonymous_text_only_report_is_accepted(ctx):
    client, state = ctx
    r = client(None).post("/api/v1/reports/model", data=_TEXT)
    assert r.status_code == 200, r.text
    assert r.json()["photo_stored"] is False
    assert state["stored"] == []
    # Anonymous path is limited per IP, not per account.
    assert state["limits"][0]["bucket"] == "model_report_ip"


def test_anonymous_photo_upload_is_rejected(ctx):
    """The whole point: unauthenticated callers cannot push binaries into
    our bucket."""
    client, state = ctx
    r = client(None).post(
        "/api/v1/reports/model", data=_TEXT,
        files={"photo": ("scooter.jpg", b"\xff\xd8\xff-not-really", "image/jpeg")},
    )
    assert r.status_code == 401
    # Nothing was stored, and no row was written.
    assert state["stored"] == []
    assert not any("INSERT INTO model_reports" in sql for sql, _ in state["sql"])


def test_signed_in_photo_upload_is_stored(ctx):
    client, state = ctx
    r = client(_USER).post(
        "/api/v1/reports/model", data=_TEXT,
        files={"photo": ("scooter.jpg", b"\xff\xd8\xff-not-really", "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["photo_stored"] is True
    assert state["stored"] and state["stored"][0][0] == 7
    assert state["limits"][0]["bucket"] == "model_report_account"


def test_store_model_photo_refuses_a_null_account():
    """Defence in depth — the helper itself won't write an ownerless
    object even if a future caller forgets the endpoint's guard."""
    from src.receipts import ReceiptError, store_model_photo

    with pytest.raises(ReceiptError):
        store_model_photo(None, b"\xff\xd8\xff")


def test_description_is_required(ctx):
    client, _ = ctx
    r = client(None).post("/api/v1/reports/model", data={"device_id": "dev-1"})
    assert r.status_code == 422


def test_bad_vehicle_identifier_is_rejected(ctx):
    client, _ = ctx
    r = client(None).post("/api/v1/reports/model",
                          data={**_TEXT, "vehicle_identifier": "NOT-HEX"})
    assert r.status_code == 422


def test_half_a_coordinate_pair_is_rejected(ctx):
    """Storing a lone lat locates nothing — it would just be a column that
    lies about being usable."""
    client, _ = ctx
    r = client(None).post("/api/v1/reports/model", data={**_TEXT, "lat": "39.74"})
    assert r.status_code == 422


def test_coordinates_are_passed_through(ctx):
    client, state = ctx
    r = client(_USER).post("/api/v1/reports/model",
                           data={**_TEXT, "lat": "39.74", "lng": "-104.98",
                                 "vehicle_identifier": _VID})
    assert r.status_code == 200, r.text
    insert = next(p for sql, p in state["sql"] if "INSERT INTO model_reports" in sql)
    assert 39.74 in insert and -104.98 in insert and _VID in insert


def test_json_body_is_refused(ctx):
    """The frontend sends multipart; a JSON body would silently lose the
    photo part, so say so instead of half-accepting it."""
    client, _ = ctx
    r = client(None).post("/api/v1/reports/model", json=_TEXT)
    assert r.status_code == 415
