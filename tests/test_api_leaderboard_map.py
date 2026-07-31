"""Contract tests for GET /api/v1/leaderboard/map and its admin sibling
GET /api/v1/private/area-leaders (src/api_leaderboard.py, src/api_private.py).

Drives the real handlers with a hand-seeded fake connection over the
sql/048 table shapes (h3_r8_area_leader_runs / h3_r8_area_report /
h3_r8_area_leaders) and a live `accounts` join — the recompute lane
(src/area_leaders.py) is not needed to test this: the handler reads
whatever rows the fake cursor hands back, exactly like a live join would.

Follows the SQL-dispatching fake-cursor idiom from
tests/test_api_h3_aggregates.py (dispatch on substrings of the executed
SQL, rather than a scripted fetchone/fetchall sequence) because the
account rows must be MUTABLE across two calls within one test (the ETag
tests flip an account's show_in_leaderboards between requests and must
observe the live-join effect on the second call).

The single most important test here is
test_etag_changes_when_show_in_leaderboards_flips_with_computed_at_unchanged:
a held If-None-Match must MISS the moment an account opts out, even
though nothing in h3_r8_area_leader_runs changed — a 304 there would
resurrect an opted-out rider's leaderboard entry in every client cache
until the next daily recompute.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import h3
import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from starlette.requests import Request

from src import api_leaderboard, api_private
from src.accounts import SessionUser, require_admin

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_CELL = h3.latlng_to_cell(39.74, -104.98, 8)
_H3_IDX = h3.str_to_int(_CELL)
_CELL_EMPTY = h3.latlng_to_cell(39.80, -105.05, 8)
_H3_IDX_EMPTY = h3.str_to_int(_CELL_EMPTY)
assert _CELL != _CELL_EMPTY

_COMPUTED_AT = datetime(2026, 7, 29, 9, 15, tzinfo=timezone.utc)
_WINDOW_START = _COMPUTED_AT - timedelta(days=28)
_RUN = (_COMPUTED_AT, _WINDOW_START, _COMPUTED_AT)
_ADMIN_RUN = (_COMPUTED_AT, _WINDOW_START, _COMPUTED_AT, 720, 1)

_T1 = _COMPUTED_AT - timedelta(days=20)
_T2 = _COMPUTED_AT - timedelta(days=15)
_T3 = _COMPUTED_AT - timedelta(days=10)


def _report_rows(h3_idx=_H3_IDX, total_points=144, distinct_earners=3):
    """One cell with a full top-3, ordered (h3_8_index, rank ASC) — the
    handler's SQL ORDER BY. Rank 1 = account 101/88pts, rank 2 = account
    102/40pts, rank 3 = account 103/16pts."""
    return [
        (h3_idx, total_points, distinct_earners, 1, 101, 88, _T1),
        (h3_idx, total_points, distinct_earners, 2, 102, 40, _T2),
        (h3_idx, total_points, distinct_earners, 3, 103, 16, _T3),
    ]


def _report_rows_with_empty_cell():
    """A second, report-only cell with no leader rows at all — sql/048's
    'devices or points but nobody in the top 3 (yet)' case."""
    return _report_rows() + [
        (_H3_IDX_EMPTY, 0, 0, None, None, None, None),
    ]


def _admin_report_rows(h3_idx=_H3_IDX, total_points=144, distinct_earners=3):
    """The admin endpoint's wider SELECT: has_devices/has_points precede
    total_points/distinct_earners."""
    return [
        (h3_idx, True, True, total_points, distinct_earners, 1, 101, 88, _T1),
        (h3_idx, True, True, total_points, distinct_earners, 2, 102, 40, _T2),
        (h3_idx, True, True, total_points, distinct_earners, 3, 103, 16, _T3),
    ]


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


_ACCOUNTS_ALL_ELIGIBLE = [
    _account(101, display_name="Duke swift🦦", ruling_color="#7c54cd",
             ruling_border_color="#382264", ruling_alpha=Decimal("0.60")),
    _account(102, display_name="Rider2🦊"),
    _account(103, display_name="Rider3🐼"),
]


# ---------------------------------------------------------------------------
# Fake DB — SQL-dispatching, with a mutable accounts list so a test can flip
# a row's visibility between two calls and observe the live-join effect.
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, run, report_rows, accounts):
        self._run = run
        self._report_rows = report_rows
        self._accounts = accounts
        self._last_sql = ""
        self._last_params: tuple | None = None

    def execute(self, sql, params=None):
        self._last_sql = sql
        self._last_params = params

    def fetchone(self):
        assert "h3_r8_area_leader_runs" in self._last_sql
        return self._run

    def fetchall(self):
        if "h3_r8_area_report" in self._last_sql:
            return self._report_rows
        if "FROM accounts" in self._last_sql:
            wanted = set(self._last_params[0])
            return [a for a in self._accounts if a[0] in wanted]
        raise AssertionError(f"unexpected fetchall for: {self._last_sql[:80]}")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, run, report_rows, accounts):
        self._run = run
        self._report_rows = report_rows
        self._accounts = accounts

    def cursor(self):
        return _FakeCursor(self._run, self._report_rows, self._accounts)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install(monkeypatch, module, run, report_rows, accounts):
    @contextmanager
    def _conn():
        yield _FakeConn(run, report_rows, accounts)

    monkeypatch.setattr(module, "connection", _conn)


def _request(headers: dict[str, str] | None = None) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/v1/leaderboard/map",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "query_string": b"",
    })


def _call(headers=None):
    return api_leaderboard.leaderboard_map(_request(headers), Response())


def _call_with_response(headers=None):
    resp = Response()
    out = api_leaderboard.leaderboard_map(_request(headers), resp)
    return out, resp


# ---------------------------------------------------------------------------
# 503 — no run yet
# ---------------------------------------------------------------------------

def test_503_when_no_run_exists(monkeypatch):
    from fastapi import HTTPException
    _install(monkeypatch, api_leaderboard, None, [], [])
    with pytest.raises(HTTPException) as e:
        _call()
    assert e.value.status_code == 503


# ---------------------------------------------------------------------------
# Basic shape
# ---------------------------------------------------------------------------

def test_top_level_shape_and_h3_string_keys(monkeypatch):
    _install(monkeypatch, api_leaderboard, _RUN, _report_rows_with_empty_cell(),
             _ACCOUNTS_ALL_ELIGIBLE)
    out = _call()
    assert out["computed_at"] == _COMPUTED_AT.isoformat()
    assert out["window_start"] == _WINDOW_START.isoformat()
    assert out["window_end"] == _COMPUTED_AT.isoformat()
    assert set(out["cells"]) == {_CELL, _CELL_EMPTY}
    for key in out["cells"]:
        assert isinstance(key, str)
        assert h3.is_valid_cell(key)


def test_leader_and_runners_up_when_everyone_is_eligible(monkeypatch):
    _install(monkeypatch, api_leaderboard, _RUN, _report_rows(), _ACCOUNTS_ALL_ELIGIBLE)
    cell = _call()["cells"][_CELL]
    assert cell["total_points"] == 144
    assert cell["distinct_earners"] == 3
    assert cell["leader"]["display_name"] == "Duke swift🦦"
    assert cell["leader"]["points"] == 88
    assert [r["display_name"] for r in cell["runners_up"]] == ["Rider2🦊", "Rider3🐼"]


def test_cell_with_no_leader_rows_reports_null_leader_and_empty_runners_up(monkeypatch):
    _install(monkeypatch, api_leaderboard, _RUN, _report_rows_with_empty_cell(),
             _ACCOUNTS_ALL_ELIGIBLE)
    cell = _call()["cells"][_CELL_EMPTY]
    assert cell == {
        "total_points": 0, "distinct_earners": 0,
        "leader": None, "runners_up": [],
    }


# ---------------------------------------------------------------------------
# Privacy fall-through
# ---------------------------------------------------------------------------

def test_rank1_hidden_promotes_rank2_to_leader(monkeypatch):
    accounts = [
        _account(101, show_in_leaderboards=False),
        _account(102, display_name="Rider2🦊"),
        _account(103, display_name="Rider3🐼"),
    ]
    _install(monkeypatch, api_leaderboard, _RUN, _report_rows(), accounts)
    cell = _call()["cells"][_CELL]
    assert cell["leader"]["display_name"] == "Rider2🦊"
    assert cell["leader"]["points"] == 40
    assert [r["display_name"] for r in cell["runners_up"]] == ["Rider3🐼"]


def test_runners_up_omit_a_mid_list_ineligible_rank(monkeypatch):
    """Distinct from the rank1/rank1+2-hidden cases above: the ineligible
    account here is NOT the leader — rank 1 stays leader, rank 2 is hidden
    and must be dropped from runners_up entirely (no null placeholder, no
    off-by-one), leaving rank 3 as the sole runner-up."""
    accounts = [
        _account(101, display_name="Duke swift🦦"),
        _account(102, show_in_leaderboards=False),
        _account(103, display_name="Rider3🐼"),
    ]
    _install(monkeypatch, api_leaderboard, _RUN, _report_rows(), accounts)
    cell = _call()["cells"][_CELL]
    assert cell["leader"]["display_name"] == "Duke swift🦦"
    assert [r["display_name"] for r in cell["runners_up"]] == ["Rider3🐼"]


def test_ranks_1_and_2_hidden_promotes_rank3_with_empty_runners_up(monkeypatch):
    accounts = [
        _account(101, show_in_leaderboards=False),
        _account(102, show_public_username=False),
        _account(103, display_name="Rider3🐼"),
    ]
    _install(monkeypatch, api_leaderboard, _RUN, _report_rows(), accounts)
    cell = _call()["cells"][_CELL]
    assert cell["leader"]["display_name"] == "Rider3🐼"
    assert cell["leader"]["points"] == 16
    assert cell["runners_up"] == []


def test_all_three_hidden_leader_is_null_and_runners_up_empty(monkeypatch):
    accounts = [
        _account(101, show_in_leaderboards=False),
        _account(102, show_in_leaderboards=False),
        _account(103, show_public_username=False),
    ]
    _install(monkeypatch, api_leaderboard, _RUN, _report_rows(), accounts)
    cell = _call()["cells"][_CELL]
    assert cell["leader"] is None
    assert cell["runners_up"] == []
    # total_points/distinct_earners are aggregate, ledger-level facts and
    # are NOT privacy-filtered — they stay as stored even when nobody is
    # eligible to be shown by name.
    assert cell["total_points"] == 144
    assert cell["distinct_earners"] == 3


def test_null_display_name_is_skipped_exactly_like_opt_out(monkeypatch):
    """sql/025's never-backfilled-username edge case: display_name IS NULL
    (propagated from sql/044's generated column) must fall through exactly
    like show_in_leaderboards=false."""
    accounts = [
        _account(101, display_name=None),
        _account(102, display_name="Rider2🦊"),
        _account(103, display_name="Rider3🐼"),
    ]
    _install(monkeypatch, api_leaderboard, _RUN, _report_rows(), accounts)
    cell = _call()["cells"][_CELL]
    assert cell["leader"]["display_name"] == "Rider2🦊"


# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------

def test_null_color_pair_nulls_ruling_alpha_alongside_it(monkeypatch):
    """ruling_alpha has NOT NULL DEFAULT 0.60 in the schema — an unclaimed
    pair must not leak that default as a meaningful fill opacity."""
    accounts = [
        _account(101, ruling_color=None, ruling_border_color=None, ruling_alpha=Decimal("0.60")),
        _account(102),
        _account(103),
    ]
    _install(monkeypatch, api_leaderboard, _RUN, _report_rows(), accounts)
    leader = _call()["cells"][_CELL]["leader"]
    assert leader["ruling_color"] is None
    assert leader["ruling_border_color"] is None
    assert leader["ruling_alpha"] is None


def test_claimed_color_pair_passes_through_with_alpha_as_float(monkeypatch):
    accounts = [
        _account(101, ruling_color="#7c54cd", ruling_border_color="#382264",
                 ruling_alpha=Decimal("0.75")),
        _account(102),
        _account(103),
    ]
    _install(monkeypatch, api_leaderboard, _RUN, _report_rows(), accounts)
    leader = _call()["cells"][_CELL]["leader"]
    assert leader["ruling_color"] == "#7c54cd"
    assert leader["ruling_border_color"] == "#382264"
    assert leader["ruling_alpha"] == 0.75
    assert isinstance(leader["ruling_alpha"], float)


# ---------------------------------------------------------------------------
# ETag / 304 — the highest-stakes tests in this file
# ---------------------------------------------------------------------------

def test_etag_and_304_on_unchanged_state(monkeypatch):
    accounts = list(_ACCOUNTS_ALL_ELIGIBLE)
    _install(monkeypatch, api_leaderboard, _RUN, _report_rows(), accounts)
    out1, resp1 = _call_with_response()
    etag = resp1.headers["etag"]
    assert resp1.headers["cache-control"] == "public, max-age=600"
    assert etag.startswith('W/"arealb:')

    out2 = _call(headers={"If-None-Match": etag})
    assert isinstance(out2, Response)
    assert out2.status_code == 304


def test_etag_changes_when_show_in_leaderboards_flips_with_computed_at_unchanged(monkeypatch):
    """THE key test: an opt-out must MISS a held If-None-Match, not 304 the
    client back onto a body that still shows the opted-out rider."""
    accounts = list(_ACCOUNTS_ALL_ELIGIBLE)
    _install(monkeypatch, api_leaderboard, _RUN, _report_rows(), accounts)
    out1, resp1 = _call_with_response()
    etag1 = resp1.headers["etag"]
    assert out1["cells"][_CELL]["leader"]["display_name"] == "Duke swift🦦"

    # Same computed_at, same report rows — only the leader's visibility flips.
    accounts[0] = _account(101, display_name="Duke swift🦦", show_in_leaderboards=False)

    out2 = _call(headers={"If-None-Match": etag1})
    assert not isinstance(out2, Response), "must not 304 an opted-out rider back into view"
    assert out2["cells"][_CELL]["leader"]["display_name"] == "Rider2🦊"

    _, resp2 = _call_with_response()
    assert resp2.headers["etag"] != etag1


def test_new_run_with_identical_cells_but_different_computed_at_also_changes_etag(monkeypatch):
    accounts = list(_ACCOUNTS_ALL_ELIGIBLE)
    _install(monkeypatch, api_leaderboard, _RUN, _report_rows(), accounts)
    out1, resp1 = _call_with_response()
    etag1 = resp1.headers["etag"]

    new_computed_at = _COMPUTED_AT + timedelta(days=1)
    new_run = (new_computed_at, _WINDOW_START + timedelta(days=1), new_computed_at)
    _install(monkeypatch, api_leaderboard, new_run, _report_rows(), accounts)

    out2 = _call(headers={"If-None-Match": etag1})
    assert not isinstance(out2, Response), "a fresh run must not 304 even with identical cells"
    assert out2["cells"] == out1["cells"], "the rendered cells are in fact identical"
    assert out2["computed_at"] == new_computed_at.isoformat()

    _, resp2 = _call_with_response()
    assert resp2.headers["etag"] != etag1


# ---------------------------------------------------------------------------
# Canonical serialization property
# ---------------------------------------------------------------------------

def test_canonical_serialization_hash_is_independent_of_dict_key_order():
    a = {
        "8828308281fffff": {
            "total_points": 144, "distinct_earners": 3,
            "leader": {"display_name": "Duke swift🦦", "points": 88,
                       "ruling_color": "#7c54cd", "ruling_border_color": "#382264",
                       "ruling_alpha": 0.6},
            "runners_up": [],
        },
        "8828308283fffff": {"total_points": 0, "distinct_earners": 0,
                              "leader": None, "runners_up": []},
    }
    # Same data, every dict rebuilt with keys inserted in a different order.
    b = {
        "8828308283fffff": {"runners_up": [], "leader": None,
                              "distinct_earners": 0, "total_points": 0},
        "8828308281fffff": {
            "runners_up": [],
            "leader": {"ruling_alpha": 0.6, "ruling_border_color": "#382264",
                       "ruling_color": "#7c54cd", "points": 88,
                       "display_name": "Duke swift🦦"},
            "distinct_earners": 3, "total_points": 144,
        },
    }
    assert api_leaderboard._digest(a) == api_leaderboard._digest(b)


def test_canonical_serialization_hash_differs_for_different_data():
    a = {"cell": {"total_points": 1, "distinct_earners": 1, "leader": None, "runners_up": []}}
    b = {"cell": {"total_points": 2, "distinct_earners": 1, "leader": None, "runners_up": []}}
    assert api_leaderboard._digest(a) != api_leaderboard._digest(b)


# ---------------------------------------------------------------------------
# GET /api/v1/private/area-leaders — admin, no privacy filtering
# ---------------------------------------------------------------------------

def _admin_client(monkeypatch, run, report_rows, accounts):
    _install(monkeypatch, api_private, run, report_rows, accounts)
    app = FastAPI()
    app.include_router(api_private.router)
    app.dependency_overrides[require_admin] = lambda: SessionUser(
        account_id=1, email="admin@example.com", scopes=("rider", "admin"),
        expires_at=datetime.now(timezone.utc), sliding=False,
        method="google", token_sha256="x",
    )
    return TestClient(app)


def test_private_area_leaders_shows_full_ranks_with_real_account_ids_unfiltered(monkeypatch):
    """The admin view applies NO privacy filtering: an opted-out account
    still shows up with its rank, account_id, and points."""
    accounts = [
        _account(101, show_in_leaderboards=False),  # opted out; public view would skip
        _account(102, display_name="Rider2🦊"),
        _account(103, display_name="Rider3🐼"),
    ]
    client = _admin_client(monkeypatch, _ADMIN_RUN, _admin_report_rows(), accounts)
    r = client.get("/api/v1/private/area-leaders")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["viewed_by"] == "admin@example.com"
    assert body["cell_count"] == 720
    assert body["led_cells"] == 1
    leaders = body["cells"][_CELL]["leaders"]
    assert [(l["rank"], l["account_id"], l["points"]) for l in leaders] == [
        (1, 101, 88), (2, 102, 40), (3, 103, 16),
    ]


def test_private_area_leaders_503_when_no_run_exists(monkeypatch):
    client = _admin_client(monkeypatch, None, [], [])
    r = client.get("/api/v1/private/area-leaders")
    assert r.status_code == 503


def test_private_area_leaders_requires_admin(monkeypatch):
    _install(monkeypatch, api_private, _ADMIN_RUN, _report_rows(), _ACCOUNTS_ALL_ELIGIBLE)
    app = FastAPI()
    app.include_router(api_private.router)
    r = TestClient(app).get("/api/v1/private/area-leaders")
    assert r.status_code == 401
