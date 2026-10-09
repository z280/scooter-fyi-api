"""Fleet reports Phase 1 (docs/FLEET_REPORTS_PLAN.md), the parts a fake DB
can pin. The SQL itself runs against real Postgres in
tests/test_fleet_reports_pg.py.

  * the `inaccessible` type: accepted, excluded from reliability, no points;
  * "why not rideable": the six reasons, the two decoys the server re-files,
    and that a client sending neither still works;
  * observed_at: a date or a timestamp, defaulted, never future, never older
    than 30 days;
  * suppression fields on /devices/current, and that an outage reads null;
  * the public CSV never carries an inaccessible report's location;
  * the identify extension's input rules (qr= / plate=, unreadable QRs);
  * every census, export, resolve and dossier route is admin-only.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src import accounts, api_fleet_reports, api_frontend_reports, api_public, fleet_reports
from src import api_vehicle_plates
from src.accounts import SessionUser, optional_session
from src.api_frontend_reports import (
    NON_RELIABILITY_REPORT_TYPES,
    NOT_RIDEABLE_DECOYS,
    NOT_RIDEABLE_REASONS,
    _REPORT_TYPES,
    reliability_report_type_sql,
)
from src.log_redaction import redact_url
from src.points import REPORT_TYPE_POINTS

_VID = "8c4a1f0d2e9b7a35"
_TS = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
_BODY = {"vehicle_identifier": _VID, "lat": 39.7392, "lng": -104.9876}
_USER = SessionUser(
    account_id=42, email="rider@example.com", scopes=("rider",),
    expires_at=_TS, sliding=True, method="google", token_sha256="x",
)


# ---------------------------------------------------------------------------
# A fake connection that records statements, like test_report_type_alias's
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, fetch, sink):
        self._fetch = fetch
        self.sink = sink

    def execute(self, sql, params=()):
        self.sink.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._fetch.pop(0)

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, fetch, sink):
        self.cur = _Cursor(fetch, sink)

    def cursor(self):
        return self.cur

    def commit(self):
        pass


def _client(monkeypatch, fetch, *, user=_USER):
    sink: list[tuple[str, tuple]] = []
    conn = _Conn(fetch, sink)

    @contextmanager
    def _fake():
        yield conn

    monkeypatch.setattr(api_frontend_reports, "connection", _fake)
    monkeypatch.setattr(api_frontend_reports, "enforce", lambda cur, **kw: None)
    monkeypatch.setattr(api_frontend_reports, "credit_report_points",
                        lambda cur, **kw: {"points": 10})
    app = FastAPI()
    app.include_router(api_frontend_reports.router)
    app.dependency_overrides[optional_session] = lambda: user
    return TestClient(app), sink


_FRESH = [None, (1, _TS)]  # dedupe miss, then INSERT ... RETURNING


def _insert(sink) -> tuple:
    return next(p for sql, p in sink if sql.startswith("INSERT INTO device_reports"))


def _insert_sql(sink) -> str:
    return next(sql for sql, _p in sink if sql.startswith("INSERT INTO device_reports"))


# ---------------------------------------------------------------------------
# The inaccessible type
# ---------------------------------------------------------------------------

def test_inaccessible_is_a_storable_type_outside_reliability():
    assert "inaccessible" in _REPORT_TYPES
    assert "inaccessible" in NON_RELIABILITY_REPORT_TYPES
    clause = reliability_report_type_sql("dr")
    assert "'inaccessible'" in clause and "'improperly_parked'" in clause
    for keep in ("not_rideable", "dead_battery", "damaged", "not_found"):
        assert keep not in clause


def test_inaccessible_earns_no_points():
    # Paying for a report that hides a vehicle from every rider pays for
    # griefing (plan risk 2).
    assert "inaccessible" not in REPORT_TYPE_POINTS


def test_inaccessible_is_accepted_and_stored(monkeypatch):
    client, sink = _client(monkeypatch, list(_FRESH))
    r = client.post("/api/v1/reports/device", json={**_BODY, "report_type": "inaccessible"})
    assert r.status_code == 200, r.text
    assert "inaccessible" in _insert(sink)


def test_every_report_type_has_a_suppression_priority():
    # A type missing from the priority tuple would never suppress, silently.
    assert sorted(fleet_reports.SUPPRESSION_REASON_PRIORITY) == sorted(_REPORT_TYPES)
    assert fleet_reports.SUPPRESSION_REASON_PRIORITY[0] == "inaccessible"


def test_the_insert_records_the_charge_at_report_time(monkeypatch):
    # §2.4: the report must remember the range so it clears on a RISE.
    client, sink = _client(monkeypatch, list(_FRESH))
    client.post("/api/v1/reports/device", json={**_BODY, "report_type": "not_rideable"})
    sql = _insert_sql(sink)
    assert "range_at_report_meters" in sql
    assert "FROM raw_telemetry_points" in sql


# ---------------------------------------------------------------------------
# Why not rideable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason", NOT_RIDEABLE_REASONS)
def test_each_reason_is_stored_on_a_not_rideable_report(monkeypatch, reason):
    client, sink = _client(monkeypatch, list(_FRESH))
    r = client.post("/api/v1/reports/device",
                    json={**_BODY, "report_type": "not_rideable", "reason": reason})
    assert r.status_code == 200, r.text
    params = _insert(sink)
    assert params[1] == "not_rideable"
    assert reason in params
    assert "report_type" not in r.json()  # not remapped


def test_the_six_reasons_are_the_owners():
    assert NOT_RIDEABLE_REASONS == (
        "acceleration", "flat_tire", "wheel", "lighting", "seat", "handlebar")


@pytest.mark.parametrize("decoy,stored", sorted(NOT_RIDEABLE_DECOYS.items()))
def test_a_decoy_reason_refiles_the_report(monkeypatch, decoy, stored):
    client, sink = _client(monkeypatch, list(_FRESH))
    r = client.post("/api/v1/reports/device",
                    json={**_BODY, "report_type": "not_rideable", "reason": decoy})
    assert r.status_code == 200, r.text
    params = _insert(sink)
    assert params[1] == stored
    # reason is dropped (it was not a reason), the choice is kept.
    sql = _insert_sql(sink)
    cols = sql.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
    assert params[cols.index("reason")] is None
    assert params[cols.index("submitted_reason")] == decoy
    assert r.json()["report_type"] == stored
    assert r.json()["remapped_from_reason"] == decoy


def test_the_decoys_map_as_the_owner_said():
    assert NOT_RIDEABLE_DECOYS == {"cannot_find": "inaccessible",
                                   "dead_battery": "dead_battery"}


def test_a_decoy_dedupes_against_the_type_it_becomes(monkeypatch):
    client, sink = _client(monkeypatch, [(7, _TS)])
    r = client.post("/api/v1/reports/device",
                    json={**_BODY, "report_type": "not_rideable", "reason": "cannot_find"})
    assert r.json()["deduped"] is True
    assert "inaccessible" in sink[0][1]


def test_a_decoy_works_through_the_deprecated_alias(monkeypatch):
    client, sink = _client(monkeypatch, list(_FRESH))
    r = client.post("/api/v1/reports/device",
                    json={**_BODY, "report_type": "failed_unlock", "reason": "dead_battery"})
    assert r.status_code == 200, r.text
    assert _insert(sink)[1] == "dead_battery"


@pytest.mark.parametrize("rtype", ["damaged", "dead_battery", "inaccessible",
                                   "improperly_parked", "not_found"])
def test_a_reason_on_another_type_is_refused(monkeypatch, rtype):
    client, _ = _client(monkeypatch, list(_FRESH))
    r = client.post("/api/v1/reports/device",
                    json={**_BODY, "report_type": rtype, "reason": "seat"})
    assert r.status_code == 422


def test_an_unknown_reason_is_refused(monkeypatch):
    client, _ = _client(monkeypatch, list(_FRESH))
    r = client.post("/api/v1/reports/device",
                    json={**_BODY, "report_type": "not_rideable", "reason": "vibes"})
    assert r.status_code == 422


def test_a_client_cannot_set_submitted_reason_itself(monkeypatch):
    client, sink = _client(monkeypatch, list(_FRESH))
    r = client.post("/api/v1/reports/device",
                    json={**_BODY, "report_type": "not_rideable",
                          "submitted_reason": "cannot_find"})
    assert r.status_code == 200
    params = _insert(sink)
    assert params[1] == "not_rideable"
    assert "cannot_find" not in params


def test_an_old_client_with_no_reason_and_no_date_still_works(monkeypatch):
    # Back-compat: the exact body every shipped client sends.
    client, sink = _client(monkeypatch, list(_FRESH))
    r = client.post("/api/v1/reports/device", json={**_BODY, "report_type": "not_rideable"})
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"id", "reported_at", "deduped", "points_awarded"}
    sql = _insert_sql(sink)
    # observed_at defaults to the submission time in SQL.
    assert "COALESCE(%s, NOW())" in sql


# ---------------------------------------------------------------------------
# observed_at
# ---------------------------------------------------------------------------

def _post_observed(monkeypatch, value):
    client, sink = _client(monkeypatch, list(_FRESH))
    r = client.post("/api/v1/reports/device",
                    json={**_BODY, "report_type": "damaged", "observed_at": value})
    return r, sink


def test_observed_at_accepts_a_date(monkeypatch):
    day = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    r, sink = _post_observed(monkeypatch, day)
    assert r.status_code == 200, r.text
    stored = _insert(sink)[2]
    assert stored.isoformat().startswith(day)
    assert stored.tzinfo is not None


def test_observed_at_accepts_a_recent_timestamp(monkeypatch):
    ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    r, _ = _post_observed(monkeypatch, ts)
    assert r.status_code == 200, r.text


def test_observed_at_in_the_future_is_refused(monkeypatch):
    r, _ = _post_observed(monkeypatch,
                          (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    assert r.status_code == 422
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()
    r, _ = _post_observed(monkeypatch, tomorrow)
    assert r.status_code == 422


def test_observed_at_tolerates_clock_skew(monkeypatch):
    r, _ = _post_observed(monkeypatch,
                          (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat())
    assert r.status_code == 200, r.text


def test_observed_at_older_than_30_days_is_refused(monkeypatch):
    r, _ = _post_observed(monkeypatch,
                          (datetime.now(timezone.utc) - timedelta(days=31)).isoformat())
    assert r.status_code == 422


def test_observed_at_that_is_not_a_date_is_refused(monkeypatch):
    r, _ = _post_observed(monkeypatch, "2026-02-31")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# The public CSV never locates an inaccessible report (plan §6)
# ---------------------------------------------------------------------------

class _CsvCursor:
    def __init__(self):
        self.n = 0

    def execute(self, sql, params=()):
        self.n += 1

    def fetchall(self):
        if self.n == 1:
            return [
                (_TS, _VID, "inaccessible", 39.73921, -104.98761, True),
                (_TS, _VID, "not_rideable", 39.73921, -104.98761, True),
            ]
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_public_csv_drops_an_inaccessible_reports_coordinates(monkeypatch):
    cur = _CsvCursor()

    class _C:
        def cursor(self):
            return cur

        def commit(self):
            pass

    @contextmanager
    def _fake():
        yield _C()

    monkeypatch.setattr(api_frontend_reports, "connection", _fake)
    monkeypatch.setattr(api_frontend_reports, "enforce", lambda cur, **kw: None)
    app = FastAPI()
    app.include_router(api_frontend_reports.router)
    r = TestClient(app).get("/api/v1/reports/export/monthly.csv", params={"month": "2026-10"})
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    inacc = next(line for line in lines if ",inaccessible," in line)
    other = next(line for line in lines if ",not_rideable," in line)
    assert "39.739" not in inacc and "-104.988" not in inacc
    assert "39.739" in other  # every other type keeps its ~100 m point


# ---------------------------------------------------------------------------
# Suppression fields on /devices/current
# ---------------------------------------------------------------------------

def test_suppression_fields_for_a_suppressed_vehicle():
    by = {_VID: ("inaccessible", _TS)}
    assert api_public._suppression_fields(by, _VID) == {
        "suppressed": True, "suppressed_reason": "inaccessible",
        "suppressed_since": _TS.isoformat()}
    assert api_public._suppression_fields(by, "0" * 16)["suppressed"] is False


def test_an_unavailable_suppression_query_reads_null_not_false():
    # A client must not read an outage as a clean bill.
    assert api_public._suppression_fields(None, _VID) == {
        "suppressed": None, "suppressed_reason": None, "suppressed_since": None}


def test_the_suppression_query_failing_does_not_break_the_map(monkeypatch):
    @contextmanager
    def _boom():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(api_public, "connection", _boom)
    assert api_public._suppressions("cycle") is None


def test_suppression_is_documented_as_separate_from_reliability():
    # §4.1(4): "or somebody will 'simplify' them together".
    import inspect
    src = inspect.getsource(api_public)
    assert "SUPPRESSION IS NOT RELIABILITY" in src
    assert "FLEET_REPORTS_PLAN.md §2.5" in src
    # And quality.py — the tier — never reads it.
    from src import quality
    assert "suppress" not in inspect.getsource(quality)


# ---------------------------------------------------------------------------
# Identify (the resolve extension): input rules, before any database
# ---------------------------------------------------------------------------

def _resolve_app(monkeypatch):
    @contextmanager
    def _no_db():
        raise AssertionError("must not reach the database")
        yield  # pragma: no cover

    monkeypatch.setattr(api_vehicle_plates, "connection", _no_db)
    app = FastAPI()
    app.include_router(api_vehicle_plates.router)
    return TestClient(app, raise_server_exceptions=True)


@pytest.mark.parametrize("payload", ["   ",
                                     # A URL with no plate in it is longer than
                                     # any plate, so extract_plate's
                                     # whole-payload fallback cannot read it as one.
                                     "https://veo.example/rides/start?number="])
def test_an_unreadable_qr_is_refused_without_a_lookup(monkeypatch, payload):
    r = _resolve_app(monkeypatch).get("/api/v1/vehicles/resolve", params={"qr": payload})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "unreadable"


def test_a_qr_payload_too_long_to_hold_a_plate_is_refused(monkeypatch):
    r = _resolve_app(monkeypatch).get("/api/v1/vehicles/resolve",
                                      params={"qr": "x" * 200})
    assert r.status_code == 400


def test_plate_and_qr_together_is_refused(monkeypatch):
    r = _resolve_app(monkeypatch).get("/api/v1/vehicles/resolve",
                                      params={"plate": "1025543", "qr": "1025543"})
    assert r.status_code == 422


def test_neither_plate_nor_qr_is_still_422(monkeypatch):
    assert _resolve_app(monkeypatch).get("/api/v1/vehicles/resolve").status_code == 422


def test_the_qr_parameter_is_redacted_from_logs():
    line = redact_url("/api/v1/vehicles/resolve?qr=https%3A%2F%2Fx%3Fnumber%3D1025543&explain=true")
    assert "1025543" not in line
    assert "explain=true" in line


# ---------------------------------------------------------------------------
# Admin gating: every new private route
# ---------------------------------------------------------------------------

_ADMIN_ROUTES = [
    ("GET", "/api/v1/private/census/arrivals", None),
    ("GET", "/api/v1/private/census/missing", None),
    ("GET", "/api/v1/private/census/gone", None),
    ("PUT", f"/api/v1/private/census/{_VID}/ack", {}),
    ("DELETE", f"/api/v1/private/census/{_VID}/ack", None),
    ("PUT", f"/api/v1/private/census/{_VID}/note", {"note": "x"}),
    ("POST", "/api/v1/private/reports/1/resolve", {"resolution": "void"}),
    ("GET", "/api/v1/private/reports/export", None),
    ("GET", f"/api/v1/private/devices/{_VID}/reports", None),
]


def _gated_client(monkeypatch, *, session: SessionUser | None, admin: bool):
    def _require_session(request):
        if session is None:
            raise HTTPException(401, "missing bearer token")
        return session

    @contextmanager
    def _no_db():
        raise AssertionError("an unauthorised request reached the database")
        yield  # pragma: no cover

    monkeypatch.setattr(accounts, "require_session", _require_session)
    monkeypatch.setattr(accounts, "is_admin_email", lambda user: admin)
    monkeypatch.setattr(api_fleet_reports, "connection", _no_db)
    app = FastAPI()
    app.include_router(api_fleet_reports.router)
    return TestClient(app)


def _call(client, method, path, body):
    return client.request(method, path, json=body) if body is not None else client.request(method, path)


@pytest.mark.parametrize("method,path,body", _ADMIN_ROUTES)
def test_census_and_export_refuse_a_signed_out_caller(monkeypatch, method, path, body):
    client = _gated_client(monkeypatch, session=None, admin=False)
    assert _call(client, method, path, body).status_code == 401


@pytest.mark.parametrize("method,path,body", _ADMIN_ROUTES)
def test_census_and_export_refuse_a_rider_who_is_not_an_admin(monkeypatch, method, path, body):
    client = _gated_client(monkeypatch, session=_USER, admin=False)
    assert _call(client, method, path, body).status_code == 403


def test_every_new_private_route_is_in_the_gating_list():
    paths = {(sorted(r.methods)[0], r.path) for r in api_fleet_reports.router.routes}
    listed = {(m, p.replace(_VID, "{vehicle_identifier}").replace("/1/", "/{report_id}/"))
              for m, p, _b in _ADMIN_ROUTES}
    assert paths == listed
