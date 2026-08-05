"""H3 r8 territory control: the nightly UNIVERSE refresh, plus the window
and ranking rules the read path shares with it.

This module used to compute the whole leaderboard on a schedule and store
it. It no longer does. sql/061 moved everything derived from the points
ledger to read time, because the two halves of the old job had wildly
different costs and only one of them ever had to be scheduled:

    UNIVERSE (still here, `refresh_universe`) -- "every r8 cell that has
    ever had an observed device OR points history", ALL-TIME. Needs
    ``SELECT DISTINCT h3_8_index FROM device_history`` over ~7.3M rows
    with no index on that column: a seq scan of a few seconds. That is the
    reason this job exists at all, and why it now runs WEEKLY rather than
    nightly -- the answer is all-time, so it barely moves, and a cell that
    only just saw its first device is already covered by the read path's
    own fallback (see src/api_leaderboard.py).

    LEADERS (gone from here; see src/api_leaderboard.py) -- the trailing
    `DEFAULT_WINDOW_DAYS` of ``user_points``, which sql/059's index serves
    directly. Storing these meant territory could not change until the
    next 09:15 run, so a rider could never watch themselves take a
    hexagon. They are ranked per request now, off the same single scan
    that already served the regional leaderboard.

UNIVERSE SOURCES, unioned in Python (`_build_universe`) rather than as a
fourth SQL UNION -- each query returns at most a few hundred small
integers, and the union-with-overlaps logic is directly unit-testable with
a fake cursor instead of only through a live Postgres run:

    SELECT DISTINCT h3_8_index         FROM device_history
    SELECT DISTINCT current_h3_8_index FROM device_state
    SELECT DISTINCT h3_8_index         FROM user_points WHERE status = 'confirmed'

What stays here besides the job is the shared vocabulary the read path
needs, so the two cannot drift apart:

    DEFAULT_WINDOW_DAYS   the trailing window both describe
    MAX_LEADERS_PER_CELL  per-cell podium depth
    MAX_REGIONAL_LEADERS  whole-database leaderboard depth
    _aggregate_window_points / _aggregate_regional_points / _rank_cell

TIE-BREAK (`_rank_cell`), deterministic and total: `points DESC`, then
`first_point_at ASC` ("whoever got there first holds the territory"), then
`account_id ASC` as the final tiebreak. The read path expresses that same
order in SQL; this stays the reference implementation, and a test holds
the two to each other.

Only `status = 'confirmed'` ledger rows ever count -- sql/028's own
docstring: nothing in this codebase's current era writes any other status,
but a future moderator-approval workflow might, and this report must not
count a row nobody has confirmed.

FULL-REPLACE, not accumulation -- mirrors src/daily_trips.py:compute_for_date.
One transaction: `DELETE FROM h3_r8_area_report` -> INSERT the fresh
universe -> INSERT one new `h3_r8_area_leader_runs` row. The runs table is
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

# Top 3 per cell, not just the winner. The read path drops an entry whose
# owner has opted out, so a winner-only model would blank a hexagon rather
# than fall through to the runner-up. Public because that read path is now
# the only thing that uses it.
MAX_LEADERS_PER_CELL = 3

# The regional (whole-database) dashboard's leaderboard depth — a real
# leaderboard length, not a 3-entry podium; see the module docstring. Was
# also sql/054's `regional_leaders.rank` CHECK bound until sql/061 dropped
# that table, so this constant is now the only place the depth is stated.
MAX_REGIONAL_LEADERS = 25


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

    Never reads `h3_8_index` — only points/first_point_at/account_id — so
    this same function also ranks the regional (whole-database) totals
    `_aggregate_regional_points` produces, where `h3_8_index` is a
    meaningless placeholder.
    """
    return sorted(entries, key=lambda e: (-e.points, e.first_point_at, e.account_id))


def _aggregate_regional_points(
    by_cell: dict[int, list[_CellAccountTotal]],
) -> list[_CellAccountTotal]:
    """Collapse the per-cell per-account totals `_aggregate_window_points`
    already computed down to ONE total per account across every cell —
    the entire-database regional dashboard. Same points-summed /
    earliest-first_point_at-kept semantics as `_aggregate_window_points`,
    just merged across cells instead of scoped to one; `by_cell` is
    already confirmed-only and window-scoped by that function, so nothing
    here re-filters status or time.

    `h3_8_index` on the returned entries is a meaningless placeholder (0)
    — this collapses the cell dimension away entirely, and `_rank_cell`
    never reads it.
    """
    totals: dict[int, list[Any]] = {}
    for entries in by_cell.values():
        for e in entries:
            if e.account_id not in totals:
                totals[e.account_id] = [0, e.first_point_at]
            elif e.first_point_at < totals[e.account_id][1]:
                totals[e.account_id][1] = e.first_point_at
            totals[e.account_id][0] += e.points
    return [
        _CellAccountTotal(h3_8_index=0, account_id=account_id, points=points, first_point_at=first_point_at)
        for account_id, (points, first_point_at) in totals.items()
    ]


# ---------------------------------------------------------------------------
# The job.
# ---------------------------------------------------------------------------
def refresh_universe(window_days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Refresh the r8 cell UNIVERSE. Safe to call repeatedly — each call
    fully replaces `h3_r8_area_report` and appends one row to the
    append-only `h3_r8_area_leader_runs` audit log.

    This is the whole job now. It reads no points and ranks nobody: the
    leaderboard is computed per request (src/api_leaderboard.py). What it
    produces is the cell list the map draws — crucially including the
    cells with NO points at all, which is the part a read-time query
    genuinely cannot know, because "a device has been seen here" is an
    all-time fact about `device_history` rather than a fact about the
    trailing window.

    `window_days` is accepted and reported only so the CLI summary says
    which window the cells it just refreshed will be read against. Nothing
    here filters on it.

    Returns a summary dict (mirrors src/daily_trips.py's
    compute_for_date/run_daily convention — src/cli.py logs whatever each
    command returns, and /admin/scheduler renders it).
    """
    run_at = datetime.now(timezone.utc)
    log.info("area_leaders.refresh_universe: starting (window_days=%d)", window_days)

    with connection() as conn:
        with conn.cursor() as cur:
            # ---- universe: three cheap DISTINCT scans, unioned in Python ----
            # The device_history scan is the expensive one, and the entire
            # reason this runs on a schedule instead of per request.
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

            # ---- full replace ----
            cur.execute("DELETE FROM h3_r8_area_report")
            report_rows = [
                (h3_8_index, flags["has_devices"], flags["has_points"])
                for h3_8_index, flags in universe.items()
            ]
            if report_rows:
                cur.executemany(
                    """
                    INSERT INTO h3_r8_area_report (h3_8_index, has_devices, has_points)
                    VALUES (%s, %s, %s)
                    """,
                    report_rows,
                )

            cell_count = len(report_rows)
            cur.execute(
                """
                INSERT INTO h3_r8_area_leader_runs (computed_at, cell_count)
                VALUES (%s, %s)
                RETURNING id
                """,
                (run_at, cell_count),
            )
            (run_id,) = cur.fetchone()
        conn.commit()

    log.info("area_leaders.refresh_universe: run_id=%s cells=%d", run_id, cell_count)
    return {
        "run_id": run_id,
        "computed_at": run_at,
        "cell_count": cell_count,
        "window_days": window_days,
    }
