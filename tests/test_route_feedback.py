"""POST /api/v1/route-feedback (src/api_route_feedback.py, sql/068).

Fake-cursor idiom, as in tests/test_device_feature_reports.py. Defended:

  * anonymous feedback is stored (metered, weighted by anonymity — but
    stored: the opinion is data);
  * an authenticated submission is attributed to the account;
  * a row with no substantive answer is a 422 — a bare profile name says
    nothing;
  * the deviation follow-up cannot outlive a non-Yes deviation answer;
  * out-of-range ratings are 422s;
  * the response carries no points — there is nothing to award.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_route_feedback
from src.accounts import SessionUser, optional_session

_TS = datetime(2026, 8, 10, tzinfo=timezone.utc)
_USER = SessionUser(
    account_id=42, email="rider@example.com", scopes=("rider",),
    expires_at=datetime.now(timezone.utc),
    sliding=True, method="google", token_sha256="x",
)

_BODY = {
    "route_profile": "shade",
    "distance_m": 3200.5,
    "duration_s": 840.0,
    "nav_route_rating": 8,
    "nav_deviated": True,
    "nav_deviated_needs_improvement": True,
    "nav_nps": 9,
    "nav_qualitative": "The shade route was great until the staircase.",
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


def _client(monkeypatch, fetch=((7, _TS),), *, authenticated=True):
    conn = _FakeConn(fetch)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_route_feedback, "connection", _fake_connection)
    monkeypatch.setattr(api_route_feedback, "enforce", lambda cur, **kw: None)
    app = FastAPI()
    app.include_router(api_route_feedback.router)
    if authenticated:
        app.dependency_overrides[optional_session] = lambda: _USER
    else:
        app.dependency_overrides[optional_session] = lambda: None
    return TestClient(app), conn


def test_stores_the_answers_and_returns_no_points(monkeypatch):
    client, conn = _client(monkeypatch)
    r = client.post("/api/v1/route-feedback", json=_BODY)
    assert r.status_code == 200
    body = r.json()
    assert body == {"id": 7, "created_at": _TS.isoformat()}
    assert "points" not in body
    idx = next(
        i for i, s in enumerate(conn.cur.statements)
        if "INSERT INTO route_feedback" in s
    )
    params = conn.cur.params[idx]
    assert params[0] == 42          # attributed to the account
    assert "shade" in params


def test_anonymous_is_stored_unattributed(monkeypatch):
    client, conn = _client(monkeypatch, authenticated=False)
    r = client.post("/api/v1/route-feedback", json=_BODY)
    assert r.status_code == 200
    idx = next(
        i for i, s in enumerate(conn.cur.statements)
        if "INSERT INTO route_feedback" in s
    )
    assert conn.cur.params[idx][0] is None


def test_a_bare_profile_name_is_a_422(monkeypatch):
    client, _ = _client(monkeypatch)
    r = client.post(
        "/api/v1/route-feedback",
        json={"route_profile": "shade"},
    )
    assert r.status_code == 422


def test_whitespace_qualitative_does_not_count_as_an_answer(monkeypatch):
    client, _ = _client(monkeypatch)
    r = client.post(
        "/api/v1/route-feedback",
        json={"route_profile": "shade", "nav_qualitative": "   "},
    )
    assert r.status_code == 422


def test_follow_up_cannot_outlive_a_non_yes_deviation(monkeypatch):
    """Mirrors the survey pane: the improvement question is only asked
    after a Yes, so a No must not smuggle a verdict through."""
    client, conn = _client(monkeypatch)
    r = client.post(
        "/api/v1/route-feedback",
        json={
            "route_profile": "shade",
            "nav_deviated": False,
            "nav_deviated_needs_improvement": True,
        },
    )
    assert r.status_code == 200
    idx = next(
        i for i, s in enumerate(conn.cur.statements)
        if "INSERT INTO route_feedback" in s
    )
    # nav_deviated_needs_improvement is the third-from-last parameter.
    assert conn.cur.params[idx][-3] is None


def test_out_of_range_answers_are_422s(monkeypatch):
    client, _ = _client(monkeypatch)
    for bad in (
        {"nav_route_rating": 0},
        {"nav_route_rating": 11},
        {"nav_nps": -1},
        {"nav_nps": 11},
        {"distance_m": -1},
    ):
        r = client.post(
            "/api/v1/route-feedback", json={"route_profile": "safe", **bad},
        )
        assert r.status_code == 422, bad
    # Non-finite floats arrive as the JSON *text* 1e400 (the client-side
    # serializer would refuse an inf, which is exactly why the raw string
    # is the honest simulation): the server parses it to inf and must 422
    # rather than store 'Infinity' in Postgres.
    for field in ("distance_m", "duration_s"):
        r = client.post(
            "/api/v1/route-feedback",
            content=f'{{"route_profile": "safe", "{field}": 1e400}}',
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 422, field


def test_blank_profile_is_a_422_and_a_padded_one_is_canonicalized(monkeypatch):
    """min_length alone would wave '   ' through — a row no analysis could
    tie to a real profile key."""
    client, conn = _client(monkeypatch)
    r = client.post(
        "/api/v1/route-feedback",
        json={"route_profile": "   ", "nav_nps": 5},
    )
    assert r.status_code == 422
    r = client.post(
        "/api/v1/route-feedback",
        json={"route_profile": "  shade  ", "nav_nps": 5},
    )
    assert r.status_code == 200
    idx = next(
        i for i, s in enumerate(conn.cur.statements)
        if "INSERT INTO route_feedback" in s
    )
    assert "shade" in conn.cur.params[idx]
    assert "  shade  " not in conn.cur.params[idx]
