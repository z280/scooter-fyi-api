"""Keep a Scooter and the QR-scan award were retired on 2026-10-06.

The frontend stopped offering them, but the API kept serving them:
- POST /api/v1/devices/qr-scan paid 100 points per vehicle, for a "scan"
  that only proved the caller knew the plate. Plates are public in Veo's own
  feed, so it could be scripted across the fleet.
- The favorites routes kept a feature the owner killed for its risks.

These guard the production app (src.main), not a test app.
"""

from __future__ import annotations

from src import api_points
from src.main import app


def _routes() -> set[tuple[str, str]]:
    """(METHOD, path) for everything the production app serves. Read from the
    OpenAPI schema: this FastAPI wraps included routers lazily, so walking
    app.routes does not see them."""
    out: set[tuple[str, str]] = set()
    for path, ops in app.openapi()["paths"].items():
        for method in ops:
            out.add((method.upper(), path))
    return out


def test_qr_scan_is_not_mounted():
    assert not any(path == "/api/v1/devices/qr-scan" for _, path in _routes())


def test_keep_a_scooter_create_read_edit_are_not_mounted():
    routes = _routes()
    assert ("GET", "/api/v1/profile/favorite-devices") not in routes
    assert ("POST", "/api/v1/profile/favorite-devices") not in routes
    assert ("PATCH", "/api/v1/profile/favorite-devices/{vehicle_identifier}") not in routes


def test_riders_can_still_delete_what_they_kept():
    # The privacy policy promises an immediate hard delete; that outlives
    # the feature.
    assert ("DELETE", "/api/v1/profile/favorite-devices/{vehicle_identifier}") in _routes()


def test_the_published_schedule_no_longer_offers_qr_scan():
    assert "qr_scan" not in api_points.points_schedule()


def test_riders_can_list_what_they_kept_to_delete_it():
    assert ("GET", "/api/v1/profile/favorite-devices/retired") in _routes()


def test_the_retired_listing_returns_no_position_or_live_state(monkeypatch):
    """Identifiers, nicknames and dates only."""
    from contextlib import contextmanager
    from datetime import datetime, timezone

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src import api_favorites
    from src.accounts import SessionUser, require_session

    class _Cur:
        def execute(self, *a, **k): pass
        def fetchall(self):
            return [("a1ef50e3bea698ba", "Rover", datetime(2026, 9, 29, tzinfo=timezone.utc))]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    @contextmanager
    def _conn():
        yield _Conn()

    monkeypatch.setattr(api_favorites, "connection", _conn)
    app_ = FastAPI()
    app_.include_router(api_favorites.router)
    app_.dependency_overrides[require_session] = lambda: SessionUser(
        account_id=4, email="r@example.com", scopes=("rider",),
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc), sliding=True,
        method="google", token_sha256="x")
    body = TestClient(app_).get("/api/v1/profile/favorite-devices/retired").json()
    (row,) = body["favorite_devices"]
    assert set(row) == {"vehicle_identifier", "nickname", "kept_at"}
