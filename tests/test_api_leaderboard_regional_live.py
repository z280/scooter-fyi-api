"""Contract tests for GET /api/v1/leaderboard/regional/live
(src/api_leaderboard.py:leaderboard_regional_live) — the request-time
aggregate behind the Leaderboard panel's "Total Regional Points (live)"
tally, as opposed to its nightly sibling GET /api/v1/leaderboard/regional.

Same fake-cursor idiom as tests/test_api_leaderboard_regional.py, but the
fixture rows are the ones the LIVE query returns — (account_id, points,
first_point_at) straight off `user_points`, already in the endpoint's
`points DESC, first_point_at ASC, account_id ASC` order — and there is no
run metadata read at all: this endpoint has no `h3_r8_area_leader_runs`
row behind it, which is exactly why it does not 503 on a database that
has never been recomputed.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import Response
from starlette.requests import Request

from src import api_leaderboard
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
_ACCOUNTS_ALL_ELIGIBLE = [
    _account(101, display_name="Duke swift🦦"),
    _account(102, display_name="Rider2🦊"),
]


class _FakeCursor:
    def __init__(self, totals, accounts, seen_sql):
        self._totals = totals
        self._accounts = accounts
        self._seen_sql = seen_sql
        self._last_sql = ""
        self._last_params = None

    def execute(self, sql, params=None):
        self._last_sql = sql
        self._last_params = params
        self._seen_sql.append((sql, params))

    def fetchall(self):
        if "FROM user_points" in self._last_sql:
            return self._totals
        if "FROM accounts" in self._last_sql:
            wanted = set(self._last_params[0])
            return [a for a in self._accounts if a[0] in wanted]
        raise AssertionError(f"unexpected fetchall for: {self._last_sql[:80]}")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, totals, accounts, seen_sql):
        self._totals = totals
        self._accounts = accounts
        self._seen_sql = seen_sql

    def cursor(self):
        return _FakeCursor(self._totals, self._accounts, self._seen_sql)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install(monkeypatch, totals, accounts) -> list:
    seen_sql: list = []

    @contextmanager
    def _conn():
        yield _FakeConn(totals, accounts, seen_sql)

    monkeypatch.setattr(api_leaderboard, "connection", _conn)
    return seen_sql


def _request(headers=None) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/v1/leaderboard/regional/live",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "query_string": b"",
    })


def _call(headers=None):
    return api_leaderboard.leaderboard_regional_live(_request(headers), Response())


def test_top_level_shape_and_ranked_leaders(monkeypatch):
    _install(monkeypatch, _TOTALS, _ACCOUNTS_ALL_ELIGIBLE)
    out = _call()
    assert [e["rank"] for e in out["leaders"]] == [1, 2]
    assert [e["display_name"] for e in out["leaders"]] == ["Duke swift🦦", "Rider2🦊"]
    assert [e["points"] for e in out["leaders"]] == [88, 40]


def test_window_is_measured_from_now_not_from_a_recompute(monkeypatch):
    seen_sql = _install(monkeypatch, _TOTALS, _ACCOUNTS_ALL_ELIGIBLE)
    out = _call()
    computed_at = datetime.fromisoformat(out["computed_at"])
    window_start = datetime.fromisoformat(out["window_start"])
    assert out["window_end"] == out["computed_at"]
    assert computed_at - window_start == timedelta(days=DEFAULT_WINDOW_DAYS)
    # The same instant is what the ledger query was actually narrowed to —
    # not merely reported in the payload.
    points_sql, params = seen_sql[0]
    assert "FROM user_points" in points_sql
    assert params == (window_start,)


def test_no_run_metadata_is_read_so_a_never_recomputed_db_still_answers(monkeypatch):
    seen_sql = _install(monkeypatch, [], [])
    out = _call()
    assert out["leaders"] == []
    assert not any("h3_r8_area_leader_runs" in sql for sql, _ in seen_sql), (
        "the live tally must not depend on the nightly run — that dependency is "
        "what makes the stored endpoint 503 before the first recompute"
    )


def test_only_confirmed_ledger_rows_are_aggregated(monkeypatch):
    seen_sql = _install(monkeypatch, _TOTALS, _ACCOUNTS_ALL_ELIGIBLE)
    _call()
    points_sql, _ = seen_sql[0]
    assert "status = 'confirmed'" in points_sql


def test_tie_break_matches_the_stored_dashboards_ordering(monkeypatch):
    seen_sql = _install(monkeypatch, _TOTALS, _ACCOUNTS_ALL_ELIGIBLE)
    _call()
    points_sql, _ = seen_sql[0]
    # area_leaders._rank_cell's rule, expressed in SQL: points DESC, then
    # "whoever got there first holds it", then account_id.
    assert "ORDER BY points DESC, first_point_at ASC, account_id ASC" in points_sql


def test_ineligible_entry_is_dropped_and_ranks_renumber(monkeypatch):
    accounts = [
        _account(101, show_in_leaderboards=False),   # opted out — dropped
        _account(102, display_name="Rider2🦊"),
    ]
    _install(monkeypatch, _TOTALS, accounts)
    out = _call()
    assert [e["display_name"] for e in out["leaders"]] == ["Rider2🦊"]
    assert [e["rank"] for e in out["leaders"]] == [1]


def test_null_display_name_is_dropped(monkeypatch):
    accounts = [
        _account(101, display_name=None),
        _account(102, display_name="Rider2🦊"),
    ]
    _install(monkeypatch, _TOTALS, accounts)
    out = _call()
    assert [e["display_name"] for e in out["leaders"]] == ["Rider2🦊"]


def test_depth_is_capped_at_max_regional_leaders_after_filtering(monkeypatch):
    # One more earner than the published depth, and the very top one opted
    # out: a SQL-side LIMIT would have returned MAX - 1 eligible entries.
    n = MAX_REGIONAL_LEADERS + 1
    totals = [(200 + i, 1000 - i, _T1) for i in range(n)]
    accounts = [_account(200, show_in_leaderboards=False)] + [
        _account(200 + i, display_name=f"Rider{i}🦊") for i in range(1, n)
    ]
    _install(monkeypatch, totals, accounts)
    out = _call()
    assert len(out["leaders"]) == MAX_REGIONAL_LEADERS
    assert [e["rank"] for e in out["leaders"]] == list(range(1, MAX_REGIONAL_LEADERS + 1))
    assert out["leaders"][0]["display_name"] == "Rider1🦊"


def test_unclaimed_color_pair_nulls_the_alpha(monkeypatch):
    _install(monkeypatch, _TOTALS, _ACCOUNTS_ALL_ELIGIBLE)
    out = _call()
    assert out["leaders"][0]["ruling_color"] is None
    assert out["leaders"][0]["ruling_alpha"] is None


def test_etag_present_and_304_on_an_unchanged_tally(monkeypatch):
    _install(monkeypatch, _TOTALS, _ACCOUNTS_ALL_ELIGIBLE)
    resp = Response()
    api_leaderboard.leaderboard_regional_live(_request(), resp)
    etag = resp.headers["ETag"]

    _install(monkeypatch, _TOTALS, _ACCOUNTS_ALL_ELIGIBLE)
    resp2 = Response()
    out2 = api_leaderboard.leaderboard_regional_live(_request({"If-None-Match": etag}), resp2)
    assert out2.status_code == 304, (
        "`now` moves on every request — an ETag keyed on it instead of on the "
        "tally's content would never produce a 304"
    )


def test_etag_changes_when_the_tally_does(monkeypatch):
    _install(monkeypatch, _TOTALS, _ACCOUNTS_ALL_ELIGIBLE)
    resp = Response()
    api_leaderboard.leaderboard_regional_live(_request(), resp)
    etag = resp.headers["ETag"]

    _install(monkeypatch, [(101, 99, _T1), (102, 40, _T2)], _ACCOUNTS_ALL_ELIGIBLE)
    resp2 = Response()
    out2 = api_leaderboard.leaderboard_regional_live(_request({"If-None-Match": etag}), resp2)
    assert not isinstance(out2, Response), "a changed tally must not 304"
    assert resp2.headers["ETag"] != etag
