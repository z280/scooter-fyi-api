"""The QR half of POST /api/v1/reports/device-features (sql/067).

Same fake-cursor idiom as tests/test_device_feature_reports.py. The
behaviours defended here are the ones the module docstring promises:

  * a scan that resolves to the claimed vehicle validates the report
    outright — plate_valid true, points paid, no typed plate needed;
  * a scan that resolves to a DIFFERENT vehicle re-targets the report to
    the scanned one (the answers describe the scooter the rider was
    standing at), keeping the claim in claimed_vehicle_identifier;
  * a scan that resolves to nothing falls back to the claimed vehicle and
    the typed-plate rule — and with no claimed vehicle at all (the
    tools-drawer flow) it is a 404;
  * the QR payload is logged verbatim on the report row, and a resolved
    scan refreshes the device_qr_codes registry;
  * a report with neither identity, or neither proof, is a 422.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_device_features
from src.accounts import SessionUser, optional_session
from src.points import POINTS_DEVICE_FEATURES_FIRST

_VID = "8c4a1f0d2e9b7a35"
_OTHER_VID = "1b2c3d4e5f607182"
_PLATE = "1025543"
_OTHER_PLATE = "7777777"
_QR = f"https://ride.veoride.com/deeplink?number={_PLATE}&foo=1"
_OTHER_QR = f"https://ride.veoride.com/deeplink?number={_OTHER_PLATE}&foo=1"
_TS = datetime(2026, 8, 8, tzinfo=timezone.utc)
_USER = SessionUser(
    account_id=42, email="rider@example.com", scopes=("rider",),
    expires_at=datetime.now(timezone.utc),
    sliding=True, method="google", token_sha256="x",
)

_BODY = {
    "vehicle_identifier": _VID,
    "device_id": "bike-77",
    "has_bell": True,
    "has_cup_holder": False,
    "has_phone_holder": True,
    "has_basket": False,
    "all_good_condition": True,
    "poor_condition": [],
    "lat": 39.7392,
    "lng": -104.9876,
}


class _FakeCursor:
    def __init__(self, fetch):
        self._fetch = list(fetch)
        self.statements: list[str] = []
        self.params: list[tuple] = []

    def execute(self, sql, params=None, *a, **k):
        self.statements.append(" ".join(str(sql).split()))
        self.params.append(params)

    def fetchone(self):
        return self._fetch.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, fetch):
        self.cur = _FakeCursor(fetch)

    def cursor(self):
        return self.cur

    def commit(self):
        pass


def _client(monkeypatch, fetch, *, authenticated=True):
    conn = _FakeConn(fetch)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_device_features, "connection", _fake_connection)
    monkeypatch.setattr(api_device_features, "enforce", lambda cur, **kw: None)
    # The real hash_plate needs the deployment salt; the mapping is all the
    # endpoint's logic actually consumes.
    monkeypatch.setattr(
        api_device_features, "hash_plate",
        lambda plate: {_PLATE: _VID, _OTHER_PLATE: _OTHER_VID}.get(
            plate, "0000000000000000",
        ) if plate else None,
    )
    app = FastAPI()
    app.include_router(api_device_features.router)
    if authenticated:
        app.dependency_overrides[optional_session] = lambda: _USER
    else:
        app.dependency_overrides[optional_session] = lambda: None
    return TestClient(app), conn


_STATE = (_PLATE, "needs_features_confirmed", None, 39.7392, -104.9876)
_OTHER_STATE = (_OTHER_PLATE, "needs_features_confirmed", None, 39.74, -104.99)


def _qr_resolved_fetch(state=_STATE, awarded=True):
    """The handler's reads when the scan resolves, in order:
      1. device_state lookup by the QR's identifier -> state
      2. dedupe probe                               -> None
      3. report INSERT RETURNING                    -> (id, reported_at)
      (the device_qr_codes upsert fetches nothing)
    then, only when points are credited:
      4. cooldown probe -> None; 5. points INSERT RETURNING -> (id, ts)
    """
    rows = [state, None, (7, _TS)]
    if awarded:
        rows += [None, (99, _TS)]
    return rows


# --- the scan as proof -------------------------------------------------------

def test_matching_qr_validates_and_pays_without_a_typed_plate(monkeypatch):
    client, conn = _client(monkeypatch, _qr_resolved_fetch())
    r = client.post(
        "/api/v1/reports/device-features",
        json={**_BODY, "qr_raw_value": _QR},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["plate_valid"] is True
    assert body["points_awarded"] == POINTS_DEVICE_FEATURES_FIRST
    assert body["qr_matched"] is True
    assert body["vehicle_identifier"] == _VID
    # The QR payload is logged verbatim, and the plate the sticker encoded
    # stands in for the never-typed submitted_plate.
    idx = next(
        i for i, s in enumerate(conn.cur.statements)
        if "INSERT INTO device_feature_reports" in s
    )
    assert _QR in conn.cur.params[idx]
    assert _PLATE in conn.cur.params[idx]
    # A resolved scan also refreshes the sql/032 registry.
    assert any("INSERT INTO device_qr_codes" in s for s in conn.cur.statements)


def test_mismatched_qr_retargets_to_the_scanned_vehicle(monkeypatch):
    """The rider tapped one scooter and scanned its neighbour: the answers
    describe the neighbour, so that is the vehicle the report attaches to,
    with the tapped one kept for audit."""
    client, conn = _client(monkeypatch, _qr_resolved_fetch(state=_OTHER_STATE))
    r = client.post(
        "/api/v1/reports/device-features",
        json={**_BODY, "qr_raw_value": _OTHER_QR},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["qr_matched"] is True
    assert body["plate_valid"] is True
    assert body["vehicle_identifier"] == _OTHER_VID
    idx = next(
        i for i, s in enumerate(conn.cur.statements)
        if "INSERT INTO device_feature_reports" in s
    )
    params = conn.cur.params[idx]
    assert params[0] == _OTHER_VID          # attached to the scanned vehicle
    assert _VID in params                   # claimed_vehicle_identifier kept


def test_unresolvable_qr_falls_back_to_the_claimed_vehicle(monkeypatch):
    """A damaged sticker (or a payload shape we don't recognize) must not
    cost the rider the report: the claimed vehicle and the typed-plate rule
    still apply, and the raw scan is still logged for debugging."""
    # Reads: 1. state lookup by QR vid -> None, 2. dedupe -> None,
    # 3. state lookup by claim -> state, 4. INSERT, then points rows.
    client, conn = _client(
        monkeypatch, [None, None, _STATE, (7, _TS), None, (99, _TS)],
    )
    r = client.post(
        "/api/v1/reports/device-features",
        json={**_BODY, "submitted_plate": _PLATE,
              "qr_raw_value": "GARBLED-STICKER"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["qr_matched"] is False
    assert body["plate_valid"] is True      # the typed plate carried it
    assert body["vehicle_identifier"] == _VID
    idx = next(
        i for i, s in enumerate(conn.cur.statements)
        if "INSERT INTO device_feature_reports" in s
    )
    assert "GARBLED-STICKER" in conn.cur.params[idx]
    assert not any("INSERT INTO device_qr_codes" in s for s in conn.cur.statements)


def test_unresolvable_qr_with_no_claim_is_a_404(monkeypatch):
    """The tools-drawer flow has no claimed vehicle: an unresolvable scan
    leaves nothing for the report to attach to."""
    client, _ = _client(monkeypatch, [None])
    body = {k: v for k, v in _BODY.items() if k != "vehicle_identifier"}
    r = client.post(
        "/api/v1/reports/device-features",
        json={**body, "qr_raw_value": "GARBLED-STICKER"},
    )
    assert r.status_code == 404


def test_qr_only_report_needs_no_vehicle_identifier(monkeypatch):
    """The tools-drawer flow: the scan IS the identity."""
    client, _ = _client(monkeypatch, _qr_resolved_fetch())
    body = {k: v for k, v in _BODY.items() if k != "vehicle_identifier"}
    r = client.post(
        "/api/v1/reports/device-features",
        json={**body, "qr_raw_value": _QR},
    )
    assert r.status_code == 200
    assert r.json()["vehicle_identifier"] == _VID


# --- the validator -----------------------------------------------------------

def test_neither_identity_is_a_422(monkeypatch):
    client, _ = _client(monkeypatch, [])
    body = {k: v for k, v in _BODY.items() if k != "vehicle_identifier"}
    assert client.post(
        "/api/v1/reports/device-features",
        json={**body, "submitted_plate": _PLATE},
    ).status_code == 422


def test_neither_proof_is_a_422(monkeypatch):
    """A vehicle_identifier alone proves nothing about presence — the
    typed plate or the scan must ride along, exactly as before sql/067."""
    client, _ = _client(monkeypatch, [])
    assert client.post(
        "/api/v1/reports/device-features", json=_BODY,
    ).status_code == 422
