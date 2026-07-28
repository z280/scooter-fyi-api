"""POST /api/v1/reports/discount — the rate limit must meter the receipt
upload, not just the row insert.

Same defect and same fix as POST /api/v1/reports/model
(tests/test_model_reports.py): store_receipt is an EXIF strip + re-encode
of up to 10 MB followed by an R2 PUT, with an R2 DELETE on the rollback
path. Running enforce() after all of that left the expensive half of the
handler unpriced while the 20/day cap protected the cheapest part.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src import api_frontend_reports
from src.accounts import SessionUser, require_session

_NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
_USER = SessionUser(
    account_id=7, email="rider@example.com", scopes=("rider",),
    expires_at=_NOW, sliding=True, method="google", token_sha256="x",
)
_FIELDS = {"ride_ended_at": _NOW.isoformat(), "zone_version": "v1",
           "amount_charged_cents": "450"}


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
    state: dict = {"sql": [], "order": []}

    @contextmanager
    def _conn():
        yield _FakeConn(state["sql"])

    monkeypatch.setattr(api_frontend_reports, "connection", _conn)
    monkeypatch.setattr(api_frontend_reports, "enforce",
                        lambda cur, **kw: state["order"].append("RATELIMIT"))
    monkeypatch.setattr(api_frontend_reports, "receipts_bucket", lambda: "bucket")
    monkeypatch.setattr(
        api_frontend_reports, "store_receipt",
        lambda account_id, data: state["order"].append("R2_PUT") or "receipt.jpg")
    monkeypatch.setattr(api_frontend_reports, "delete_receipt",
                        lambda key: state["order"].append("R2_DELETE"))

    app = FastAPI()
    app.include_router(api_frontend_reports.router)
    app.dependency_overrides[require_session] = lambda: _USER
    return TestClient(app), state


def test_rate_limit_runs_before_the_receipt_upload(ctx):
    client, state = ctx
    r = client.post(
        "/api/v1/reports/discount", data=_FIELDS,
        files={"receipt": ("receipt.jpg", b"\xff\xd8\xff-not-really", "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["receipt_stored"] is True
    assert state["order"] == ["RATELIMIT", "R2_PUT"]


def test_a_rejected_caller_never_reaches_storage(ctx, monkeypatch):
    client, state = ctx

    def _deny(cur, **kw):
        state["order"].append("RATELIMIT")
        raise HTTPException(429, "rate limit exceeded — try again later")

    monkeypatch.setattr(api_frontend_reports, "enforce", _deny)
    r = client.post(
        "/api/v1/reports/discount", data=_FIELDS,
        files={"receipt": ("receipt.jpg", b"\xff\xd8\xff-not-really", "image/jpeg")},
    )
    assert r.status_code == 429
    assert state["order"] == ["RATELIMIT"]


def test_json_submission_without_a_receipt_still_works(ctx):
    """The reordering must not change the no-receipt path."""
    client, state = ctx
    r = client.post("/api/v1/reports/discount", json={
        "ride_ended_at": _NOW.isoformat(), "zone_version": "v2",
        "end_lat": 39.74, "end_lng": -104.98,
    })
    assert r.status_code == 200, r.text
    assert r.json()["receipt_stored"] is False
    assert state["order"] == ["RATELIMIT"]
