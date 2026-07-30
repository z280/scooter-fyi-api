"""Contract tests for GET /api/v1/leaderboard/regional and its admin
sibling GET /api/v1/private/regional-leaders (src/api_leaderboard.py,
src/api_private.py) — the whole-database companion to
GET /api/v1/leaderboard/map added per @zNeill's clarification on
scooter-fyi-api#37 (sql/054 regional_leaders).

Same fake-cursor idiom as tests/test_api_leaderboard_map.py, scoped down
to this endpoint's own two SQL statements (the run metadata read and the
flat `regional_leaders` read) plus the shared `accounts` live-join.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from starlette.requests import Request

from src import api_leaderboard, api_private
from src.accounts import SessionUser, require_admin

_COMPUTED_AT = datetime(2026, 7, 29, 9, 15, tzinfo=timezone.utc)
_WINDOW_START = _COMPUTED_AT - timedelta(days=28)
_RUN = (_COMPUTED_AT, _WINDOW_START, _COMPUTED_AT)

_T1 = _COMPUTED_AT - timedelta(days=20)
_T2 = _COMPUTED_AT - timedelta(days=15)


def _account(
    account_id: int,
    *,
    display_name: str | None = "Rider",
    show_in_leaderboards: bool = True,
    show_public_username: bool = True,
    ruling_color: str | None = None,
    ruling_border_color: str | None = None,
    ruling_alpha=Decimal("0.60"),
) -> tuple:
    return (account_id, display_name, show_in_leaderboards, show_public_username,
            ruling_color, ruling_border_color, ruling_alpha)


_LEADER_ROWS = [
    (1, 101, 88, _T1),
    (2, 102, 40, _T2),
]
_ACCOUNTS_ALL_ELIGIBLE = [
    _account(101, display_name="Duke swift🦦"),
    _account(102, display_name="Rider2🦊"),
]


class _FakeCursor:
    def __init__(self, run, leader_rows, accounts):
        self._run = run
        self._leader_rows = leader_rows
        self._accounts = accounts
        self._last_sql = ""
        self._last_params = None

    def execute(self, sql, params=None):
        self._last_sql = sql
        self._last_params = params

    def fetchone(self):
        assert "h3_r8_area_leader_runs" in self._last_sql
        return self._run

    def fetchall(self):
        if "regional_leaders" in self._last_sql:
            return self._leader_rows
        if "FROM accounts" in self._last_sql:
            wanted = set(self._last_params[0])
            return [a for a in self._accounts if a[0] in wanted]
        raise AssertionError(f"unexpected fetchall for: {self._last_sql[:80]}")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, run, leader_rows, accounts):
        self._run = run
        self._leader_rows = leader_rows
        self._accounts = accounts

    def cursor(self):
        return _FakeCursor(self._run, self._leader_rows, self._accounts)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install(monkeypatch, module, run, leader_rows, accounts):
    @contextmanager
    def _conn():
        yield _FakeConn(run, leader_rows, accounts)

    monkeypatch.setattr(module, "connection", _conn)


def _request(headers=None) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/v1/leaderboard/regional",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "query_string": b"",
    })


def _call(headers=None):
    return api_leaderboard.leaderboard_regional(_request(headers), Response())


def test_503_when_no_run_exists(monkeypatch):
    _install(monkeypatch, api_leaderboard, None, [], [])
    with pytest.raises(Exception) as exc_info:
        _call()
    assert getattr(exc_info.value, "status_code", None) == 503


def test_top_level_shape_and_ranked_leaders(monkeypatch):
    _install(monkeypatch, api_leaderboard, _RUN, _LEADER_ROWS, _ACCOUNTS_ALL_ELIGIBLE)
    out = _call()
    assert out["computed_at"] == _COMPUTED_AT.isoformat()
    assert out["window_start"] == _WINDOW_START.isoformat()
    assert [e["rank"] for e in out["leaders"]] == [1, 2]
    assert [e["display_name"] for e in out["leaders"]] == ["Duke swift🦦", "Rider2🦊"]
    assert [e["points"] for e in out["leaders"]] == [88, 40]


def test_ineligible_entry_is_dropped_not_promoted_and_ranks_renumber(monkeypatch):
    accounts = [
        _account(101, show_in_leaderboards=False),   # opted out — dropped
        _account(102, display_name="Rider2🦊"),
    ]
    _install(monkeypatch, api_leaderboard, _RUN, _LEADER_ROWS, accounts)
    out = _call()
    assert [e["display_name"] for e in out["leaders"]] == ["Rider2🦊"]
    assert [e["rank"] for e in out["leaders"]] == [1], \
        "the survivor is renumbered to rank 1, not left at its stored rank 2"


def test_null_display_name_is_dropped(monkeypatch):
    accounts = [
        _account(101, display_name=None),
        _account(102, display_name="Rider2🦊"),
    ]
    _install(monkeypatch, api_leaderboard, _RUN, _LEADER_ROWS, accounts)
    out = _call()
    assert [e["display_name"] for e in out["leaders"]] == ["Rider2🦊"]


def test_empty_regional_leaders_yields_empty_list_not_an_error(monkeypatch):
    _install(monkeypatch, api_leaderboard, _RUN, [], [])
    out = _call()
    assert out["leaders"] == []


def test_etag_present_and_304_on_repeat(monkeypatch):
    _install(monkeypatch, api_leaderboard, _RUN, _LEADER_ROWS, _ACCOUNTS_ALL_ELIGIBLE)
    resp = Response()
    api_leaderboard.leaderboard_regional(_request(), resp)
    etag = resp.headers["ETag"]

    _install(monkeypatch, api_leaderboard, _RUN, _LEADER_ROWS, _ACCOUNTS_ALL_ELIGIBLE)
    resp2 = Response()
    out2 = api_leaderboard.leaderboard_regional(_request({"If-None-Match": etag}), resp2)
    assert out2.status_code == 304


# ---------------------------------------------------------------------------
# Admin sibling: GET /api/v1/private/regional-leaders
# ---------------------------------------------------------------------------
def _admin_client(monkeypatch, run, leader_rows, accounts):
    _install(monkeypatch, api_private, run, leader_rows, accounts)
    app = FastAPI()
    app.include_router(api_private.router)
    app.dependency_overrides[require_admin] = lambda: SessionUser(
        account_id=1, email="admin@example.com", scopes=("rider", "admin"),
        expires_at=datetime.now(timezone.utc), sliding=False,
        method="google", token_sha256="x",
    )
    return TestClient(app)


def test_private_regional_leaders_shows_full_ranks_unfiltered(monkeypatch):
    accounts = [_account(101, show_in_leaderboards=False)]  # opted out — still shown here
    client = _admin_client(monkeypatch, _RUN, _LEADER_ROWS, accounts)
    resp = client.get("/api/v1/private/regional-leaders")
    assert resp.status_code == 200
    body = resp.json()
    assert [e["account_id"] for e in body["leaders"]] == [101, 102]
    assert [e["rank"] for e in body["leaders"]] == [1, 2]
    assert body["viewed_by"] == "admin@example.com"


def test_private_regional_leaders_503_when_no_run_exists(monkeypatch):
    client = _admin_client(monkeypatch, None, [], [])
    resp = client.get("/api/v1/private/regional-leaders")
    assert resp.status_code == 503


def test_private_regional_leaders_requires_admin(monkeypatch):
    _install(monkeypatch, api_private, _RUN, _LEADER_ROWS, _ACCOUNTS_ALL_ELIGIBLE)
    app = FastAPI()
    app.include_router(api_private.router)
    r = TestClient(app).get("/api/v1/private/regional-leaders")
    assert r.status_code == 401
