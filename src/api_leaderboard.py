"""Rider-facing territory control, computed at READ time.

GET /api/v1/leaderboard/map       — per H3 r8 cell: who holds it, plus totals.
GET /api/v1/leaderboard/regional  — the same window, ranked across the whole
                                    database rather than split by cell.

Both used to be served from tables a nightly job wrote (sql/048's
``h3_r8_area_leaders``, sql/054's ``regional_leaders``). sql/061 dropped
those tables and moved the work here. The reason is the one riders feel:
territory could not change until the next 09:15 run, so nobody could ever
watch themselves take a hexagon. Everything these endpoints report is a
fact about the trailing `DEFAULT_WINDOW_DAYS` of ``user_points`` — a
window is a read-time idea, and pinning it to a nightly run was what made
it stale.

It is also cheap, which is why it was worth doing. Both endpoints are one
indexed range scan of the ledger (sql/061's
``idx_user_points_confirmed_created``, partial on ``status='confirmed'``,
carrying ``points`` and ``h3_8_index`` so neither read has to visit the
heap). They differ only in how far they group: the map by
``(h3_8_index, account_id)``, the regional board by ``account_id`` alone.
That is exactly the relationship the old nightly job had internally —
``_aggregate_regional_points(by_cell)`` — now expressed as two endpoints
so a client can ask for one without paying for the other.

THE UNIVERSE is the one thing still precomputed, and the one thing a
read-time query genuinely cannot derive: ``h3_r8_area_report`` lists every
r8 cell that has ever had an observed device, ALL-TIME. Those are the
unclaimed cells the map draws as bare outlines, and "a device has been
seen here" is not a fact about the trailing window. src/area_leaders.py
refreshes it weekly. This endpoint UNIONS it with the cells that have
points in the window, so:

    * a cell that earned its first point since the last refresh still
      renders, rather than waiting up to a week to appear; and
    * there is no 503. The stored endpoints used to fail outright before
      the first recompute; this one answers from the ledger alone on a
      database whose universe has never been refreshed.

PRIVACY is applied here, at read time, by a live join against
``accounts`` — unchanged in rule, and now unchanged in freshness too,
since there is no stored copy left to lag behind it. A stored rank is
skipped (and the next eligible earner falls through into its place) when:

    * ``show_in_leaderboards`` is false — the rider opted out outright.
    * ``show_public_username`` is false — the same "hide the name" rule
      ``GET /api/v1/devices/{vid}/photos`` already applies at read time.
    * ``display_name IS NULL`` — sql/025's never-backfilled-username edge
      case; a nameless leader would render as a literal ``null``.

``total_points``/``distinct_earners`` are NOT privacy-filtered: they are
aggregate counts with no identity attached (a number reveals nobody), and
they count EVERY earner in the cell, not only the eligible top 3.

Colors are live-joined from the same row. The pair is coherent by
``accounts_ruling_colors_coherent`` (sql/044) — both NULL or both set —
but ``ruling_alpha`` carries ``NOT NULL DEFAULT 0.60``, so an account with
no claimed pair still has a non-null alpha in its row. This handler NULLs
``ruling_alpha`` whenever the color pair is NULL; forwarding the column
default would leak a meaningless number as if it were a real fill opacity.
(The frontend ignores the field entirely and paints every claimed hexagon
at one constant opacity — but that is its decision, not a licence for this
layer to send nonsense.)

No ``royalty_title`` field: ``display_name`` already composes it
(sql/044's generated column, restyled by sql/060: the title, a space,
then the capitalized adjective, a space, and the emoji — "Duke Swift
🦦") — shipping the title again would be a second copy of the same fact
that can only drift.

ETAGS are content-only — ``W/"arealb:<sha256(payload)[:16]>"`` — where the
stored endpoints keyed on the run's ``computed_at`` plus a content hash.
There is no run behind these payloads any more, and ``computed_at`` is now
simply "when you asked", which moves every request: keying on it would
make every tag unique and defeat the 304 entirely. The hash is taken over
a CANONICAL serialization (``sort_keys=True``) so nothing
process-dependent can churn the tag across workers.

``Cache-Control: public, max-age=30`` on both, down from 600. That is the
freshness the whole change exists to buy, and it bounds how stale a hit
can be; the frontend already re-polls the map every 90 s, so this is what
finally makes that poll mean something.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import h3
from fastapi import APIRouter, Request, Response

from .api_public import _if_none_match_hit
from .area_leaders import (
    DEFAULT_WINDOW_DAYS,
    MAX_LEADERS_PER_CELL,
    MAX_REGIONAL_LEADERS,
)
from .pg import connection

router = APIRouter()

# Short enough that "live" is not a lie, long enough that a burst of opens
# shares one aggregate. A rider who just earned points sees them within the
# minute.
_CACHE_HEADER = "public, max-age=30"

# The tie-break, in SQL. This is `area_leaders._rank_cell`'s rule spelled
# out — points first, then whoever got there first holds the territory, then
# account id to make it total. Kept as one string so the two endpoints below
# cannot drift from each other, and tested against the Python reference
# implementation so neither can drift from the recompute lane's definition.
_TIE_BREAK = "points DESC, first_point_at ASC, account_id ASC"


def _window_start(now: datetime) -> datetime:
    return now - timedelta(days=DEFAULT_WINDOW_DAYS)


def _is_eligible(display_name: str | None, show_in_leaderboards: bool, show_public_username: bool) -> bool:
    return bool(show_in_leaderboards) and bool(show_public_username) and display_name is not None


def _leader_entry(
    display_name: str,
    points: int,
    ruling_color: str | None,
    ruling_border_color: str | None,
    ruling_alpha,
) -> dict[str, Any]:
    """The unclaimed-pair rule: ruling_color/ruling_border_color are
    both-or-neither (accounts_ruling_colors_coherent), but ruling_alpha
    carries a NOT NULL DEFAULT — so when the pair is NULL, alpha is
    explicitly nulled here too rather than forwarding the column default."""
    if ruling_color is None:
        ruling_border_color = None
        alpha_out = None
    else:
        alpha_out = float(ruling_alpha) if ruling_alpha is not None else None
    return {
        "display_name": display_name,
        "points": int(points),
        "ruling_color": ruling_color,
        "ruling_border_color": ruling_border_color,
        "ruling_alpha": alpha_out,
    }


def _fetch_accounts(cur, account_ids: list[int]) -> dict[int, tuple]:
    """account_id -> (display_name, show_in_leaderboards, show_public_username,
    ruling_color, ruling_border_color, ruling_alpha). One query for the whole
    payload's cast, not one per entry."""
    if not account_ids:
        return {}
    cur.execute(
        """
        SELECT id, display_name, show_in_leaderboards, show_public_username,
               ruling_color, ruling_border_color, ruling_alpha
        FROM accounts
        WHERE id = ANY(%s)
        """,
        (sorted(set(account_ids)),),
    )
    return {a[0]: tuple(a[1:]) for a in cur.fetchall()}


def _eligible_entry(account_id: int, points: int, accounts_by_id: dict[int, tuple]) -> dict[str, Any] | None:
    """One ledger total -> a payload entry, or None when the account is
    ineligible (or, defensively, missing: the FK guarantees it exists, but a
    race should fall through rather than 500)."""
    acct = accounts_by_id.get(account_id)
    if acct is None:
        return None
    (display_name, show_in_leaderboards, show_public_username,
     ruling_color, ruling_border_color, ruling_alpha) = acct
    if not _is_eligible(display_name, show_in_leaderboards, show_public_username):
        return None
    return _leader_entry(display_name, points, ruling_color, ruling_border_color, ruling_alpha)


def _build_cells(
    totals_rows,
    universe_cells,
    accounts_by_id: dict[int, tuple],
) -> dict[str, dict[str, Any]]:
    """`totals_rows` are (h3_8_index, account_id, points, first_point_at),
    already grouped per (cell, account) and ordered by
    (h3_8_index, <tie-break>) — the handler's SQL does this, and a fake
    cursor in tests must replicate the ordering, since the first eligible
    row per cell is taken as its leader.

    `universe_cells` are the all-time cell ids from `h3_r8_area_report`.
    They are UNIONed with whatever appears in `totals_rows`, so a cell that
    earned its first point since the last universe refresh still renders,
    and a database with no refresh at all still returns the claimed cells.
    """
    cells: dict[str, dict[str, Any]] = {}

    def cell_for(h3_idx: int) -> dict[str, Any]:
        key = h3.int_to_str(int(h3_idx))
        cell = cells.get(key)
        if cell is None:
            cell = cells[key] = {"total_points": 0, "distinct_earners": 0, "_eligible": []}
        return cell

    for h3_idx in universe_cells:
        cell_for(h3_idx)

    for h3_idx, account_id, points, _first_point_at in totals_rows:
        cell = cell_for(h3_idx)
        # Totals count EVERY earner, eligible or not — they carry no
        # identity, and hiding a rider must not silently shrink a cell's
        # reported activity.
        cell["total_points"] += int(points)
        cell["distinct_earners"] += 1
        if len(cell["_eligible"]) >= MAX_LEADERS_PER_CELL:
            continue
        entry = _eligible_entry(account_id, points, accounts_by_id)
        if entry is not None:
            cell["_eligible"].append(entry)

    return {
        key: {
            "total_points": cell["total_points"],
            "distinct_earners": cell["distinct_earners"],
            "leader": cell["_eligible"][0] if cell["_eligible"] else None,
            "runners_up": cell["_eligible"][1:],
        }
        for key, cell in cells.items()
    }


def _build_regional_leaders(
    totals_rows, accounts_by_id: dict[int, tuple], limit: int = MAX_REGIONAL_LEADERS,
) -> list[dict[str, Any]]:
    """`totals_rows` are (account_id, points, first_point_at), already
    ordered by the tie-break. An ineligible earner is DROPPED, and the
    survivors are renumbered to a contiguous `rank` from 1 — so `rank` is
    display position, not a stored position with holes in it.

    `limit` is applied to ELIGIBLE entries, deliberately not in SQL: the
    filtering happens after the aggregate, so a pre-truncated set would
    return fewer than the published depth whenever a top earner had opted
    out.
    """
    out: list[dict[str, Any]] = []
    for account_id, points, _first_point_at in totals_rows:
        if len(out) >= limit:
            break
        entry = _eligible_entry(account_id, points, accounts_by_id)
        if entry is None:
            continue
        entry["rank"] = len(out) + 1
        out.append(entry)
    return out


def _digest(payload: Any) -> str:
    """sha256[:16] of a CANONICAL serialization — sort_keys so nested dict
    insertion order (or a differently-ordered SQL result) can never change
    the digest for identical data."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _respond(request: Request, response: Response, payload: dict[str, Any], body_key: str) -> Any:
    """Shared ETag/304 + cache-header tail. The tag is keyed on the
    payload's substance only — never on `computed_at`, which is "now" and
    would make every tag unique."""
    etag = f'W/"arealb:{_digest(payload[body_key])}"'
    if _if_none_match_hit(request, etag):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": _CACHE_HEADER})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = _CACHE_HEADER
    return payload


@router.get("/api/v1/leaderboard/map")
def leaderboard_map(request: Request, response: Response) -> Any:
    """The choropleth plus every cell's click-through detail in one fetch:
    the full eligible top-`MAX_LEADERS_PER_CELL` per cell, not just the
    leader, so a client never needs a second request to show a cell's
    runners-up.

        const r = await fetch("/api/v1/leaderboard/map");
        const { cells } = await r.json();
    """
    now = datetime.now(timezone.utc)
    window_start = _window_start(now)

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT h3_8_index FROM h3_r8_area_report")
            universe_cells = [r[0] for r in cur.fetchall()]

            cur.execute(
                f"""
                SELECT h3_8_index, account_id,
                       SUM(points) AS points, MIN(created_at) AS first_point_at
                FROM user_points
                WHERE status = 'confirmed' AND created_at >= %s
                GROUP BY h3_8_index, account_id
                ORDER BY h3_8_index, {_TIE_BREAK}
                """,
                (window_start,),
            )
            totals_rows = cur.fetchall()
            accounts_by_id = _fetch_accounts(cur, [r[1] for r in totals_rows])

    payload = {
        "computed_at": now.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        "cells": _build_cells(totals_rows, universe_cells, accounts_by_id),
    }
    return _respond(request, response, payload, "cells")


@router.get("/api/v1/leaderboard/regional")
def leaderboard_regional(request: Request, response: Response) -> Any:
    """The whole-database companion to the map: the same window and the
    same tie-break, ranked across every cell at once rather than split by
    one. Top `MAX_REGIONAL_LEADERS` eligible accounts.

        const r = await fetch("/api/v1/leaderboard/regional");
        const { leaders } = await r.json();
    """
    now = datetime.now(timezone.utc)
    window_start = _window_start(now)

    with connection() as conn:
        with conn.cursor() as cur:
            # Deliberately NOT capped in SQL — see `_build_regional_leaders`.
            # It groups by account, so it returns one row per account with
            # confirmed points in the window: bounded by active riders, not
            # by ledger size.
            cur.execute(
                f"""
                SELECT account_id, SUM(points) AS points, MIN(created_at) AS first_point_at
                FROM user_points
                WHERE status = 'confirmed' AND created_at >= %s
                GROUP BY account_id
                ORDER BY {_TIE_BREAK}
                """,
                (window_start,),
            )
            totals_rows = cur.fetchall()
            accounts_by_id = _fetch_accounts(cur, [r[0] for r in totals_rows])

    payload = {
        "computed_at": now.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        "leaders": _build_regional_leaders(totals_rows, accounts_by_id),
    }
    return _respond(request, response, payload, "leaders")
