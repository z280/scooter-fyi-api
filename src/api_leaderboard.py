"""GET /api/v1/leaderboard/map — the rider-facing FEATURE_PLAN §11 H3 r8
area-leader choropleth feed.

Reads the daily area-leader report (the recompute lane's
``src/area_leaders.py:recompute``, table group ``h3_r8_area_report`` /
``h3_r8_area_leaders`` / ``h3_r8_area_leader_runs``, sql/048) — but
privacy is NOT baked into those stored rows. It is applied HERE, at read
time, by a live join against ``accounts``, so a rider's visibility choice
takes effect on their very next request instead of waiting for tomorrow's
09:15 recompute. A stored top-3 rank is skipped (and the next stored rank
falls through into its place) when:

    * ``show_in_leaderboards`` is false — the rider opted out outright.
    * ``show_public_username`` is false — the same "hide the name" rule
      ``GET /api/v1/devices/{vid}/photos`` already applies at read time:
      ``CASE WHEN show_public_username THEN public_username ELSE NULL END``
      (see src/api_device_photos.py). A leaderboard entry with a hidden
      name is exactly the case that rule exists to prevent.
    * ``display_name IS NULL`` — sql/025's never-backfilled-username edge
      case (an account created before the username machinery ran and not
      yet backfilled). sql/044's ``display_name`` generated column reads
      straight off ``username_adjective``/``username_emoji``, so a NULL
      there propagates to a NULL ``display_name``, and a nameless leader
      would render as a literal ``null`` on the choropleth.

``leader`` is the highest surviving stored rank (1..3); ``runners_up`` is
whatever eligible ranks remain (so ``leader`` + ``runners_up`` totals at
most 3, and can be fewer, including zero eligible entries at all — a
cell can report real ``total_points``/``distinct_earners`` from the
ledger while showing ``leader: null`` because every earner there opted
out).

``total_points``/``distinct_earners`` are NOT privacy-filtered: they are
aggregate counts with no identity attached (a number reveals nobody), and
sql/048's ``h3_r8_area_report`` stores them as report-level facts
independent of any single account.

Colors (``ruling_color``/``ruling_border_color``/``ruling_alpha``) are
also live-joined from ``accounts``. The pair is coherent by
``accounts_ruling_colors_coherent`` (sql/044) — both NULL or both set —
but ``ruling_alpha`` carries ``NOT NULL DEFAULT 0.60`` in the schema, so
an account with no claimed color pair still has a non-null alpha in its
row. This handler NULLs ``ruling_alpha`` in the payload whenever the
color pair is NULL; forwarding the column default would leak a
meaningless number as if it were a real fill opacity.

No ``royalty_title`` field: ``display_name`` already composes it
(sql/044's generated column: ``COALESCE(royalty_title || ' ', '') ||
username_adjective || username_emoji``) — shipping the title again would
be a second copy of the same fact that can only drift.

ETAG — deliberately NOT run-keyed. ``/api/v1/h3/aggregates`` can key its
weak ETag on the ingest cycle because that payload is a pure function of
the cycle (src/api_h3.py says so explicitly). This one is not: it is a
LIVE JOIN, so an account's ``show_in_leaderboards``/
``show_public_username``/colors/re-rolled name can all change the
rendered body between recomputes. An ETag keyed only on
``h3_r8_area_leader_runs.computed_at`` would happily answer
``If-None-Match`` with a 304 that resurrects an opted-out rider until the
next 09:15 run — exactly the leak read-time filtering exists to prevent.
So the weak ETag is keyed on BOTH ``computed_at`` AND a hash of the
rendered ``cells`` payload:
``W/"arealb:<computed_at epoch>:<sha256(cells)[:16]>"``. The hash is
taken over a CANONICAL serialization
(``json.dumps(cells, sort_keys=True, separators=(",", ":"))``) —
anything process-dependent (e.g. incidental dict/set iteration order)
would churn the tag across workers and silently defeat every 304. Both
components are load-bearing: the content hash catches an eligibility/
color/name change with ``computed_at`` unchanged, and the ``computed_at``
component catches a fresh run whose cells happen to render identically
(near-certain at launch volumes) — a cells-only tag would revalidate
clients onto a stale ``window_start``/``window_end``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import h3
from fastapi import APIRouter, HTTPException, Request, Response

from .api_public import _if_none_match_hit
from .pg import connection

router = APIRouter()

_CACHE_HEADER = "public, max-age=600"


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


def _build_cells(report_rows, accounts_by_id: dict[int, tuple]) -> dict[str, dict[str, Any]]:
    """`report_rows` are (h3_8_index, total_points, distinct_earners, rank,
    account_id, points, first_point_at) — the LEFT JOIN's rank/account_id/
    points/first_point_at are NULL for a report cell with no leader rows
    at all. Rows MUST already be ordered by (h3_8_index, rank ASC) — the
    handler's SQL does this; a fake cursor in tests must replicate the
    ordering, since NULL ranks (no leaders) sort however Postgres likes
    but are skipped here regardless of position.

    `accounts_by_id` maps account_id -> (display_name, show_in_leaderboards,
    show_public_username, ruling_color, ruling_border_color, ruling_alpha).
    """
    cells: dict[str, dict[str, Any]] = {}
    for h3_idx, total_points, distinct_earners, rank, account_id, points, _first_point_at in report_rows:
        key = h3.int_to_str(int(h3_idx))
        cell = cells.get(key)
        if cell is None:
            cell = cells[key] = {
                "total_points": int(total_points),
                "distinct_earners": int(distinct_earners),
                "_eligible": [],
            }
        if rank is None:
            continue
        acct = accounts_by_id.get(account_id)
        if acct is None:
            # Defensive: the FK guarantees the account exists, but a stale
            # fixture/race should fall through rather than 500.
            continue
        (display_name, show_in_leaderboards, show_public_username,
         ruling_color, ruling_border_color, ruling_alpha) = acct
        if not _is_eligible(display_name, show_in_leaderboards, show_public_username):
            continue
        cell["_eligible"].append(_leader_entry(
            display_name, points, ruling_color, ruling_border_color, ruling_alpha))

    result: dict[str, dict[str, Any]] = {}
    for key, cell in cells.items():
        eligible = cell["_eligible"]
        result[key] = {
            "total_points": cell["total_points"],
            "distinct_earners": cell["distinct_earners"],
            "leader": eligible[0] if eligible else None,
            "runners_up": eligible[1:],
        }
    return result


def _cells_digest(cells: dict[str, Any]) -> str:
    """sha256[:16] of a CANONICAL serialization — sort_keys so nested dict
    insertion order (or a differently-ordered SQL result) can never change
    the digest for identical data."""
    canonical = json.dumps(cells, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _etag_for(computed_at, cells: dict[str, Any]) -> str:
    return f'W/"arealb:{int(computed_at.timestamp())}:{_cells_digest(cells)}"'


@router.get("/api/v1/leaderboard/map")
def leaderboard_map(request: Request, response: Response) -> Any:
    """The choropleth + click-through detail feed in one fetch: full
    eligible top-3 per cell, not just the leader (the RIDE_MODE_OVERHAUL
    extension to §11.4's literal shape).

        const r = await fetch("/api/v1/leaderboard/map");
        const { cells } = await r.json();
    """
    with connection() as conn:
        with conn.cursor() as cur:
            # REVIEW FIX: the metadata read below and the cells read further
            # down are two separate statements. Under the default READ
            # COMMITTED isolation, a recompute committing between them could
            # make the two disagree — an old computed_at/ETag paired with new
            # cells, or vice versa — so a client's cache validator would stop
            # meaning anything. REPEATABLE READ (must be set before the first
            # statement in the transaction) gives every statement in this
            # read-only transaction ONE consistent snapshot.
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cur.execute(
                """
                SELECT computed_at, window_start, window_end
                FROM h3_r8_area_leader_runs
                ORDER BY computed_at DESC, id DESC
                LIMIT 1
                """
            )
            run = cur.fetchone()
            if not run:
                raise HTTPException(503, detail="no leaderboard computed yet")
            computed_at, window_start, window_end = run

            cur.execute(
                """
                SELECT r.h3_8_index, r.total_points, r.distinct_earners,
                       l.rank, l.account_id, l.points, l.first_point_at
                FROM h3_r8_area_report r
                LEFT JOIN h3_r8_area_leaders l ON l.h3_8_index = r.h3_8_index
                ORDER BY r.h3_8_index, l.rank
                """
            )
            report_rows = cur.fetchall()

            account_ids = sorted({row[4] for row in report_rows if row[4] is not None})
            accounts_by_id: dict[int, tuple] = {}
            if account_ids:
                cur.execute(
                    """
                    SELECT id, display_name, show_in_leaderboards, show_public_username,
                           ruling_color, ruling_border_color, ruling_alpha
                    FROM accounts
                    WHERE id = ANY(%s)
                    """,
                    (account_ids,),
                )
                accounts_by_id = {a[0]: tuple(a[1:]) for a in cur.fetchall()}

    cells = _build_cells(report_rows, accounts_by_id)
    etag = _etag_for(computed_at, cells)

    if _if_none_match_hit(request, etag):
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": _CACHE_HEADER},
        )
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = _CACHE_HEADER

    return {
        "computed_at": computed_at.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "cells": cells,
    }
