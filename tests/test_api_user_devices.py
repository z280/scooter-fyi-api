"""GET /api/v1/user/devices/current — session-gated map feed.

Same shape as the public endpoint for any rider; adds the admin-only
private fields (raw vehicle_plate + first-ever sighting + observed max
range) when the session email is in ADMIN_EMAILS — via EITHER sign-in
door. Replaces the retired /api/v1/private/devices/current.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import Response
from starlette.requests import Request

from src import accounts, api_public, api_user
from src.accounts import SessionUser

_CYCLE_ID = uuid.UUID("8f3a2d10-1234-4abc-8def-0123456789ab")
_SNAP = datetime(2026, 7, 6, 14, 30, tzinfo=timezone.utc)

# 30-column row in the exact order _devices_current_impl's SELECT produces
# (r[0]..r[29]); the last four are the admin-only private fields.
_ROW = (
    "dev1", "scooter", 39.7392, -104.9876, "denver_core",
    "8c4a1f0d2e9b7a35", False, False, 45293, "electric",
    111, 222, 333,
    "75", "40/52", "3100/4100", "3100/6000", "12/40", "3/8", "1/1",
    False,          # 20 has_negative_report
    52800,          # 21 max_range_meters_for_type
    0,              # 22 number_failed_starts
    None,           # 23 first_observed_at_location
    "standing", "Astro",   # 24-25
    "1025543",      # 26 vehicle_plate
    None,           # 27 first_ever_observed_at
    50000,          # 28 max_observed_range_meters
    None,           # 29 max_observed_range_at
)

_PLATE_FIELDS = (
    "vehicle_plate", "first_ever_observed_at",
    "max_observed_range_meters", "max_observed_range_at",
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
def _fake_db(monkeypatch):
    @contextmanager
    def _conn():
        yield _FakeConn()

    # The impl lives in api_public, so patch there.
    monkeypatch.setattr(api_public, "connection", _conn)
    monkeypatch.setattr(api_public, "stats_for_cycle", lambda cycle_id, snapshot_time: {})


_ADMINS = frozenset({"z@neill.io"})


def _user(email: str, method: str = "magic_link") -> SessionUser:
    # Note: scopes is always just ("rider",) — the gate is email membership,
    # NOT the admin scope, so a magic-link session (no admin scope) with an
    # allowlisted email must still unlock plates.
    return SessionUser(
        account_id=1, email=email, scopes=("rider",),
        supporter=False, expires_at=_SNAP, sliding=True,
        method=method, token_sha256="x" * 64,
    )


def _request(headers: dict[str, str] | None = None) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/api/v1/user/devices/current",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "query_string": b"",
    })


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch):
    # _wants_plate -> accounts.is_admin_email -> accounts.admin_emails()
    monkeypatch.setattr(accounts, "admin_emails", lambda: _ADMINS)


def _call(*, email="rider@example.com", method="magic_link", headers=None, response=None):
    return api_user.user_devices_current(
        _request(headers), response or Response(), user=_user(email, method),
        form_factor=None, spatial_status=None, include_outliers=False,
        bbox=None, include=None,
    )


# ---------- non-allowlisted rider ---------------------------------------------
def test_non_admin_gets_map_without_plate(_fake_db):
    out = _call(email="rider@example.com")
    assert out["metadata"]["device_count"] == 1
    props = out["features"][0]["properties"]
    for f in _PLATE_FIELDS:
        assert f not in props, f"{f} must be admin-only"
    # Still gets the safe signed-in fields.
    assert props["reliability_tier"] in ("ok", "unknown", "high_risk")
    assert isinstance(props["battery_percent"], int)
    assert out["metadata"]["admin"] is False
    assert out["metadata"]["viewed_by"] == "rider@example.com"


# ---------- allowlisted email, EITHER door ------------------------------------
def test_magic_link_admin_email_gets_plate_fields(_fake_db):
    """The whole point of the either-door gate: a magic-link session (no
    admin scope) whose email is allowlisted still sees plates."""
    out = _call(email="z@neill.io", method="magic_link")
    props = out["features"][0]["properties"]
    assert props["vehicle_plate"] == "1025543"
    assert props["max_observed_range_meters"] == 50000
    assert props["first_ever_observed_at"] is None
    assert props["max_observed_range_at"] is None
    assert out["metadata"]["admin"] is True


def test_google_admin_email_gets_plate_fields(_fake_db):
    out = _call(email="z@neill.io", method="google")
    assert out["features"][0]["properties"]["vehicle_plate"] == "1025543"
    assert out["metadata"]["admin"] is True


def test_email_match_is_case_insensitive(_fake_db):
    out = _call(email="Z@Neill.IO", method="magic_link")
    assert out["metadata"]["admin"] is True


def test_admin_and_non_admin_get_distinct_etags(_fake_db):
    r_admin, r_plain = Response(), Response()
    _call(email="z@neill.io", response=r_admin)
    _call(email="rider@example.com", response=r_plain)
    assert r_admin.headers["etag"] != r_plain.headers["etag"]
    # Per-user response must not be shared-cached, must revalidate, and must
    # vary by bearer so one token's (plate-bearing) body can't be reused for
    # another.
    assert r_admin.headers["cache-control"] == "private, no-cache"
    assert r_admin.headers["vary"] == "Authorization"


def test_etag_is_per_user(_fake_db):
    """Two users at the SAME admin level still get different ETags (identity
    is folded into the key), so no cross-user 304 reuse even if a cache
    mishandles Vary. Both non-admin here, so only the email differs."""
    r1, r2 = Response(), Response()
    _call(email="a@example.com", response=r1)
    _call(email="b@example.com", response=r2)
    assert r1.headers["etag"] != r2.headers["etag"]


def test_user_endpoint_304_on_revalidation(_fake_db):
    resp = Response()
    _call(email="rider@example.com", response=resp)
    etag = resp.headers["etag"]
    assert "user-devices" in etag
    out = _call(email="rider@example.com", headers={"If-None-Match": etag})
    assert isinstance(out, Response)
    assert out.status_code == 304
