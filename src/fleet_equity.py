"""Rental outcomes inside vs outside Denver's official Equity Areas.

THE QUESTION. Do rentals UNLOCKED inside the city's official Equity Areas
(data/equity.geojson, EQ_001..EQ_030) end where they began more often than
rentals unlocked outside them? It is the number a DOTI equity micro-grant asks
for, which is why every caveat below travels in the response, not in a doc.

ATTRIBUTED AT WRITE TIME, BY POINT (sql/090 + sql/092). device_state's
counters are per vehicle and cumulative; splitting them by where a vehicle is
PARKED NOW would credit its past rentals to wherever it stands today.
rental_outcomes_hourly instead records each rental, at the moment of release,
against the Equity Area containing its UNLOCK POINT (`equity_area`). The point
is tested, not its r9 cell: the first version of this cut classified whole
hexagons, and on production origins that kept 1.5% of rentals "inside", threw
away about nine in ten rentals that really started in an Equity Area and let
one area be half the sample (PR #114 review). A point is inside or it is not;
nothing straddles.

WHAT IS LEFT OUT, AND SAID SO.
  * `unknown`: rentals with no unlock point to test (a vehicle first seen
    mid-rental, no fix, or an unreadable boundary file). They are written to
    rows of their own, so the exclusion is exact: `excluded.unknown_origin`.
  * `unrecorded`: rentals counted before sql/092, which recorded no area.
    They are outside the coverage window (`data_since`), not silently dropped.
  * any radius other than the ingest's current one: one ring per figure.

WHAT THE NUMBER IS, AND IS NOT.
  `ended_within_radius_rate`: the share of rentals whose DROP point was within
  `radius_meters` of the unlock point. A loop ride back to the same rack
  counts, and loop rides may be more common in some places (parks,
  residential streets) than others, which alone can move this figure. So the
  response also carries `never_left_radius_rate`: the share that never got
  farther than the radius at any point (over rentals whose maximum is known).
  Neither says why: the vehicle, the app, the weather and a rider changing
  their mind all look the same here.

UNCERTAINTY. Each rate carries a 95% Wilson interval, and the difference a
Newcombe (hybrid Wilson) interval. Those assume independent rentals; rentals
cluster by vehicle and place, so the true uncertainty is wider and the
response says so. Under MIN_RENTALS_FOR_RATE a side keeps its counts and loses
its rate, and the difference is computed only when both sides clear it.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import load
from .fleet_outcomes import MIN_RENTALS_FOR_RATE
from .pg import connection

log = logging.getLogger(__name__)

#: The windows the endpoint serves, in days.
WINDOWS = {"7d": 7, "28d": 28}

#: equity_area values that are not an Equity Area (sql/092).
OUTSIDE, UNKNOWN, UNRECORDED = "outside", "unknown", "unrecorded"

#: How many official Equity Areas exist (data/equity.geojson).
OFFICIAL_AREA_COUNT = 30

_Z = 1.959964  # two-sided 95%

CAVEATS = (
    "Counts rentals that ended within the radius of where they were unlocked; "
    "a ride that looped back to the same spot counts too (see "
    "never_left_radius_rate for rentals that never left it). It does not say "
    "why. Intervals assume independent rentals; rentals cluster by vehicle and "
    "place, so the real uncertainty is wider. A release with no movement "
    "(including some reservations the feed cannot tell apart) is counted as a rental."
)


def wilson(k: int, n: int) -> tuple[float, float] | None:
    """95% Wilson score interval for k of n, or None when n is 0."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + _Z * _Z / n
    centre = (p + _Z * _Z / (2 * n)) / denom
    half = _Z * math.sqrt(p * (1 - p) / n + _Z * _Z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def newcombe(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float] | None:
    """95% interval for p1 - p2 (Newcombe's hybrid score method, #10)."""
    a, b = wilson(k1, n1), wilson(k2, n2)
    if a is None or b is None:
        return None
    p1, p2 = k1 / n1, k2 / n2
    d = p1 - p2
    lo = d - math.sqrt((p1 - a[0]) ** 2 + (b[1] - p2) ** 2)
    hi = d + math.sqrt((a[1] - p1) ** 2 + (p2 - b[0]) ** 2)
    return lo, hi


def _side(acc: dict[str, int]) -> dict[str, Any]:
    n, k = acc["rentals"], acc["no_gos"]
    mk, km = acc["max_known"], acc["no_gos_max"]
    rated = n >= MIN_RENTALS_FOR_RATE
    rated_max = mk >= MIN_RENTALS_FOR_RATE
    ci = wilson(k, n) if rated else None
    ci_max = wilson(km, mk) if rated_max else None
    return {
        "rentals": n,
        "ended_within_radius": k,
        "ended_within_radius_rate": round(k / n, 4) if rated else None,
        "ended_within_radius_ci95": [round(ci[0], 4), round(ci[1], 4)] if ci else None,
        "max_known": mk,
        "never_left_radius": km,
        "never_left_radius_rate": round(km / mk, 4) if rated_max else None,
        "never_left_radius_ci95": [round(ci_max[0], 4), round(ci_max[1], 4)] if ci_max else None,
    }


def summarize_areas(rows: list[tuple[str, int, int, int, int]]) -> dict[str, Any]:
    """rows: (equity_area, rentals, no_gos, no_gos_max, max_known), summed per
    area over the window. The arithmetic, testable without a database."""
    zero = lambda: {"rentals": 0, "no_gos": 0, "no_gos_max": 0, "max_known": 0}  # noqa: E731
    inside, outside = zero(), zero()
    excluded = {"unknown_origin": 0, "unrecorded": 0}
    by_area: dict[str, dict[str, int]] = {}
    for area, rentals, no_gos, no_gos_max, max_known in rows:
        if area == UNKNOWN:
            excluded["unknown_origin"] += rentals
            continue
        if area == UNRECORDED:
            excluded["unrecorded"] += rentals
            continue
        accs = [outside] if area == OUTSIDE else [inside, by_area.setdefault(area, zero())]
        for acc in accs:
            acc["rentals"] += rentals
            acc["no_gos"] += no_gos
            acc["no_gos_max"] += no_gos_max
            acc["max_known"] += max_known

    both = inside["rentals"] >= MIN_RENTALS_FOR_RATE and outside["rentals"] >= MIN_RENTALS_FOR_RATE
    diff = ci = None
    if both:
        # From raw counts, not from the rounded rates.
        p_in = inside["no_gos"] / inside["rentals"]
        p_out = outside["no_gos"] / outside["rentals"]
        diff = round((p_in - p_out) * 100, 2)
        lo_hi = newcombe(inside["no_gos"], inside["rentals"], outside["no_gos"], outside["rentals"])
        ci = [round(lo_hi[0] * 100, 2), round(lo_hi[1] * 100, 2)] if lo_hi else None

    return {
        "inside": {**_side(inside), "areas_represented": len(by_area),
                   "areas_official": OFFICIAL_AREA_COUNT},
        "outside": _side(outside),
        # Percentage points, inside minus outside; only when BOTH clear the floor.
        "difference_points": diff,
        "difference_points_ci95": ci,
        # An interval that spans zero is not a difference this data can show.
        "difference_distinguishable": (ci[0] > 0 or ci[1] < 0) if ci else None,
        "by_area": [
            {"area": a, **_side(v)} for a, v in sorted(by_area.items())
        ],
        "excluded": excluded,
    }


_SQL = """
SELECT equity_area, SUM(rentals), SUM(no_gos), SUM(no_gos_max), SUM(max_known)
FROM rental_outcomes_hourly
WHERE hour >= %s AND hour < %s AND radius_m = %s
GROUP BY equity_area
"""

# The first hour the table recorded areas at this radius (sql/092 onward).
_SQL_SINCE = """
SELECT MIN(hour) FROM rental_outcomes_hourly
WHERE radius_m = %s AND equity_area <> 'unrecorded'
"""


def summarize(window: str = "7d", *, now: datetime | None = None) -> dict[str, Any]:
    """The inside/outside comparison over a trailing window. Never raises: a
    failure is `status: "unavailable"` with empty figures, so it can never be
    mistaken for a quiet week."""
    days = WINDOWS[window]
    end = (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    # NUMERIC(6,2) in the table: compare at the same precision.
    radius = round(float(load().device_tracking.stationary_threshold_meters), 2)
    status = "ok"
    since: datetime | None = None
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_SQL, (start, end, radius))
                rows = [(str(r[0]), int(r[1]), int(r[2]), int(r[3]), int(r[4]))
                        for r in cur.fetchall()]
                cur.execute(_SQL_SINCE, (radius,))
                got = cur.fetchone()
                since = got[0] if got and got[0] else None
        out = summarize_areas(rows)
    except Exception:  # noqa: BLE001
        log.exception("equity outcomes summary failed")
        status = "unavailable"
        out = summarize_areas([])

    # Coverage: how much of the named window the table actually holds. A
    # "28-day" figure over two days of data must not be quoted as 28 days.
    covered_from = max(start, since) if since else None
    hours_covered = (
        max(0, int((end - covered_from).total_seconds() // 3600)) if covered_from else 0
    )
    out.update({
        "status": status,
        "window": window,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "data_since": since.isoformat() if since else None,
        "hours_covered": hours_covered,
        "hours_in_window": days * 24,
        "window_complete": hours_covered >= days * 24,
        "radius_meters": radius,
        "min_rentals_for_rate": MIN_RENTALS_FOR_RATE,
        "definition": "end_displacement",
        "attribution": "unlock_point",
        "boundary": "official_equity_areas",
        "caveats": CAVEATS,
    })
    return out
