"""Contract tests for GET /api/v1/leaderboard/map and its admin sibling
GET /api/v1/private/area-leaders (src/api_leaderboard.py, src/api_private.py).

Both are computed at READ time since sql/061 — there is no stored
`h3_r8_area_leaders` table any more — so the fixtures here are the rows the
LIVE query returns: `(h3_8_index, account_id, points, first_point_at)`
already grouped per (cell, account) and already in
`(h3_8_index, points DESC, first_point_at ASC, account_id ASC)` order,
exactly as the handler's SQL produces them. A fake cursor MUST replicate
that ordering, since the first eligible row per cell is taken as its
leader. The universe is a separate, much smaller read of
`h3_r8_area_report`.

Same SQL-dispatching fake-cursor idiom as tests/test_api_h3_aggregates.py
(dispatch on substrings of the executed SQL rather than a scripted
fetchone/fetchall sequence), because the account rows must be MUTABLE
across two calls within one test — the ETag tests flip an account's
`show_in_leaderboards` between requests and must observe the live join
change the payload on the second call.

The privacy rules are unchanged and are still the point of most of this
file: an ineligible earner is skipped and the next one falls through into
its place, while the cell's `total_points`/`distinct_earners` keep
counting everybody.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import h3
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from starlette.requests import Request

from src import api_leaderboard, api_private
from src.accounts import SessionUser, require_admin
from src.area_leaders import DEFAULT_WINDOW_DAYS, MAX_LEADERS_PER_CELL

_NOW = datetime.now(timezone.utc)
_T1 = _NOW - timedelta(days=20)
_T2 = _NOW - timedelta(days=15)
_T3 = _NOW - timedelta(days=10)

_CELL = int(h3.latlng_to_cell(39.7392, -104.9903, 8), 16)
_EMPTY_CELL = int(h3.latlng_to_cell(39.75, -105.0, 8), 16)


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


# Four earners in one cell, already tie-break ordered.
_TOTALS = [
    (_CELL, 101, 88, _T1),
    (_CELL, 102, 40, _T2),
    (_CELL, 103, 12, _T3),
    (_CELL, 104, 4, _T3),
]
_UNIVERSE = [(_CELL, True, True), (_EMPTY_CELL, True, False)]
_ALL_ELIGIBLE = [
    _account(101, display_name="Duke swift🦦"),
    _account(102, display_name="Rider2🦊"),
    _account(103, display_name="Rider3🦉"),
    _account(104, display_name="Rider4🦝"),
]


class _FakeCursor:
    def __init__(self, totals, universe, accounts, seen):
        self._totals, self._universe, self._accounts, self._seen = totals, universe, accounts, seen
        self._sql, self._params = "", None

    def execute(self, sql, params=None):
        self._sql, self._params = sql, params
        self._seen.append((sql, params))

    def fetchone(self):
        # The universe-refresh audit row — read by the admin endpoint only.
        return (_NOW, len(self._universe))

    def fetchall(self):
        if "FROM h3_r8_area_report" in self._sql:
            # The public endpoint asks for ids only; the admin one wants flags.
            if "has_devices" in self._sql:
                return list(self._universe)
            return [(c[0],) for c in self._universe]
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
    def __init__(self, totals, universe, accounts, seen):
        self._args = (totals, universe, accounts, seen)

    def cursor(self):
        return _FakeCursor(*self._args)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install(monkeypatch, module, totals, universe, accounts) -> list:
    seen: list = []

    @contextmanager
    def _conn():
        yield _FakeConn(totals, universe, accounts, seen)

    monkeypatch.setattr(module, "connection", _conn)
    return seen


def _request(headers=None) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/api/v1/leaderboard/map",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "query_string": b"",
    })


def _call(headers=None):
    return api_leaderboard.leaderboard_map(_request(headers), Response())


def _cell(out, cell_int=_CELL):
    return out["cells"][h3.int_to_str(cell_int)]


# ---------------------------------------------------------------------------
# Shape and the universe
# ---------------------------------------------------------------------------

def test_cell_keys_are_canonical_h3_strings_not_raw_integers(monkeypatch):
    _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, _ALL_ELIGIBLE)
    out = _call()
    for key in out["cells"]:
        assert isinstance(key, str) and h3.is_valid_cell(key)


def test_leader_runners_up_and_totals(monkeypatch):
    _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, _ALL_ELIGIBLE)
    cell = _cell(_call())
    assert cell["leader"]["display_name"] == "Duke swift🦦"
    assert [r["display_name"] for r in cell["runners_up"]] == ["Rider2🦊", "Rider3🦉"]
    assert cell["total_points"] == 144
    assert cell["distinct_earners"] == 4, "totals count every earner, not just the podium"


def test_podium_is_capped_but_totals_are_not(monkeypatch):
    _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, _ALL_ELIGIBLE)
    cell = _cell(_call())
    assert 1 + len(cell["runners_up"]) == MAX_LEADERS_PER_CELL
    assert cell["distinct_earners"] > MAX_LEADERS_PER_CELL


def test_universe_cell_with_no_points_renders_as_unclaimed(monkeypatch):
    _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, _ALL_ELIGIBLE)
    cell = _cell(_call(), _EMPTY_CELL)
    assert cell == {"total_points": 0, "distinct_earners": 0, "leader": None, "runners_up": []}


def test_a_cell_with_points_but_not_yet_in_the_universe_still_renders(monkeypatch):
    # The universe refresh is weekly now; a cell that earned its first point
    # since the last run must not wait up to a week to appear on the map.
    _install(monkeypatch, api_leaderboard, _TOTALS, [], _ALL_ELIGIBLE)
    out = _call()
    assert h3.int_to_str(_CELL) in out["cells"]
    assert _cell(out)["leader"]["display_name"] == "Duke swift🦦"


def test_no_universe_and_no_points_is_an_empty_map_not_a_503(monkeypatch):
    # The stored endpoint used to 503 before the first recompute.
    _install(monkeypatch, api_leaderboard, [], [], [])
    out = _call()
    assert out["cells"] == {}


def test_window_is_measured_from_now(monkeypatch):
    seen = _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, _ALL_ELIGIBLE)
    out = _call()
    computed_at = datetime.fromisoformat(out["computed_at"])
    window_start = datetime.fromisoformat(out["window_start"])
    assert out["window_end"] == out["computed_at"]
    assert computed_at - window_start == timedelta(days=DEFAULT_WINDOW_DAYS)
    points_sql, params = next((s, p) for s, p in seen if "FROM user_points" in s)
    assert params == (window_start,)
    assert "status = 'confirmed'" in points_sql


def test_tie_break_is_expressed_in_sql(monkeypatch):
    seen = _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, _ALL_ELIGIBLE)
    _call()
    points_sql = next(s for s, _ in seen if "FROM user_points" in s)
    assert "ORDER BY h3_8_index, points DESC, first_point_at ASC, account_id ASC" in points_sql


# ---------------------------------------------------------------------------
# Privacy — the read-time filter
# ---------------------------------------------------------------------------

def test_runners_up_omit_a_mid_list_ineligible_earner(monkeypatch):
    accounts = [
        _account(101, display_name="Duke swift🦦"),
        _account(102, show_in_leaderboards=False),
        _account(103, display_name="Rider3🦉"),
        _account(104, display_name="Rider4🦝"),
    ]
    _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, accounts)
    cell = _cell(_call())
    assert cell["leader"]["display_name"] == "Duke swift🦦"
    assert [r["display_name"] for r in cell["runners_up"]] == ["Rider3🦉", "Rider4🦝"], \
        "the next eligible earner falls through into the vacated slot"


def test_top_two_hidden_promotes_the_third(monkeypatch):
    accounts = [
        _account(101, show_in_leaderboards=False),
        _account(102, show_public_username=False),
        _account(103, display_name="Rider3🦉"),
        _account(104, display_name="Rider4🦝"),
    ]
    _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, accounts)
    cell = _cell(_call())
    assert cell["leader"]["display_name"] == "Rider3🦉"
    assert [r["display_name"] for r in cell["runners_up"]] == ["Rider4🦝"]


def test_everyone_hidden_leaves_a_null_leader_but_real_totals(monkeypatch):
    accounts = [_account(a, show_in_leaderboards=False) for a in (101, 102, 103, 104)]
    _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, accounts)
    cell = _cell(_call())
    assert cell["leader"] is None and cell["runners_up"] == []
    assert cell["total_points"] == 144 and cell["distinct_earners"] == 4, \
        "aggregate counts carry no identity and are never privacy-filtered"


def test_null_display_name_is_skipped_exactly_like_an_opt_out(monkeypatch):
    accounts = [
        _account(101, display_name=None),
        _account(102, display_name="Rider2🦊"),
        _account(103, display_name="Rider3🦉"),
        _account(104, display_name="Rider4🦝"),
    ]
    _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, accounts)
    assert _cell(_call())["leader"]["display_name"] == "Rider2🦊"


def test_a_missing_account_row_falls_through_rather_than_500(monkeypatch):
    _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, _ALL_ELIGIBLE[1:])
    assert _cell(_call())["leader"]["display_name"] == "Rider2🦊"


# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------

def test_null_color_pair_nulls_ruling_alpha_alongside_it(monkeypatch):
    _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, _ALL_ELIGIBLE)
    leader = _cell(_call())["leader"]
    assert leader["ruling_color"] is None
    assert leader["ruling_border_color"] is None
    assert leader["ruling_alpha"] is None, \
        "the column default 0.60 must not leak as if it were a real opacity"


def test_claimed_color_pair_passes_through_with_alpha_as_a_float(monkeypatch):
    accounts = [_account(101, display_name="Duke swift🦦", ruling_color="#7c54cd",
                         ruling_border_color="#382264", ruling_alpha=Decimal("0.60"))] + _ALL_ELIGIBLE[1:]
    _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, accounts)
    leader = _cell(_call())["leader"]
    assert leader["ruling_color"] == "#7c54cd"
    assert isinstance(leader["ruling_alpha"], float) and leader["ruling_alpha"] == 0.6


# ---------------------------------------------------------------------------
# ETag — content-only, because `computed_at` is now "when you asked"
# ---------------------------------------------------------------------------

def test_304_on_an_unchanged_map(monkeypatch):
    _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, _ALL_ELIGIBLE)
    resp = Response()
    api_leaderboard.leaderboard_map(_request(), resp)
    etag = resp.headers["ETag"]

    _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, _ALL_ELIGIBLE)
    out2 = api_leaderboard.leaderboard_map(_request({"If-None-Match": etag}), Response())
    assert out2.status_code == 304, (
        "`computed_at` moves every request — an ETag keyed on it would never 304"
    )


def test_etag_changes_the_moment_an_account_opts_out(monkeypatch):
    # The single most important test in this file: a held If-None-Match must
    # MISS as soon as someone opts out. This is the leak read-time filtering
    # exists to prevent, and a run-keyed ETag would have served a 304 that
    # resurrected them.
    _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, _ALL_ELIGIBLE)
    resp = Response()
    api_leaderboard.leaderboard_map(_request(), resp)
    etag = resp.headers["ETag"]

    hidden = [_account(101, show_in_leaderboards=False)] + _ALL_ELIGIBLE[1:]
    _install(monkeypatch, api_leaderboard, _TOTALS, _UNIVERSE, hidden)
    resp2 = Response()
    out2 = api_leaderboard.leaderboard_map(_request({"If-None-Match": etag}), resp2)
    assert not isinstance(out2, Response), "a changed payload must not 304"
    assert resp2.headers["ETag"] != etag


# ---------------------------------------------------------------------------
# Admin sibling: GET /api/v1/private/area-leaders
# ---------------------------------------------------------------------------

def _admin_client(monkeypatch, totals, universe, accounts):
    _install(monkeypatch, api_private, totals, universe, accounts)
    app = FastAPI()
    app.include_router(api_private.router)
    app.dependency_overrides[require_admin] = lambda: SessionUser(
        account_id=1, email="admin@example.com", scopes=("rider", "admin"),
        expires_at=datetime.now(timezone.utc), sliding=False,
        method="google", token_sha256="x",
    )
    return TestClient(app)


def test_private_area_leaders_is_unfiltered_with_real_account_ids(monkeypatch):
    accounts = [_account(101, show_in_leaderboards=False)] + _ALL_ELIGIBLE[1:]
    client = _admin_client(monkeypatch, _TOTALS, _UNIVERSE, accounts)
    body = client.get("/api/v1/private/area-leaders").json()
    cell = body["cells"][h3.int_to_str(_CELL)]
    assert [l["account_id"] for l in cell["leaders"]] == [101, 102, 103, 104], \
        "the opted-out rider is still visible to an admin, and nothing is capped at 3"
    assert body["viewed_by"] == "admin@example.com"


def test_private_area_leaders_answers_without_a_universe_refresh(monkeypatch):
    client = _admin_client(monkeypatch, [], [], [])
    resp = client.get("/api/v1/private/area-leaders")
    assert resp.status_code == 200, "no stored run to depend on any more, so no 503"
    assert resp.json()["cells"] == {}


def test_private_area_leaders_requires_admin(monkeypatch):
    _install(monkeypatch, api_private, _TOTALS, _UNIVERSE, _ALL_ELIGIBLE)
    app = FastAPI()
    app.include_router(api_private.router)
    assert TestClient(app).get("/api/v1/private/area-leaders").status_code == 401
