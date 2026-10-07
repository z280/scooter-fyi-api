"""Rental outcomes inside vs outside Denver's Equity Areas.

THE QUESTION. Do rentals started inside the city's official Equity Areas end
where they began more often than rentals started outside them? It is the
number a DOTI equity micro-grant asks for, and the frontend plan calls the
comparison "the single most important sentence this app can produce" — which
is exactly why it is built on the rollup and nothing weaker.

WHY ONLY THE ROLLUP CAN ANSWER IT (sql/090). device_state's counters are
per vehicle and cumulative; splitting them by where a vehicle is PARKED NOW
would credit a vehicle's 400 rentals across the city to whichever side of a
boundary it happens to be standing on today. rental_outcomes_hourly instead
records each rental at write time against the r9 cell where it was UNLOCKED,
so attribution happens when the rental happens. Built on the rollup this is a
query; built on device_state it would be a claim that does not survive being
checked.

HOW A CELL IS ASSIGNED, AND THE BOUNDARY RULE. A rollup row knows an r9 cell,
not a point. A cell counts as INSIDE only when its whole hexagon (centre and
all six vertices) is inside an Equity Area, and OUTSIDE only when all seven
points are outside every Equity Area. A cell that straddles a boundary is
counted in neither: it is reported as `boundary_excluded`, because assigning
it to a side would draw a line more precisely than the data can. (House rule:
a boundary we cannot resolve is not drawn as though we could.)

WHAT ELSE IS LEFT OUT, AND SAID SO.
  * rows carrying origin_unknown rentals (a vehicle first seen mid-rental, so
    its cell is where it was first sighted, not where it was unlocked) —
    excluded whole and counted in `origin_unknown_excluded`;
  * any radius other than the ingest's current one — one ring per figure.

WHAT THE NUMBER IS. The share of rentals that ENDED within `radius_meters`
of where they were unlocked (end displacement, the same definition as
device_state.rentals_no_go and /api/v1/fleet/outcomes). A loop ride back to
the same rack counts. Not a cause: the vehicle, the app, the weather or a
rider changing their mind all look the same here. Every figure travels with
its window, its sample and its radius, and a side under the floor gets
counts and no rate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Callable

import h3

from .config import load
from .fleet_outcomes import MIN_RENTALS_FOR_RATE
from .geo import region_for_point
from .pg import connection

log = logging.getLogger(__name__)

#: The windows the endpoint serves, in days.
WINDOWS = {"7d": 7, "28d": 28}

INSIDE, OUTSIDE, BOUNDARY = "inside", "outside", "boundary"


def _in_equity_area(lon: float, lat: float) -> bool:
    return region_for_point("equity", lon, lat) is not None


@lru_cache(maxsize=65_536)
def classify_cell(cell: int, contains: Callable[[float, float], bool] = _in_equity_area) -> str:
    """INSIDE / OUTSIDE / BOUNDARY for an r9 cell stored as a BIGINT.

    Centre plus the six vertices: all inside -> INSIDE; none inside ->
    OUTSIDE; anything else -> BOUNDARY (straddles a line).
    """
    hexid = h3.int_to_str(int(cell))
    points = [h3.cell_to_latlng(hexid), *h3.cell_to_boundary(hexid)]
    hits = sum(1 for lat, lon in points if contains(lon, lat))
    if hits == len(points):
        return INSIDE
    if hits == 0:
        return OUTSIDE
    return BOUNDARY


def _rate(no_gos: int, rentals: int) -> float | None:
    """Share, or None under the floor. Never divides by zero."""
    if rentals < MIN_RENTALS_FOR_RATE or rentals <= 0:
        return None
    return round(no_gos / rentals, 4)


def summarize_cells(
    rows: list[tuple[int, int, int, int]],
    *,
    classify: Callable[[int], str] | None = None,
) -> dict[str, Any]:
    """rows: (h3_9, rentals, no_gos, origin_unknown) summed per cell.

    The arithmetic, split out so it is testable without a database."""
    classify = classify or classify_cell
    sides = {s: {"rentals": 0, "no_gos": 0, "cells": 0} for s in (INSIDE, OUTSIDE, BOUNDARY)}
    origin_unknown_excluded = 0
    for cell, rentals, no_gos, origin_unknown in rows:
        if origin_unknown:
            origin_unknown_excluded += rentals
            continue
        side = sides[classify(cell)]
        side["rentals"] += rentals
        side["no_gos"] += no_gos
        side["cells"] += 1

    for s in (INSIDE, OUTSIDE):
        sides[s]["no_go_rate"] = _rate(sides[s]["no_gos"], sides[s]["rentals"])
    rin, rout = sides[INSIDE]["no_go_rate"], sides[OUTSIDE]["no_go_rate"]
    return {
        "inside": sides[INSIDE],
        "outside": sides[OUTSIDE],
        "boundary_excluded": {k: sides[BOUNDARY][k] for k in ("rentals", "no_gos", "cells")},
        "origin_unknown_excluded": origin_unknown_excluded,
        # Computed, not left to the reader's eye, and only when BOTH sides
        # clear the floor: percentage points, inside minus outside.
        "difference_points": (
            round((rin - rout) * 100, 2) if rin is not None and rout is not None else None
        ),
    }


_SQL = """
SELECT h3_9, SUM(rentals), SUM(no_gos), SUM(origin_unknown)
FROM rental_outcomes_hourly
WHERE hour >= %s AND hour < %s AND radius_m = %s
GROUP BY h3_9, (origin_unknown > 0)
"""


def summarize(window: str = "7d", *, now: datetime | None = None) -> dict[str, Any]:
    """The inside/outside comparison over a trailing window. Never raises:
    a failure is an empty comparison (zeros, null rates), told apart from a
    real one by the rentals counts."""
    days = WINDOWS[window]
    end = (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    radius = float(load().device_tracking.stationary_threshold_meters)
    rows: list[tuple[int, int, int, int]] = []
    first_hour: str | None = None
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_SQL, (start, end, radius))
                rows = [(int(r[0]), int(r[1]), int(r[2]), int(r[3])) for r in cur.fetchall()]
                cur.execute("SELECT MIN(hour) FROM rental_outcomes_hourly WHERE radius_m = %s", (radius,))
                got = cur.fetchone()
                first_hour = got[0].isoformat() if got and got[0] else None
        out = summarize_cells(rows)
    except Exception:  # noqa: BLE001
        # Database or boundary-layer failure: an empty comparison (zeros,
        # null rates), never a 500 on a public page.
        log.exception("equity outcomes summary failed")
        out = summarize_cells([])
    out.update({
        "window": window,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        # The rollup began on 2026-10-07; a window that reaches back past it
        # covers less than its name says, and the response says so.
        "data_since": first_hour,
        "radius_meters": radius,
        "min_rentals_for_rate": MIN_RENTALS_FOR_RATE,
        "definition": "end_displacement",
        "attribution": "unlock_point_r9_cell",
        "boundary": "official_equity_areas",
    })
    return out
