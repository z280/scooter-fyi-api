"""Plate lookups through our API (src/api_vehicle_plates.py), against a fake DB.

  FORWARD  GET /api/v1/vehicles/plates?device_ids=…   signed-in rider only
  REVERSE  GET /api/v1/vehicles/resolve?plate=…        public, per-IP limited

The rules a fake can pin: the session gate, the 50-id cap and parsing, that
unknown ids are omitted, the plate normalisation (which must match the
frontend's normalizePlate byte for byte), the 404, which rate-limit buckets
are charged, and — the privacy line — that the reverse response never carries
the plate. The SQL itself runs against real Postgres in
tests/test_vehicle_plates_pg.py.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src import api_vehicle_plates as mod
from src.accounts import SessionUser, require_session

_CYCLE = uuid.UUID("8f3a2d10-1234-4abc-8def-0123456789ab")
_SNAP = datetime(2026, 10, 8, 16, 40, tzinfo=timezone.utc)

# The fake fleet: device_id -> (vehicle_identifier, raw plate as stored).
_FLEET = {
    "bike-a": ("8c4a1f0d2e9b7a35", "1025543"),
    "bike-b": ("0123456789abcdef", "1031187"),
    "bike-c": ("fedcba9876543210", None),          # feed row with no plate
}


def _norm_sql_side(plate: str) -> str:
    # What _SQL_NORMALIZED_PLATE computes in Postgres.
    import re
    return re.sub(r"[\s-]+", "", plate).upper()


class _Cursor:
    """Answers the three statements the module issues, from _FLEET."""

    def __init__(self, fleet, cycle_row):
        self.fleet = fleet
        self.cycle_row = cycle_row
        self._result: list = []
        self.queries: list[str] = []

    def execute(self, sql, params=None):
        self.queries.append(sql)
        if "FROM observation_cycles" in sql:
            self._result = [self.cycle_row] if self.cycle_row else []
        elif "device_id = ANY" in sql:
            _cycle, ids = params
            self._result = [(d, self.fleet[d][1]) for d in sorted(ids)
                            if d in self.fleet and self.fleet[d][1]]
        elif "regexp_replace" in sql:
            _cycle, want = params
            self._result = [(d, vid) for d, (vid, p) in self.fleet.items()
                            if p and _norm_sql_side(p) == want][:2]
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Limiter:
    """Stands in for ratelimit.enforce: counts per (bucket, key) and 429s at
    the configured limit, exactly like the real one's contract."""

    def __init__(self):
        self.calls: list[tuple[str, str, int, int]] = []
        self.counts: dict[tuple[str, str], int] = {}

    def __call__(self, cur, *, bucket, key, limit, window_seconds):
        self.calls.append((bucket, key, limit, window_seconds))
        n = self.counts.get((bucket, key), 0)
        if n >= limit:
            raise HTTPException(429, detail="rate limit exceeded — try again later",
                                headers={"Retry-After": "1"})
        self.counts[(bucket, key)] = n + 1


@pytest.fixture
def env(monkeypatch):
    state = {"fleet": dict(_FLEET), "cycle_row": (_CYCLE, _SNAP)}
    limiter = _Limiter()
    cursors: list[_Cursor] = []

    @contextmanager
    def _connection():
        cur = _Cursor(state["fleet"], state["cycle_row"])
        cursors.append(cur)
        yield _Conn(cur)

    monkeypatch.setattr(mod, "connection", _connection)
    monkeypatch.setattr(mod, "enforce", limiter)
    state["limiter"] = limiter
    state["cursors"] = cursors
    return state


def _app(user: SessionUser | None) -> TestClient:
    app = FastAPI()
    app.include_router(mod.router)
    if user is not None:
        app.dependency_overrides[require_session] = lambda: user
    return TestClient(app)


def _rider(account_id: int = 7) -> SessionUser:
    return SessionUser(
        account_id=account_id, email="rider@example.com", scopes=("rider",),
        expires_at=_SNAP, sliding=True, method="magic_link", token_sha256="x" * 64,
    )


# ---------------------------------------------------------------------------
# normalize_plate — must equal the frontend's normalizePlate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw, want", [
    # The frontend's own test vectors (ride-deeplink.test.ts).
    (" 10-25 543 ", "1025543"),
    ("ab12", "AB12"),
    ("", ""),
    # Runs of mixed separators, tabs, leading/trailing.
    ("10 - 25\t543", "1025543"),
    ("--1025543--", "1025543"),
    (None, ""),
])
def test_normalize_plate_matches_frontend(raw, want):
    assert mod.normalize_plate(raw) == want


# ---------------------------------------------------------------------------
# FORWARD: device -> plate
# ---------------------------------------------------------------------------
def test_forward_requires_a_session(env):
    """No override, no Authorization header: the real require_session 401s
    before any DB work."""
    r = _app(None).get("/api/v1/vehicles/plates", params={"device_ids": "bike-a"})
    assert r.status_code == 401
    assert env["cursors"] == []
    assert env["limiter"].calls == []


def test_forward_rejects_a_malformed_bearer(env):
    r = _app(None).get("/api/v1/vehicles/plates", params={"device_ids": "bike-a"},
                       headers={"Authorization": "Basic abc"})
    assert r.status_code == 401


def test_forward_returns_plates_for_known_ids_and_omits_the_rest(env):
    r = _app(_rider()).get(
        "/api/v1/vehicles/plates",
        params={"device_ids": "bike-a, bike-b,nope,bike-c,bike-a"},
    )
    assert r.status_code == 200
    body = r.json()
    # bike-c is in the feed but plateless; `nope` is not in the snapshot.
    # Both are OMITTED, not null.
    assert body == {
        "plates": {"bike-a": "1025543", "bike-b": "1031187"},
        "as_of": _SNAP.isoformat(),
    }
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["vary"] == "Authorization"


def test_forward_reads_the_current_complete_cycle(env):
    _app(_rider()).get("/api/v1/vehicles/plates", params={"device_ids": "bike-a"})
    cur = env["cursors"][0]
    assert any("job_status = 'complete'" in q for q in cur.queries)


def test_forward_all_unknown_is_an_empty_map(env):
    r = _app(_rider()).get("/api/v1/vehicles/plates", params={"device_ids": "x,y"})
    assert r.status_code == 200
    assert r.json()["plates"] == {}


def test_forward_accepts_exactly_50_ids(env):
    ids = ",".join(f"d{i}" for i in range(50))
    r = _app(_rider()).get("/api/v1/vehicles/plates", params={"device_ids": ids})
    assert r.status_code == 200


def test_forward_rejects_51_ids_before_touching_the_db(env):
    ids = ",".join(f"d{i}" for i in range(51))
    r = _app(_rider()).get("/api/v1/vehicles/plates", params={"device_ids": ids})
    assert r.status_code == 400
    assert "50" in r.json()["detail"]
    assert env["cursors"] == []


def test_forward_duplicates_do_not_count_toward_the_cap(env):
    ids = ",".join(["bike-a"] * 80)
    r = _app(_rider()).get("/api/v1/vehicles/plates", params={"device_ids": ids})
    assert r.status_code == 200
    assert r.json()["plates"] == {"bike-a": "1025543"}


@pytest.mark.parametrize("raw", ["", " , ,", ",,,"])
def test_forward_rejects_an_empty_list(env, raw):
    r = _app(_rider()).get("/api/v1/vehicles/plates", params={"device_ids": raw})
    assert r.status_code == 400


def test_forward_missing_param_is_422(env):
    r = _app(_rider()).get("/api/v1/vehicles/plates")
    assert r.status_code == 422


def test_forward_rejects_an_overlong_id(env):
    r = _app(_rider()).get("/api/v1/vehicles/plates", params={"device_ids": "x" * 65})
    assert r.status_code == 400


def test_forward_charges_account_and_ip_buckets(env):
    _app(_rider(account_id=42)).get(
        "/api/v1/vehicles/plates", params={"device_ids": "bike-a"},
        headers={"CF-Connecting-IP": "203.0.113.9"},
    )
    assert env["limiter"].calls == [
        ("vehicle_plates_account", "42", 60, 60),
        ("vehicle_plates_ip", "203.0.113.9", 120, 60),
    ]


def test_forward_429_after_60_per_account(env):
    c = _app(_rider())
    for _ in range(60):
        assert c.get("/api/v1/vehicles/plates",
                     params={"device_ids": "bike-a"}).status_code == 200
    r = c.get("/api/v1/vehicles/plates", params={"device_ids": "bike-a"})
    assert r.status_code == 429
    assert "retry-after" in r.headers


def test_forward_per_ip_cap_spans_accounts(env):
    """One address cycling through accounts still hits the IP cap."""
    hdr = {"CF-Connecting-IP": "198.51.100.1"}
    for i in range(120):
        r = _app(_rider(account_id=1000 + i)).get(
            "/api/v1/vehicles/plates", params={"device_ids": "bike-a"}, headers=hdr)
        assert r.status_code == 200
    r = _app(_rider(account_id=9999)).get(
        "/api/v1/vehicles/plates", params={"device_ids": "bike-a"}, headers=hdr)
    assert r.status_code == 429


def test_forward_503_before_any_complete_cycle(env):
    env["cycle_row"] = None
    r = _app(_rider()).get("/api/v1/vehicles/plates", params={"device_ids": "bike-a"})
    assert r.status_code == 503


def test_forward_never_logs_plates(env, caplog):
    caplog.set_level(logging.DEBUG)
    c = _app(_rider())
    c.get("/api/v1/vehicles/plates", params={"device_ids": "bike-a,bike-b"})
    for _ in range(60):
        c.get("/api/v1/vehicles/plates", params={"device_ids": "bike-a"})
    for plate in ("1025543", "1031187"):
        assert plate not in _server_log(caplog)


# ---------------------------------------------------------------------------
# REVERSE: plate -> vehicle
# ---------------------------------------------------------------------------
def test_reverse_is_public_and_returns_only_public_identifiers(env):
    r = _app(None).get("/api/v1/vehicles/resolve", params={"plate": "1025543"})
    assert r.status_code == 200
    body = r.json()
    assert body == {"device_id": "bike-a", "vehicle_identifier": "8c4a1f0d2e9b7a35"}
    # The privacy line: no plate, under any key, anywhere in the body.
    assert "1025543" not in r.text
    assert set(body) == {"device_id", "vehicle_identifier"}
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("typed", [
    "1025543", " 10-25 543 ", "10 25 543", "1025-543", "\t1025543\n",
])
def test_reverse_normalises_like_the_frontend(env, typed):
    r = _app(None).get("/api/v1/vehicles/resolve", params={"plate": typed})
    assert r.status_code == 200
    assert r.json()["device_id"] == "bike-a"


def test_reverse_is_case_insensitive(env):
    env["fleet"]["bike-x"] = ("aaaaaaaaaaaaaaaa", "ab-12")
    r = _app(None).get("/api/v1/vehicles/resolve", params={"plate": "AB12"})
    assert r.status_code == 200
    assert r.json()["device_id"] == "bike-x"


def test_reverse_unknown_plate_is_404_without_echo(env):
    r = _app(None).get("/api/v1/vehicles/resolve", params={"plate": "9999999"})
    assert r.status_code == 404
    assert "9999999" not in r.text
    assert r.headers["cache-control"] == "no-store"


def test_reverse_ambiguous_plate_is_404(env):
    """Two vehicles normalising to the same plate: missing beats wrong."""
    env["fleet"]["bike-dup"] = ("1111111111111111", "10-25543")
    r = _app(None).get("/api/v1/vehicles/resolve", params={"plate": "1025543"})
    assert r.status_code == 404


@pytest.mark.parametrize("plate", ["", "   ", "- -"])
def test_reverse_empty_after_normalisation_is_400(env, plate):
    r = _app(None).get("/api/v1/vehicles/resolve", params={"plate": plate})
    assert r.status_code == 400
    assert env["cursors"] == []


def test_reverse_overlong_plate_is_400(env):
    r = _app(None).get("/api/v1/vehicles/resolve", params={"plate": "1" * 33})
    assert r.status_code == 400


def test_reverse_missing_param_is_422(env):
    assert _app(None).get("/api/v1/vehicles/resolve").status_code == 422


def test_reverse_charges_the_ip_bucket(env):
    _app(None).get("/api/v1/vehicles/resolve", params={"plate": "1025543"},
                   headers={"CF-Connecting-IP": "203.0.113.9"})
    assert env["limiter"].calls == [("vehicle_resolve_ip", "203.0.113.9", 30, 60)]


def test_reverse_429_after_30_per_ip_and_misses_count(env):
    c = _app(None)
    hdr = {"CF-Connecting-IP": "192.0.2.4"}
    for i in range(30):
        # Mostly misses — enumeration traffic — and every one is charged.
        r = c.get("/api/v1/vehicles/resolve", params={"plate": f"{9000000 + i}"},
                  headers=hdr)
        assert r.status_code == 404
    r = c.get("/api/v1/vehicles/resolve", params={"plate": "1025543"}, headers=hdr)
    assert r.status_code == 429
    # A different address is unaffected.
    r = c.get("/api/v1/vehicles/resolve", params={"plate": "1025543"},
              headers={"CF-Connecting-IP": "192.0.2.5"})
    assert r.status_code == 200


def test_reverse_503_before_any_complete_cycle(env):
    env["cycle_row"] = None
    r = _app(None).get("/api/v1/vehicles/resolve", params={"plate": "1025543"})
    assert r.status_code == 503


def _server_log(caplog) -> str:
    # httpx is the TEST CLIENT logging its own request URL; it is not part of
    # the server. Everything else captured is.
    return "\n".join(r.getMessage() for r in caplog.records
                     if not r.name.startswith("httpx"))


def test_reverse_never_logs_plates(env, caplog):
    caplog.set_level(logging.DEBUG)
    c = _app(None)
    c.get("/api/v1/vehicles/resolve", params={"plate": "1025543"})
    c.get("/api/v1/vehicles/resolve", params={"plate": "7654321"})
    assert "1025543" not in _server_log(caplog)
    assert "7654321" not in _server_log(caplog)


def test_access_log_filter_redacts_the_plate():
    """uvicorn.access records the full request line. The filter src/main.py
    attaches must strip the plate from it — and only from this route."""
    import src.main  # noqa: F401 — attaches the filter

    access = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, mod.RedactPlateQuery) for f in access.filters)

    def _line(path: str) -> str:
        rec = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1,
            '%s - "%s %s HTTP/%s" %d',
            ("203.0.113.9:5555", "GET", path, "1.1", 200), None,
        )
        for f in access.filters:
            f.filter(rec)
        return rec.getMessage()

    line = _line("/api/v1/vehicles/resolve?plate=10-25%20543&x=1")
    assert "10-25" not in line and "543" not in line
    assert "plate=[redacted]&x=1" in line
    assert "1025543" not in _line("/api/v1/vehicles/resolve?plate=1025543")
    # Other routes pass through untouched.
    other = "/api/v1/devices/current?bbox=1,2,3,4"
    assert other in _line(other)


def test_routes_are_mounted_by_the_app():
    from src.main import app
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/v1/vehicles/plates" in paths
    assert "/api/v1/vehicles/resolve" in paths
