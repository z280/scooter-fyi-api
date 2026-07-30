"""H3 r8 area leader report (FEATURE_PLAN_2026-07.md §11 /
PLAN_RIDE_MODE_API.md Phase A4; sql/048_h3_r8_area_leaders.sql).

"All r8 hexagons in the local network, with the user who earned the most
points there in the last four weeks, recalculated." `recompute()` is the
nightly job (`python -m src.cli recompute_area_leaders`, crontab
`15 9 * * *`); this module owns only the recompute side — the read
endpoints (`GET /api/v1/leaderboard/map`, `GET /api/v1/private/area-leaders`)
and their read-time privacy filtering live in src/api_leaderboard.py /
src/api_private.py (a different lane; not touched here).

UNIVERSE (h3_r8_area_report rows): every r8 cell that has ever had an
observed device OR points history — ALL-TIME, not windowed ("720 distinct
r8 cells observed all-time today", FEATURE_PLAN §11.1) — unioned from three
sources:

    SELECT DISTINCT h3_8_index         FROM device_history
    SELECT DISTINCT current_h3_8_index FROM device_state
    SELECT DISTINCT h3_8_index         FROM user_points WHERE status = 'confirmed'

device_history is ~7.3M rows with no index on h3_8_index — the plain
DISTINCT scan is a deliberate, acknowledged cost ("a seq scan of a few
seconds, once a day, off-peak" — FEATURE_PLAN §11.1) rather than maintaining
a ~150 MB index at boot in every environment for one daily query. Each of
the three queries returns at most a few hundred small integers, so the
union/dedup happens in Python (`_build_universe`) rather than as a fourth
SQL UNION — that also makes the union-with-overlaps logic directly
unit-testable with a fake cursor instead of only through a live Postgres
run.

WINDOW (what total_points / distinct_earners / the leaders themselves
measure): the trailing `window_days` (28) ending at the run's start,
stamped into h3_r8_area_leader_runs so the report says what it measured.
Only `status = 'confirmed'` ledger rows ever count — sql/028's own
docstring: nothing in this codebase's current era writes any other status,
but a future moderator-approval workflow might, and this report must not
count a row nobody has confirmed. The confirmed-only filter and the
points/first_point_at aggregation both happen in Python
(`_aggregate_window_points`) for the same testability reason as the
universe union: user_points, unlike device_history, is naturally bounded by
the 28-day window (SQL narrows to that range before any row reaches
Python), so aggregating client-side costs nothing at this scale and buys a
tie-break implementation that a fake-cursor test can exercise directly.

TIE-BREAK (`_rank_cell`), deterministic and total: `points DESC`, then
`first_point_at ASC` ("whoever got there first holds the territory"), then
`account_id ASC` as the final tiebreak. Top 3 per cell are stored — not just
the winner — because privacy (`show_in_leaderboards` / `show_public_username`)
is applied at READ time by the endpoint and can flip at any moment; storing
only the winner would mean an opt-out blanks a hex until tomorrow's run
instead of falling through to the runner-up immediately.

FULL-REPLACE, not accumulation — mirrors src/daily_trips.py:compute_for_date.
One transaction: `DELETE FROM h3_r8_area_report` (cascades to
h3_r8_area_leaders via its FK) -> INSERT the fresh universe -> INSERT the
fresh leaders -> INSERT one new h3_r8_area_leader_runs row. The runs table is
the one exception to "replace": it is an APPEND-ONLY audit log, one row per
call, never deleted here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .pg import connection

log = logging.getLogger(__name__)

# FEATURE_PLAN §11.1 / §11.3: trailing 28 days, ending at the run's start.
DEFAULT_WINDOW_DAYS = 28

# The only ledger status this report ever counts (sql/028_user_points.sql:
# every credit path in this codebase's current era goes straight to
# 'confirmed'; 'pending_review' exists for a future moderator-approval
# workflow and must stay invisible to this report until something approves
# it).
_CONFIRMED_STATUS = "confirmed"

# Top 3 per cell, not just the winner — see the module docstring.
_MAX_LEADERS_PER_CELL = 3


@dataclass(frozen=True)
class _CellAccountTotal:
    """One account's summed, confirmed, in-window points in one r8 cell."""

    h3_8_index: int
    account_id: int
    points: int
    first_point_at: datetime


# ---------------------------------------------------------------------------
# Pure logic — no cursor, no I/O. Covered by tests/test_area_leaders_logic.py
# with a fake cursor feeding canned rows; tests/test_area_leaders_pg.py
# covers the SQL that produces those rows against a real Postgres.
# ---------------------------------------------------------------------------
def _build_universe(
    device_history_cells: Iterable[int],
    device_state_cells: Iterable[int],
    points_cells: Iterable[int],
) -> dict[int, dict[str, bool]]:
    """Union three cell-id sources into ``{h3_8_index: {"has_devices": bool,
    "has_points": bool}}``, deduping cells that appear in more than one
    source. Callers are responsible for any filtering each source needs
    BEFORE it reaches here (e.g. ``status = 'confirmed'`` for
    ``points_cells``) — this function only merges and dedupes.
    """
    universe: dict[int, dict[str, bool]] = {}
    for h3_8_index in device_history_cells:
        flags = universe.setdefault(h3_8_index, {"has_devices": False, "has_points": False})
        flags["has_devices"] = True
    for h3_8_index in device_state_cells:
        flags = universe.setdefault(h3_8_index, {"has_devices": False, "has_points": False})
        flags["has_devices"] = True
    for h3_8_index in points_cells:
        flags = universe.setdefault(h3_8_index, {"has_devices": False, "has_points": False})
        flags["has_points"] = True
    return universe


def _aggregate_window_points(
    rows: Iterable[tuple[int, int, int, datetime, str]],
) -> dict[int, list[_CellAccountTotal]]:
    """Sum points and find the earliest `created_at` per (cell, account),
    from raw ``(h3_8_index, account_id, points, created_at, status)`` ledger
    rows already narrowed to the window by the caller's SQL WHERE clause.

    Only ``status == 'confirmed'`` rows are counted — everything else
    (today: nothing; in the future, a 'pending_review' moderation row) is
    dropped here, in Python, so this rule is exercised by an ordinary
    fake-cursor unit test rather than only by a live Postgres run.

    Returns EVERY earner per cell (not just the top 3) — callers need the
    full set both to rank (`_rank_cell`) and to compute a cell's
    `total_points` / `distinct_earners`, which cover every earner, not only
    the stored top 3 (see the response-shape example in
    PLAN_RIDE_MODE_API.md Phase A4: `distinct_earners: 4` alongside a
    3-entry leader+runners_up list).
    """
    totals: dict[tuple[int, int], list[Any]] = {}
    for h3_8_index, account_id, points, created_at, status in rows:
        if status != _CONFIRMED_STATUS:
            continue
        key = (h3_8_index, account_id)
        if key not in totals:
            totals[key] = [0, created_at]
        else:
            if created_at < totals[key][1]:
                totals[key][1] = created_at
        totals[key][0] += points

    by_cell: dict[int, list[_CellAccountTotal]] = {}
    for (h3_8_index, account_id), (points, first_point_at) in totals.items():
        by_cell.setdefault(h3_8_index, []).append(
            _CellAccountTotal(h3_8_index, account_id, points, first_point_at)
        )
    return by_cell


def _rank_cell(entries: list[_CellAccountTotal]) -> list[_CellAccountTotal]:
    """Deterministic, total tie-break order for one cell's earners:
    points DESC, then first_point_at ASC ("whoever got there first holds
    the territory"), then account_id ASC as the final tiebreak.
    """
    return sorted(entries, key=lambda e: (-e.points, e.first_point_at, e.account_id))


# ---------------------------------------------------------------------------
# The job.
# ---------------------------------------------------------------------------
def recompute(window_days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Recompute the H3 r8 area leader report. Safe to call repeatedly —
    each call fully replaces `h3_r8_area_report` / `h3_r8_area_leaders`
    (DELETE-cascade then INSERT, in one transaction — the
    src/daily_trips.py:compute_for_date idiom) and appends one new row to
    the append-only `h3_r8_area_leader_runs` audit log.

    Returns a summary dict (mirrors src/daily_trips.py's
    compute_for_date/run_daily convention — src/cli.py logs whatever each
    command returns).
    """
    run_at = datetime.now(timezone.utc)
    window_start = run_at - timedelta(days=window_days)
    window_end = run_at

    log.info(
        "area_leaders.recompute: window %s .. %s (window_days=%d)",
        window_start, window_end, window_days,
    )

    with connection() as conn:
        with conn.cursor() as cur:
            # ---- universe: three cheap DISTINCT scans, unioned in Python ----
            cur.execute("SELECT DISTINCT h3_8_index FROM device_history WHERE h3_8_index IS NOT NULL")
            device_history_cells = [r[0] for r in cur.fetchall()]

            cur.execute(
                "SELECT DISTINCT current_h3_8_index FROM device_state "
                "WHERE current_h3_8_index IS NOT NULL"
            )
            device_state_cells = [r[0] for r in cur.fetchall()]

            cur.execute(
                "SELECT DISTINCT h3_8_index FROM user_points WHERE status = %s",
                (_CONFIRMED_STATUS,),
            )
            points_cells = [r[0] for r in cur.fetchall()]

            universe = _build_universe(device_history_cells, device_state_cells, points_cells)

            # ---- windowed ledger rows -> per-(cell, account) totals ----
            cur.execute(
                """
                SELECT h3_8_index, account_id, points, created_at, status
                FROM user_points
                WHERE created_at >= %s AND created_at < %s
                """,
                (window_start, window_end),
            )
            by_cell = _aggregate_window_points(cur.fetchall())

            # ---- full replace: report + leaders ----
            cur.execute("DELETE FROM h3_r8_area_report")

            report_rows: list[tuple[int, bool, bool, int, int]] = []
            leader_rows: list[tuple[int, int, int, int, datetime]] = []
            led_cells = 0
            for h3_8_index, flags in universe.items():
                entries = by_cell.get(h3_8_index, [])
                total_points = sum(e.points for e in entries)
                distinct_earners = len(entries)
                report_rows.append((
                    h3_8_index, flags["has_devices"], flags["has_points"],
                    total_points, distinct_earners,
                ))
                if entries:
                    led_cells += 1
                    ranked = _rank_cell(entries)
                    for rank, entry in enumerate(ranked[:_MAX_LEADERS_PER_CELL], start=1):
                        leader_rows.append((
                            h3_8_index, rank, entry.account_id, entry.points, entry.first_point_at,
                        ))

            if report_rows:
                cur.executemany(
                    """
                    INSERT INTO h3_r8_area_report
                        (h3_8_index, has_devices, has_points, total_points, distinct_earners)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    report_rows,
                )
            if leader_rows:
                cur.executemany(
                    """
                    INSERT INTO h3_r8_area_leaders
                        (h3_8_index, rank, account_id, points, first_point_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    leader_rows,
                )

            cell_count = len(report_rows)
            cur.execute(
                """
                INSERT INTO h3_r8_area_leader_runs
                    (computed_at, window_start, window_end, cell_count, led_cells)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (run_at, window_start, window_end, cell_count, led_cells),
            )
            (run_id,) = cur.fetchone()
        conn.commit()

    log.info(
        "area_leaders.recompute: run_id=%s cells=%d led_cells=%d",
        run_id, cell_count, led_cells,
    )
    return {
        "run_id": run_id,
        "computed_at": run_at,
        "window_start": window_start,
        "window_end": window_end,
        "cell_count": cell_count,
        "led_cells": led_cells,
    }
