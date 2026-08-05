"""Pure-logic + fake-cursor coverage for src/area_leaders.py
(sql/048_h3_r8_area_leaders.sql; FEATURE_PLAN_2026-07.md §11.3;
PLAN_RIDE_MODE_API.md Phase A4).

Three properties are asserted directly against the pure helper functions
(_build_universe / _aggregate_window_points / _rank_cell — no cursor, no
I/O) AND end-to-end through `recompute()` against a fake cursor that
records every executed statement:

  * universe union from three sources, overlaps deduped
  * only status='confirmed' ledger rows are ever counted
  * tie-break is exactly points DESC, first_point_at ASC, account_id ASC

The SQL text these fakes match against is deliberately brittle (whitespace-
normalized substring checks against the literal queries area_leaders.py
issues) — the point is to pin the shape of the orchestration, not to be a
general SQL simulator. Anything genuinely SQL-engine-dependent (real
UNION/window-function semantics, the FK cascade, full-replace idempotence
against a live database, the universe actually matching real
device_history/device_state/user_points contents) is
tests/test_area_leaders_pg.py's job, not this file's.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from src import area_leaders

_EPOCH = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _t(offset_seconds: int) -> datetime:
    return _EPOCH + timedelta(seconds=offset_seconds)


# ---------------------------------------------------------------------------
# Pure functions — no cursor at all.
# ---------------------------------------------------------------------------
class TestBuildUniverse:
    def test_union_dedupes_overlaps_across_all_three_sources(self):
        universe = area_leaders._build_universe(
            device_history_cells=[1, 2, 3],
            device_state_cells=[2, 4],       # 2 overlaps device_history
            points_cells=[3, 5],             # 3 overlaps device_history
        )
        assert set(universe) == {1, 2, 3, 4, 5}

    def test_flags_reflect_which_source(self):
        universe = area_leaders._build_universe(
            device_history_cells=[1],
            device_state_cells=[2],
            points_cells=[3],
        )
        assert universe[1] == {"has_devices": True, "has_points": False}
        assert universe[2] == {"has_devices": True, "has_points": False}
        assert universe[3] == {"has_devices": False, "has_points": True}

    def test_cell_in_all_three_sources_gets_both_flags(self):
        universe = area_leaders._build_universe(
            device_history_cells=[9],
            device_state_cells=[9],
            points_cells=[9],
        )
        assert universe[9] == {"has_devices": True, "has_points": True}

    def test_empty_sources_yield_empty_universe(self):
        assert area_leaders._build_universe([], [], []) == {}

    def test_duplicate_ids_within_one_source_dont_multiply_entries(self):
        universe = area_leaders._build_universe(
            device_history_cells=[1, 1, 1],
            device_state_cells=[],
            points_cells=[],
        )
        assert universe == {1: {"has_devices": True, "has_points": False}}


class TestAggregateWindowPoints:
    def test_only_confirmed_rows_counted(self):
        rows = [
            (100, 1, 10, _t(0), "confirmed"),
            (100, 1, 5000, _t(1), "pending_review"),   # must be ignored entirely
            (100, 2, 20, _t(2), "confirmed"),
        ]
        by_cell = area_leaders._aggregate_window_points(rows)
        entries = {e.account_id: e for e in by_cell[100]}
        assert set(entries) == {1, 2}
        assert entries[1].points == 10, "the pending_review row must not be summed in"

    def test_account_with_only_unconfirmed_points_is_absent_entirely(self):
        rows = [(100, 1, 998, _t(0), "pending_review")]
        by_cell = area_leaders._aggregate_window_points(rows)
        assert by_cell == {}

    def test_sums_points_and_keeps_earliest_created_at_per_account(self):
        rows = [
            (100, 1, 10, _t(50), "confirmed"),
            (100, 1, 15, _t(10), "confirmed"),   # earlier than the row above
            (100, 1, 5,  _t(90), "confirmed"),
        ]
        by_cell = area_leaders._aggregate_window_points(rows)
        (entry,) = by_cell[100]
        assert entry.points == 30
        assert entry.first_point_at == _t(10)

    def test_different_cells_kept_separate(self):
        rows = [
            (100, 1, 10, _t(0), "confirmed"),
            (200, 1, 10, _t(0), "confirmed"),
        ]
        by_cell = area_leaders._aggregate_window_points(rows)
        assert set(by_cell) == {100, 200}


class TestRankCell:
    def test_orders_by_points_desc(self):
        entries = [
            area_leaders._CellAccountTotal(1, 10, 6, _t(0)),
            area_leaders._CellAccountTotal(1, 20, 50, _t(0)),
            area_leaders._CellAccountTotal(1, 30, 20, _t(0)),
        ]
        ranked = area_leaders._rank_cell(entries)
        assert [e.account_id for e in ranked] == [20, 30, 10]

    def test_tie_on_points_broken_by_first_point_at_asc(self):
        entries = [
            area_leaders._CellAccountTotal(1, 10, 50, _t(100)),
            area_leaders._CellAccountTotal(1, 20, 50, _t(5)),    # earlier -> wins the tie
        ]
        ranked = area_leaders._rank_cell(entries)
        assert [e.account_id for e in ranked] == [20, 10]

    def test_tie_on_points_and_first_point_at_broken_by_account_id_asc(self):
        entries = [
            area_leaders._CellAccountTotal(1, 30, 50, _t(5)),
            area_leaders._CellAccountTotal(1, 10, 50, _t(5)),
            area_leaders._CellAccountTotal(1, 20, 50, _t(5)),
        ]
        ranked = area_leaders._rank_cell(entries)
        assert [e.account_id for e in ranked] == [10, 20, 30]

    def test_full_deterministic_order_exactly_as_specified(self):
        # points DESC, then first_point_at ASC, then account_id ASC — all
        # three tiers exercised in one call.
        entries = [
            area_leaders._CellAccountTotal(1, 1, 10, _t(0)),     # lowest points
            area_leaders._CellAccountTotal(1, 2, 50, _t(20)),    # tied points, later
            area_leaders._CellAccountTotal(1, 3, 50, _t(10)),    # tied points, earlier -> #1
            area_leaders._CellAccountTotal(1, 4, 30, _t(0)),
        ]
        ranked = area_leaders._rank_cell(entries)
        assert [e.account_id for e in ranked] == [3, 2, 4, 1]


class TestAggregateRegionalPoints:
    def test_sums_one_accounts_points_across_multiple_cells(self):
        by_cell = {
            100: [area_leaders._CellAccountTotal(100, 1, 10, _t(5))],
            200: [area_leaders._CellAccountTotal(200, 1, 15, _t(0))],   # same account, earlier
        }
        regional = area_leaders._aggregate_regional_points(by_cell)
        (entry,) = regional
        assert entry.account_id == 1
        assert entry.points == 25
        assert entry.first_point_at == _t(0), "earliest first_point_at across cells wins"

    def test_different_accounts_kept_separate_even_in_the_same_cell(self):
        by_cell = {
            100: [
                area_leaders._CellAccountTotal(100, 1, 10, _t(0)),
                area_leaders._CellAccountTotal(100, 2, 20, _t(0)),
            ],
        }
        regional = area_leaders._aggregate_regional_points(by_cell)
        by_account = {e.account_id: e.points for e in regional}
        assert by_account == {1: 10, 2: 20}

    def test_empty_by_cell_yields_empty_list(self):
        assert area_leaders._aggregate_regional_points({}) == []

    def test_result_is_rankable_with_rank_cell(self):
        by_cell = {
            100: [area_leaders._CellAccountTotal(100, 1, 5, _t(0))],
            200: [area_leaders._CellAccountTotal(200, 1, 5, _t(0)),
                  area_leaders._CellAccountTotal(200, 2, 50, _t(0))],
        }
        regional = area_leaders._aggregate_regional_points(by_cell)
        ranked = area_leaders._rank_cell(regional)
        # account 1: 5+5=10 across two cells; account 2: 50 in one cell.
        assert [e.account_id for e in ranked] == [2, 1]


# ---------------------------------------------------------------------------
# End-to-end through refresh_universe() against a fake cursor/connection.
#
# The job is universe-only since sql/061: it reads no points and ranks
# nobody. The ranking helpers above are still exercised as pure logic
# because the READ path (src/api_leaderboard.py) is what uses them now —
# and the last test in this file pins that endpoint's SQL ORDER BY to
# `_rank_cell`, so the two definitions of "who holds a territory" cannot
# drift apart now that they live in different modules.
# ---------------------------------------------------------------------------
class _FakeCursor:
    """Matches the literal SQL text area_leaders.refresh_universe() issues
    and returns canned rows. Also records every INSERT/DELETE it sees so a
    test can assert on exactly what would have been written."""

    def __init__(self, canned: dict) -> None:
        self._canned = canned
        self._result: list[tuple] = []
        self.deletes: list[str] = []
        self.inserted_report_rows: list[tuple] = []
        self.run_row: tuple | None = None
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @staticmethod
    def _norm(sql: str) -> str:
        return " ".join(sql.split())

    def execute(self, sql, params=()):
        s = self._norm(sql)
        self.statements.append(s)
        self._result = []

        if s == "SELECT DISTINCT h3_8_index FROM device_history WHERE h3_8_index IS NOT NULL":
            self._result = [(c,) for c in self._canned["device_history_cells"]]
        elif s == ("SELECT DISTINCT current_h3_8_index FROM device_state "
                    "WHERE current_h3_8_index IS NOT NULL"):
            self._result = [(c,) for c in self._canned["device_state_cells"]]
        elif s == "SELECT DISTINCT h3_8_index FROM user_points WHERE status = %s":
            assert params == (area_leaders._CONFIRMED_STATUS,)
            self._result = [(c,) for c in self._canned["points_cells"]]
        elif s == "DELETE FROM h3_r8_area_report":
            self.deletes.append(s)
        elif s.startswith("INSERT INTO h3_r8_area_leader_runs"):
            self.run_row = params
            self._result = [(1,)]
        else:
            raise AssertionError(f"unexpected SQL: {s!r}")

    def executemany(self, sql, rows):
        s = self._norm(sql)
        rows = list(rows)
        if s.startswith("INSERT INTO h3_r8_area_report"):
            self.inserted_report_rows.extend(rows)
        else:
            raise AssertionError(f"unexpected executemany SQL: {s!r}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


class _FakeConn:
    def __init__(self, cur: _FakeCursor) -> None:
        self._cur = cur
        self.committed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True


@pytest.fixture()
def fake_refresh(monkeypatch):
    """Returns a function seed(canned) -> (result, cur, conn) that runs
    area_leaders.refresh_universe() against a fake connection."""

    def _run(canned: dict, window_days: int = 28):
        cur = _FakeCursor(canned)
        conn = _FakeConn(cur)

        @contextmanager
        def _fake_connection():
            yield conn

        monkeypatch.setattr(area_leaders, "connection", _fake_connection)
        result = area_leaders.refresh_universe(window_days=window_days)
        return result, cur, conn

    return _run


def test_universe_is_the_union_of_all_three_sources_deduped(fake_refresh):
    _result, cur, conn = fake_refresh({
        "device_history_cells": [1, 2],
        "device_state_cells": [2, 3],       # 2 overlaps device_history
        "points_cells": [3, 4],             # 3 overlaps device_state
    })
    written = {row[0]: (row[1], row[2]) for row in cur.inserted_report_rows}
    assert sorted(written) == [1, 2, 3, 4], "each cell appears exactly once"
    assert written[1] == (True, False), "device_history only"
    assert written[2] == (True, False), "both device sources, still device-only"
    assert written[3] == (True, True), "device_state AND points"
    assert written[4] == (False, True), "points only"
    assert conn.committed


def test_full_replace_deletes_before_inserting(fake_refresh):
    _result, cur, _conn = fake_refresh({
        "device_history_cells": [1], "device_state_cells": [], "points_cells": [],
    })
    assert cur.deletes == ["DELETE FROM h3_r8_area_report"]


def test_only_confirmed_points_contribute_a_cell(fake_refresh):
    # The status filter lives in the SQL for this source, so the assertion
    # is on the parameter the job binds — the fake asserts it too.
    _result, cur, _conn = fake_refresh({
        "device_history_cells": [], "device_state_cells": [], "points_cells": [9],
    })
    assert [r[0] for r in cur.inserted_report_rows] == [9]


def test_no_points_are_read_and_nobody_is_ranked(fake_refresh):
    """The whole point of sql/061: the scheduled job stopped touching the
    leaderboard. A windowed ledger read here would be the old behavior
    creeping back in."""
    _result, cur, _conn = fake_refresh({
        "device_history_cells": [1], "device_state_cells": [], "points_cells": [1],
    })
    windowed = [s for s in cur.statements
                if "FROM user_points" in s and "created_at" in s]
    assert windowed == [], "refresh_universe must not read the points window"
    assert not any("h3_r8_area_leaders" in s for s in cur.statements)
    assert not any("regional_leaders" in s for s in cur.statements)


def test_run_row_records_the_refresh(fake_refresh):
    result, cur, _conn = fake_refresh({
        "device_history_cells": [1, 2], "device_state_cells": [], "points_cells": [3],
    }, window_days=28)
    run_at, cell_count = cur.run_row
    assert cell_count == 3
    assert result["cell_count"] == 3
    assert result["computed_at"] == run_at
    assert result["run_id"] == 1
    assert result["window_days"] == 28, \
        "reported for continuity with the read path's window; nothing here filters on it"


def test_empty_universe_skips_the_insert_without_error(fake_refresh):
    result, cur, conn = fake_refresh({
        "device_history_cells": [], "device_state_cells": [], "points_cells": [],
    })
    assert cur.inserted_report_rows == []
    assert cur.deletes == ["DELETE FROM h3_r8_area_report"], "still a full replace"
    assert result["cell_count"] == 0
    assert conn.committed


# ---------------------------------------------------------------------------
# The tie-break lives in two places now — `_rank_cell` here, and an ORDER BY
# in src/api_leaderboard.py. This holds them to each other.
# ---------------------------------------------------------------------------
def test_read_paths_sql_order_by_matches_rank_cell(monkeypatch):
    from src import api_leaderboard

    assert api_leaderboard._TIE_BREAK == "points DESC, first_point_at ASC, account_id ASC"

    # And that string really is what `_rank_cell` does: same points, earlier
    # first_point_at wins; same both, lower account_id wins.
    t_early = datetime(2026, 7, 1, tzinfo=timezone.utc)
    t_late = datetime(2026, 7, 2, tzinfo=timezone.utc)
    entries = [
        area_leaders._CellAccountTotal(1, 300, 10, t_late),
        area_leaders._CellAccountTotal(1, 100, 10, t_early),
        area_leaders._CellAccountTotal(1, 200, 10, t_early),
        area_leaders._CellAccountTotal(1, 400, 99, t_late),
    ]
    ranked = area_leaders._rank_cell(entries)
    assert [e.account_id for e in ranked] == [400, 100, 200, 300], (
        "points DESC first, then first_point_at ASC, then account_id ASC — "
        "the same order the endpoint's ORDER BY asks Postgres for"
    )
