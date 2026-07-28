"""Deploy-order safety for the failed_unlock -> not_rideable rename
(sql/037).

The button that sends report_type lives in a different repository, so
backend and frontend cannot deploy atomically. report_type is validated by
a pydantic `pattern`, which means a spelling mismatch is a 422 — riders
would see "Couldn't send — please try again" forever and the reliability
signal would stop flowing, in WHICHEVER order the two repos shipped.

So the backend accepts both spellings and normalises to the canonical one
before anything reads it. These tests pin both halves: the old spelling is
accepted, and nothing downstream ever sees it.
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
_TS = datetime(2026, 7, 28, tzinfo=timezone.utc)
_BODY = {"vehicle_identifier": _VID, "lat": 39.7392, "lng": -104.9876}
_USER = SessionUser(
    account_id=42, email="rider@example.com", scopes=("rider",),
    expires_at=_TS, sliding=True, method="google", token_sha256="x",
)


class _FakeCursor:
    def __init__(self, fetch, sink):
        self._fetch = list(fetch)
        self.sink = sink

    def execute(self, sql, params=()):
        self.sink.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._fetch.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, fetch, sink):
        self.cur = _FakeCursor(fetch, sink)

    def cursor(self):
        return self.cur

    def commit(self):
        pass


def _client(monkeypatch, fetch, *, user=_USER):
    sink: list[tuple[str, tuple]] = []
    conn = _FakeConn(fetch, sink)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_frontend_reports, "connection", _fake_connection)
    monkeypatch.setattr(api_frontend_reports, "enforce", lambda cur, **kw: None)
    app = FastAPI()
    app.include_router(api_frontend_reports.router)
    app.dependency_overrides[optional_session] = lambda: user
    return TestClient(app), sink


def _device_insert(sink) -> tuple:
    return next(p for sql, p in sink if sql.startswith("INSERT INTO device_reports"))


@pytest.mark.parametrize("spelling", ["not_rideable", "failed_unlock"])
def test_both_spellings_are_accepted(monkeypatch, spelling):
    """Neither deploy order can 422 a rider's report."""
    client, sink = _client(monkeypatch, [None, (1, _TS), (99, _TS)])
    r = client.post("/api/v1/reports/device", json={**_BODY, "report_type": spelling})
    assert r.status_code == 200, r.text


def test_the_deprecated_spelling_is_stored_as_the_canonical_one(monkeypatch):
    """sql/037's CHECK constraint only permits 'not_rideable', so storing
    the alias verbatim would be a 500 — and would split one signal across
    two values for every reader."""
    client, sink = _client(monkeypatch, [None, (1, _TS), (99, _TS)])
    r = client.post("/api/v1/reports/device",
                    json={**_BODY, "report_type": "failed_unlock"})
    assert r.status_code == 200, r.text
    assert "not_rideable" in _device_insert(sink)
    assert not any("failed_unlock" in str(p) for _sql, p in sink)


def test_the_deprecated_spelling_still_earns_points(monkeypatch):
    """src/points.py maps the CANONICAL name, so an un-normalised alias
    would silently award zero — the rename must not cost the rider points
    for the same action."""
    client, _ = _client(monkeypatch, [None, (1, _TS), (99, _TS)])
    r = client.post("/api/v1/reports/device",
                    json={**_BODY, "report_type": "failed_unlock"})
    assert r.json()["points_awarded"] == 10  # POINTS_REPORT_NOT_RIDEABLE


def test_the_deprecated_spelling_dedupes_against_the_canonical_one(monkeypatch):
    """Normalising at the edge means the 30-minute dedupe probe searches for
    the canonical value, so a rider whose app updates mid-window doesn't get
    a duplicate row."""
    client, sink = _client(monkeypatch, [(7, _TS)])  # dedupe hit
    r = client.post("/api/v1/reports/device",
                    json={**_BODY, "report_type": "failed_unlock"})
    assert r.status_code == 200, r.text
    assert r.json()["deduped"] is True
    dedupe_sql, dedupe_params = sink[0]
    assert "SELECT id, reported_at FROM device_reports" in dedupe_sql
    assert "not_rideable" in dedupe_params


def test_an_unknown_report_type_is_still_rejected(monkeypatch):
    """The alias widens the accepted set by exactly one value, not into a
    free-text column."""
    client, _ = _client(monkeypatch, [])
    r = client.post("/api/v1/reports/device",
                    json={**_BODY, "report_type": "wont_start"})
    assert r.status_code == 422


def test_the_alias_is_marked_for_removal():
    """A deprecated alias with no removal note is just a second spelling
    forever. Fails loudly if someone adds one without saying how it goes
    away."""
    assert api_frontend_reports._DEPRECATED_REPORT_TYPE_ALIASES == {
        "failed_unlock": "not_rideable"}
    import inspect
    source = inspect.getsource(api_frontend_reports)
    marker = source[:source.index("_DEPRECATED_REPORT_TYPE_ALIASES = {")]
    assert "DEPRECATED" in marker and "REMOVAL" in marker
