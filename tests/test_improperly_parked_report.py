"""The 'improperly_parked' device report type (parking complaints).

Parking complaints are a first-class device report: stored, rate-limited,
and counted in the reports summary/export compliance signal exactly like the
failure types. They are deliberately kept OUT of has_negative_report /
reliability_tier — a badly-parked scooter can still be a great ride — which
these tests pin via reliability_report_type_sql().

enforce()/connection() are faked (same harness as
test_device_report_rate_limits.py), so no Postgres is needed.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_frontend_reports
from src.api_frontend_reports import (
    NON_RELIABILITY_REPORT_TYPES,
    _REPORT_TYPES,
    reliability_report_type_sql,
)

_VID = "8c4a1f0d2e9b7a35"
_TS = datetime(2026, 7, 5, tzinfo=timezone.utc)


class _FakeCursor:
    def __init__(self, fetch):
        self._fetch = list(fetch)

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self._fetch.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, fetch):
        self._fetch = fetch

    def cursor(self):
        return _FakeCursor(self._fetch)

    def commit(self):
        pass


def _app():
    app = FastAPI()
    app.include_router(api_frontend_reports.router)
    return app


def _install(monkeypatch, fetch):
    def fake_enforce(cur, **kw):
        pass

    @contextmanager
    def fake_connection():
        yield _FakeConn(fetch)

    monkeypatch.setattr(api_frontend_reports, "enforce", fake_enforce)
    monkeypatch.setattr(api_frontend_reports, "connection", fake_connection)


# Fresh (non-dup) submit: dedup SELECT -> None, INSERT RETURNING -> (id, ts).
_FRESH = [None, (1, _TS)]
_BODY = {"vehicle_identifier": _VID, "lat": 39.7392, "lng": -104.9876}


def test_improperly_parked_is_accepted(monkeypatch):
    _install(monkeypatch, _FRESH)
    r = TestClient(_app()).post(
        "/api/v1/reports/device",
        json={**_BODY, "report_type": "improperly_parked"},
    )
    assert r.status_code == 200
    assert r.json()["deduped"] is False


def test_unknown_report_type_is_rejected():
    r = TestClient(_app()).post(
        "/api/v1/reports/device",
        json={**_BODY, "report_type": "on_fire"},
    )
    assert r.status_code == 422


def test_excluded_types_are_a_subset_of_all_types():
    # Guards against typo drift: every non-reliability type must be a real,
    # storable report type.
    assert set(NON_RELIABILITY_REPORT_TYPES) <= set(_REPORT_TYPES)
    assert "improperly_parked" in NON_RELIABILITY_REPORT_TYPES


def test_reliability_clause_excludes_parking_and_keeps_failures():
    clause = reliability_report_type_sql("dr")
    # Parking is filtered out of the has_negative_report / reliability signal…
    assert "improperly_parked" in clause
    assert clause.startswith("dr.report_type NOT IN (")
    # …while the ride-affecting failure types are NOT named in the exclusion,
    # so they still count toward reliability.
    for keep in ("failed_unlock", "dead_battery", "damaged"):
        assert keep not in clause
