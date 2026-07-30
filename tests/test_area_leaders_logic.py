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


# ---------------------------------------------------------------------------
# End-to-end through recompute() against a fake cursor/connection.
# ---------------------------------------------------------------------------
class _FakeCursor:
    """Matches the literal SQL text area_leaders.recompute() issues and
    returns canned rows. Also records every INSERT/DELETE it sees so the
    test can assert on exactly what would have been written.
    """

    def __init__(self, canned: dict) -> None:
        self._canned = canned
        self._result: list[tuple] = []
        self.deletes: list[str] = []
        self.inserted_report_rows: list[tuple] = []
        self.inserted_leader_rows: list[tuple] = []
        self.run_row: tuple | None = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @staticmethod
    def _norm(sql: str) -> str:
        return " ".join(sql.split())

    def execute(self, sql, params=()):
        s = self._norm(sql)
        self._result = []

        if s == "SELECT DISTINCT h3_8_index FROM device_history WHERE h3_8_index IS NOT NULL":
            self._result = [(c,) for c in self._canned["device_history_cells"]]
        elif s == ("SELECT DISTINCT current_h3_8_index FROM device_state "
                    "WHERE current_h3_8_index IS NOT NULL"):
            self._result = [(c,) for c in self._canned["device_state_cells"]]
        elif s == "SELECT DISTINCT h3_8_index FROM user_points WHERE status = %s":
            assert params == (area_leaders._CONFIRMED_STATUS,)
            self._result = [(c,) for c in self._canned["points_cells"]]
        elif s.startswith("SELECT h3_8_index, account_id, points, created_at, status FROM user_points"):
            self._result = self._canned["window_rows"]
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
        elif s.startswith("INSERT INTO h3_r8_area_leaders"):
            self.inserted_leader_rows.extend(rows)
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
def fake_recompute(monkeypatch):
    """Returns a function seed(canned) -> (result, cur) that runs
    area_leaders.recompute() against a fake connection built from `canned`.
    """

    def _run(canned: dict, window_days: int = 28):
        cur = _FakeCursor(canned)
        conn = _FakeConn(cur)

        @contextmanager
        def _fake_connection():
            yield conn

        monkeypatch.setattr(area_leaders, "connection", _fake_connection)
        result = area_leaders.recompute(window_days=window_days)
        return result, cur, conn

    return _run


def test_recompute_universe_union_deduped_across_sources(fake_recompute):
    canned = {
        "device_history_cells": [1, 2],
        "device_state_cells": [2, 3],          # 2 overlaps device_history
        "points_cells": [3, 4],                # 3 overlaps device_state
        "window_rows": [],
    }
    result, cur, conn = fake_recompute(canned)

    cells_written = {row[0] for row in cur.inserted_report_rows}
    assert cells_written == {1, 2, 3, 4}
    assert result["cell_count"] == 4
    assert conn.committed is True
    # DELETE ran exactly once, before the fresh INSERTs (full replace).
    assert cur.deletes == ["DELETE FROM h3_r8_area_report"]


def test_recompute_only_confirmed_rows_become_leaders_or_totals(fake_recompute):
    canned = {
        "device_history_cells": [],
        "device_state_cells": [],
        "points_cells": [100],
        "window_rows": [
            (100, 1, 40, _t(0), "confirmed"),
            (100, 2, 998, _t(1), "pending_review"),   # must not outrank account 1
        ],
    }
    result, cur, conn = fake_recompute(canned)

    leaders_for_100 = [row for row in cur.inserted_leader_rows if row[0] == 100]
    assert len(leaders_for_100) == 1
    _, rank, account_id, points, _first_point_at = leaders_for_100[0]
    assert (rank, account_id, points) == (1, 1, 40)

    report_row = next(row for row in cur.inserted_report_rows if row[0] == 100)
    _, has_devices, has_points, total_points, distinct_earners = report_row
    assert (total_points, distinct_earners) == (40, 1), \
        "the pending_review row must not count toward total_points/distinct_earners either"
    assert result["led_cells"] == 1


def test_recompute_stores_top_3_but_counts_every_earner_in_totals(fake_recompute):
    window_rows = [
        (100, acct, points, _t(acct), "confirmed")
        for acct, points in [(1, 10), (2, 40), (3, 30), (4, 20)]
    ]
    canned = {
        "device_history_cells": [],
        "device_state_cells": [],
        "points_cells": [100],
        "window_rows": window_rows,
    }
    result, cur, conn = fake_recompute(canned)

    leaders_for_100 = sorted(
        (row for row in cur.inserted_leader_rows if row[0] == 100), key=lambda r: r[1]
    )
    assert [row[2] for row in leaders_for_100] == [2, 3, 4], "top 3 by points DESC"
    assert [row[1] for row in leaders_for_100] == [1, 2, 3], "ranks 1..3"

    report_row = next(row for row in cur.inserted_report_rows if row[0] == 100)
    _, _has_devices, _has_points, total_points, distinct_earners = report_row
    assert total_points == 10 + 40 + 30 + 20
    assert distinct_earners == 4, "all 4 earners count even though only 3 are stored as leaders"


def test_recompute_tie_break_order_end_to_end(fake_recompute):
    # Two accounts tied on points; the earlier first_point_at wins rank 1.
    window_rows = [
        (100, 5, 50, _t(0), "confirmed"),
        (100, 5, 0,  _t(0), "confirmed"),     # same account, folded into one total
        (100, 9, 50, _t(-10), "confirmed"),   # earlier -> should outrank account 5
    ]
    canned = {
        "device_history_cells": [],
        "device_state_cells": [],
        "points_cells": [100],
        "window_rows": window_rows,
    }
    result, cur, conn = fake_recompute(canned)

    leaders_for_100 = sorted(
        (row for row in cur.inserted_leader_rows if row[0] == 100), key=lambda r: r[1]
    )
    assert [row[2] for row in leaders_for_100] == [9, 5]


def test_recompute_run_row_stamps_window_and_counts(fake_recompute):
    canned = {
        "device_history_cells": [1],
        "device_state_cells": [],
        "points_cells": [2],
        "window_rows": [(2, 1, 10, _t(0), "confirmed")],
    }
    result, cur, conn = fake_recompute(canned, window_days=28)

    computed_at, window_start, window_end, cell_count, led_cells = cur.run_row
    assert cell_count == 2
    assert led_cells == 1
    assert window_end - window_start == timedelta(days=28)
    assert window_end == computed_at
    assert result == {
        "run_id": 1,
        "computed_at": computed_at,
        "window_start": window_start,
        "window_end": window_end,
        "cell_count": 2,
        "led_cells": 1,
    }


def test_recompute_empty_universe_skips_inserts_without_error(fake_recompute):
    canned = {
        "device_history_cells": [],
        "device_state_cells": [],
        "points_cells": [],
        "window_rows": [],
    }
    result, cur, conn = fake_recompute(canned)
    assert result["cell_count"] == 0
    assert result["led_cells"] == 0
    assert cur.inserted_report_rows == []
    assert cur.inserted_leader_rows == []
    assert conn.committed is True
