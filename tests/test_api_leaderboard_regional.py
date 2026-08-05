"""Contract tests for GET /api/v1/leaderboard/regional and its admin
sibling GET /api/v1/private/regional-leaders (src/api_leaderboard.py,
src/api_private.py) — the whole-database companion to
GET /api/v1/leaderboard/map.

Live since sql/061, which dropped the stored `regional_leaders` table, so
the fixtures are the rows the live GROUP BY returns:
`(account_id, points, first_point_at)`, already in the endpoint's
`points DESC, first_point_at ASC, account_id ASC` order.

Two things distinguish this endpoint from its per-cell sibling and get
their own tests here:

  * the depth cap is applied to ELIGIBLE entries, not in SQL — filtering
    happens after the aggregate, so a pre-truncated set would come up
    short whenever a top earner had opted out; and
  * no run metadata is read at all, which is why a database that has never
    had a universe refresh still answers instead of 503ing.

(This file absorbed tests/test_api_leaderboard_regional_live.py, which
covered the same endpoint back when the live version had a separate URL.)
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from starlette.requests import Request

from src import api_leaderboard, api_private
from src.accounts import SessionUser, require_admin
from src.area_leaders import DEFAULT_WINDOW_DAYS, MAX_REGIONAL_LEADERS

_NOW = datetime.now(timezone.utc)
_T1 = _NOW - timedelta(days=20)
_T2 = _NOW - timedelta(days=15)


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


# (account_id, points, first_point_at) — the shape of the live GROUP BY.
_TOTALS = [
    (101, 88, _T1),
    (102, 40, _T2),
]
_ALL_ELIGIBLE = [
    _account(101, display_name="Duke swift🦦"),
    _account(102, display_name="Rider2🦊"),
]


class _FakeCursor:
    def __init__(self, totals, accounts, seen):
        self._totals, self._accounts, self._seen = totals, accounts, seen
        self._sql, self._params = "", None

    def execute(self, sql, params=None):
        self._sql, self._params = sql, params
        self._seen.append((sql, params))

    def fetchall(self):
        if "FROM user_points" in self._sql:
            return list(self._totals)
        if "FROM accounts" in self._sql:
            wanted = set(self._params[0])
            return [a for a in self._accounts if a[0] in wanted]
        raise AssertionError(f"unexpected fetchall for: {self._sql[:80]}")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, totals, accounts, seen):
        self._args = (totals, accounts, seen)

    def cursor(self):
        return _FakeCursor(*self._args)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install(monkeypatch, module, totals, accounts) -> list:
    seen: list = []

    @contextmanager
    def _conn():
        yield _FakeConn(totals, accounts, seen)

    monkeypatch.setattr(module, "connection", _conn)
    return seen


def _request(headers=None) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/api/v1/leaderboard/regional",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "query_string": b"",
    })


def _call(headers=None):
    return api_leaderboard.leaderboard_regional(_request(headers), Response())


def test_top_level_shape_and_ranked_leaders(monkeypatch):
    _install(monkeypatch, api_leaderboard, _TOTALS, _ALL_ELIGIBLE)
    out = _call()
    assert [e["rank"] for e in out["leaders"]] == [1, 2]
    assert [e["display_name"] for e in out["leaders"]] == ["Duke swift🦦", "Rider2🦊"]
    assert [e["points"] for e in out["leaders"]] == [88, 40]


def test_window_is_measured_from_now(monkeypatch):
    seen = _install(monkeypatch, api_leaderboard, _TOTALS, _ALL_ELIGIBLE)
    out = _call()
    computed_at = datetime.fromisoformat(out["computed_at"])
    window_start = datetime.fromisoformat(out["window_start"])
    assert out["window_end"] == out["computed_at"]
    assert computed_at - window_start == timedelta(days=DEFAULT_WINDOW_DAYS)
    # The same instant the ledger query was actually narrowed to — not
    # merely a number reported in the payload.
    points_sql, params = next((s, p) for s, p in seen if "FROM user_points" in s)
    assert params == (window_start,)
    assert "status = 'confirmed'" in points_sql


def test_no_run_metadata_is_read_so_a_never_refreshed_db_still_answers(monkeypatch):
    seen = _install(monkeypatch, api_leaderboard, [], [])
    out = _call()
    assert out["leaders"] == []
    assert not any("h3_r8_area_leader_runs" in sql for sql, _ in seen), (
        "the regional board depends on no scheduled job at all — that dependency "
        "is what used to make it 503 before the first recompute"
    )


def test_tie_break_matches_the_recompute_lanes_ordering(monkeypatch):
    seen = _install(monkeypatch, api_leaderboard, _TOTALS, _ALL_ELIGIBLE)
    _call()
    points_sql = next(s for s, _ in seen if "FROM user_points" in s)
    # area_leaders._rank_cell's rule, expressed in SQL: points first, then
    # "whoever got there first holds it", then account_id to make it total.
    assert "ORDER BY points DESC, first_point_at ASC, account_id ASC" in points_sql


def test_ineligible_entry_is_dropped_and_ranks_renumber(monkeypatch):
    accounts = [
        _account(101, show_in_leaderboards=False),   # opted out — dropped
        _account(102, display_name="Rider2🦊"),
    ]
    _install(monkeypatch, api_leaderboard, _TOTALS, accounts)
    out = _call()
    assert [e["display_name"] for e in out["leaders"]] == ["Rider2🦊"]
    assert [e["rank"] for e in out["leaders"]] == [1], \
        "the survivor is renumbered to rank 1, not left at its original rank 2"


def test_null_display_name_is_dropped(monkeypatch):
    accounts = [
        _account(101, display_name=None),
        _account(102, display_name="Rider2🦊"),
    ]
    _install(monkeypatch, api_leaderboard, _TOTALS, accounts)
    assert [e["display_name"] for e in _call()["leaders"]] == ["Rider2🦊"]


def test_empty_ledger_yields_an_empty_list_not_an_error(monkeypatch):
    _install(monkeypatch, api_leaderboard, [], [])
    assert _call()["leaders"] == []


def test_depth_is_capped_after_filtering_not_in_sql(monkeypatch):
    # One more earner than the published depth, and the very top one opted
    # out: a SQL-side LIMIT would have returned MAX - 1 eligible entries.
    n = MAX_REGIONAL_LEADERS + 1
    totals = [(200 + i, 1000 - i, _T1) for i in range(n)]
    accounts = [_account(200, show_in_leaderboards=False)] + [
        _account(200 + i, display_name=f"Rider{i}🦊") for i in range(1, n)
    ]
    seen = _install(monkeypatch, api_leaderboard, totals, accounts)
    out = _call()
    assert len(out["leaders"]) == MAX_REGIONAL_LEADERS
    assert [e["rank"] for e in out["leaders"]] == list(range(1, MAX_REGIONAL_LEADERS + 1))
    assert out["leaders"][0]["display_name"] == "Rider1🦊"
    points_sql = next(s for s, _ in seen if "FROM user_points" in s)
    assert "LIMIT" not in points_sql.upper(), \
        "capping in SQL would silently return a short list whenever a top earner opted out"


def test_unclaimed_color_pair_nulls_the_alpha(monkeypatch):
    _install(monkeypatch, api_leaderboard, _TOTALS, _ALL_ELIGIBLE)
    out = _call()
    assert out["leaders"][0]["ruling_color"] is None
    assert out["leaders"][0]["ruling_alpha"] is None


def test_304_on_an_unchanged_tally(monkeypatch):
    _install(monkeypatch, api_leaderboard, _TOTALS, _ALL_ELIGIBLE)
    resp = Response()
    api_leaderboard.leaderboard_regional(_request(), resp)
    etag = resp.headers["ETag"]

    _install(monkeypatch, api_leaderboard, _TOTALS, _ALL_ELIGIBLE)
    out2 = api_leaderboard.leaderboard_regional(_request({"If-None-Match": etag}), Response())
    assert out2.status_code == 304, (
        "`computed_at` moves every request — an ETag keyed on it instead of on "
        "the tally's content would never produce a 304"
    )


def test_etag_changes_when_the_tally_does(monkeypatch):
    _install(monkeypatch, api_leaderboard, _TOTALS, _ALL_ELIGIBLE)
    resp = Response()
    api_leaderboard.leaderboard_regional(_request(), resp)
    etag = resp.headers["ETag"]

    _install(monkeypatch, api_leaderboard, [(101, 99, _T1), (102, 40, _T2)], _ALL_ELIGIBLE)
    resp2 = Response()
    out2 = api_leaderboard.leaderboard_regional(_request({"If-None-Match": etag}), resp2)
    assert not isinstance(out2, Response), "a changed tally must not 304"
    assert resp2.headers["ETag"] != etag


# ---------------------------------------------------------------------------
# Admin sibling: GET /api/v1/private/regional-leaders
# ---------------------------------------------------------------------------

def _admin_client(monkeypatch, totals, accounts):
    seen = _install(monkeypatch, api_private, totals, accounts)
    app = FastAPI()
    app.include_router(api_private.router)
    app.dependency_overrides[require_admin] = lambda: SessionUser(
        account_id=1, email="admin@example.com", scopes=("rider", "admin"),
        expires_at=datetime.now(timezone.utc), sliding=False,
        method="google", token_sha256="x",
    )
    return TestClient(app), seen


def test_private_regional_leaders_is_unfiltered(monkeypatch):
    accounts = [_account(101, show_in_leaderboards=False)]  # opted out — still shown here
    client, _ = _admin_client(monkeypatch, _TOTALS, accounts)
    body = client.get("/api/v1/private/regional-leaders").json()
    assert [e["account_id"] for e in body["leaders"]] == [101, 102]
    assert [e["rank"] for e in body["leaders"]] == [1, 2]
    assert body["viewed_by"] == "admin@example.com"


def test_private_regional_leaders_may_cap_in_sql_because_it_filters_nothing(monkeypatch):
    client, seen = _admin_client(monkeypatch, _TOTALS, _ALL_ELIGIBLE)
    client.get("/api/v1/private/regional-leaders")
    points_sql = next(s for s, _ in seen if "FROM user_points" in s)
    assert "LIMIT" in points_sql.upper(), (
        "nothing is dropped from this view, so the cap can be pushed into SQL — "
        "unlike the public endpoint, where filtering happens after the aggregate"
    )


def test_private_regional_leaders_answers_without_a_universe_refresh(monkeypatch):
    client, _ = _admin_client(monkeypatch, [], [])
    resp = client.get("/api/v1/private/regional-leaders")
    assert resp.status_code == 200
    assert resp.json()["leaders"] == []


def test_private_regional_leaders_requires_admin(monkeypatch):
    _install(monkeypatch, api_private, _TOTALS, _ALL_ELIGIBLE)
    app = FastAPI()
    app.include_router(api_private.router)
    assert TestClient(app).get("/api/v1/private/regional-leaders").status_code == 401
