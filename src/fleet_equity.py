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

  `stayed_rate` (sql/098): the share that never left the spot, i.e. never got
  more than 50 m (`stayed_radius_meters`) from the unlock point and was
  released there, over `stayed_known` (rentals written since sql/098, which
  recorded it). Rows from before `stayed_counted_since` hold 0/0, meaning
  "not recorded", so a window that opens before it is reported over the
  recorded part only (`stayed_hours_covered`).

UNCERTAINTY. Rentals are not independent draws: they cluster by place (and
by vehicle). Each rate's 95% interval is therefore CLUSTER-ROBUST, treating
each unlock-point r9 cell as a cluster (the linearised variance of a ratio
estimator over cells), and the difference's interval adds the two sides'
variances. `difference_distinguishable` is true only when that interval
excludes zero AND both sides span at least MIN_CLUSTERS cells; with fewer
cells there is no honest interval and the flag is null. Vehicle-level
clustering is not captured (the table holds no vehicle), so even these
intervals are a lower bound on the uncertainty, and the caveats say so. Under
MIN_RENTALS_FOR_RATE a side keeps its counts and loses its rate.

ONLY DENVER. 'outside' means in Denver (a council district) and in no Equity
Area; unlock points outside the City and County ('outside_city') are not
part of a city comparison and are excluded and reported.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import load
from .fleet_outcomes import (
    MIN_RENTALS_FOR_RATE, STAYED_COUNTED_SINCE_MIGRATION, STAYED_DEFINITION,
    STAYED_RADIUS_METERS,
)
from .pg import connection

log = logging.getLogger(__name__)

#: The windows the endpoint serves, in days.
WINDOWS = {"7d": 7, "28d": 28}

#: equity_area values that are not an Equity Area (sql/092).
OUTSIDE, OUTSIDE_CITY, UNKNOWN, UNRECORDED = "outside", "outside_city", "unknown", "unrecorded"

#: Fewest cells a side needs before a cluster-robust interval means anything.
MIN_CLUSTERS = 30

#: Above this share of unknown origins the comparison is reported "degraded":
#: a broken boundary file would otherwise push everything into "unknown"
#: while the status still said ok.
DEGRADED_UNKNOWN_SHARE = 0.2

_Z = 1.959964  # two-sided 95%

CAVEATS = (
    "Counts rentals that ended within the radius of where they were unlocked; "
    "a ride that looped back to the same spot counts too (see "
    "never_left_radius_rate for rentals that never left it). It does not say "
    "why. Intervals treat each map cell as a cluster but cannot see vehicles, "
    "so the real uncertainty is wider still. Only rentals unlocked in Denver "
    "are compared. A release with no movement (including some reservations "
    "the feed cannot tell apart) is counted as a rental."
)


def official_area_count() -> int | None:
    """How many Equity Areas the boundary file holds; None if unreadable."""
    try:
        from .geo import region_names
        return len(region_names("equity"))
    except Exception:  # noqa: BLE001
        return None


def cluster_ratio(clusters: list[tuple[int, int]]) -> tuple[float, float] | None:
    """(p, variance) of p = sum(k) / sum(n) with each (k, n) a cluster, by
    linearisation: m/(m-1) * sum((k - p n)^2) / N^2. None under MIN_CLUSTERS
    clusters or with no rentals."""
    clusters = [(k, n) for k, n in clusters if n > 0]
    m, total = len(clusters), sum(n for _, n in clusters)
    if m < MIN_CLUSTERS or total <= 0:
        return None
    p = sum(k for k, _ in clusters) / total
    var = m / (m - 1) * sum((k - p * n) ** 2 for k, n in clusters) / (total * total)
    return p, var


def _ci(est: tuple[float, float] | None) -> list[float] | None:
    if est is None:
        return None
    p, var = est
    half = _Z * math.sqrt(var)
    return [round(max(0.0, p - half), 4), round(min(1.0, p + half), 4)]


def _zero() -> dict[str, Any]:
    return {"rentals": 0, "no_gos": 0, "no_gos_max": 0, "max_known": 0,
            "stayed": 0, "stayed_known": 0, "cells": {}}


def _add(acc: dict[str, Any], cell: int, rentals: int, no_gos: int, no_gos_max: int,
         max_known: int, stayed: int = 0, stayed_known: int = 0) -> None:
    acc["rentals"] += rentals
    acc["no_gos"] += no_gos
    acc["no_gos_max"] += no_gos_max
    acc["max_known"] += max_known
    acc["stayed"] += stayed
    acc["stayed_known"] += stayed_known
    c = acc["cells"].setdefault(cell, [0, 0, 0, 0, 0, 0])
    c[0] += rentals
    c[1] += no_gos
    c[2] += no_gos_max
    c[3] += max_known
    c[4] += stayed
    c[5] += stayed_known


def _side(acc: dict[str, Any]) -> dict[str, Any]:
    n, k = acc["rentals"], acc["no_gos"]
    mk, km = acc["max_known"], acc["no_gos_max"]
    cells = acc["cells"].values()
    rated = n >= MIN_RENTALS_FOR_RATE
    rated_max = mk >= MIN_RENTALS_FOR_RATE
    sk, st = acc["stayed_known"], acc["stayed"]
    rated_stayed = sk >= MIN_RENTALS_FOR_RATE
    return {
        "rentals": n,
        "cells": len(acc["cells"]),
        "ended_within_radius": k,
        "ended_within_radius_rate": round(k / n, 4) if rated else None,
        "ended_within_radius_ci95": _ci(cluster_ratio([(c[1], c[0]) for c in cells])) if rated else None,
        "max_known": mk,
        "never_left_radius": km,
        "never_left_radius_rate": round(km / mk, 4) if rated_max else None,
        "never_left_radius_ci95": _ci(cluster_ratio([(c[2], c[3]) for c in cells])) if rated_max else None,
        # sql/098: never left the spot (50 m), over the rentals that recorded it.
        "stayed_known": sk,
        "stayed": st,
        "stayed_rate": round(st / sk, 4) if rated_stayed else None,
        "stayed_ci95": _ci(cluster_ratio([(c[4], c[5]) for c in cells])) if rated_stayed else None,
    }


def summarize_areas(rows: list[tuple]) -> dict[str, Any]:
    """rows: (equity_area, h3_9, rentals, no_gos, no_gos_max, max_known
    [, stayed, stayed_known]), summed per (area, cell) over the window; the
    two sql/098 counts default to 0 (not recorded). Pure: testable without a
    database."""
    inside, outside = _zero(), _zero()
    excluded = {"unknown_origin": 0, "outside_city": 0, "unrecorded": 0}
    by_area: dict[str, dict[str, Any]] = {}
    for area, cell, rentals, no_gos, no_gos_max, max_known, *more in rows:
        counts = (rentals, no_gos, no_gos_max, max_known, *(tuple(more) + (0, 0))[:2])
        if area == UNKNOWN:
            excluded["unknown_origin"] += rentals
        elif area == OUTSIDE_CITY:
            excluded["outside_city"] += rentals
        elif area == UNRECORDED:
            excluded["unrecorded"] += rentals
        elif area == OUTSIDE:
            _add(outside, cell, *counts)
        else:
            _add(inside, cell, *counts)
            _add(by_area.setdefault(area, _zero()), cell, *counts)

    both = inside["rentals"] >= MIN_RENTALS_FOR_RATE and outside["rentals"] >= MIN_RENTALS_FOR_RATE
    diff = ci = distinguishable = None
    if both:
        # From raw counts, not from the rounded rates.
        p_in = inside["no_gos"] / inside["rentals"]
        p_out = outside["no_gos"] / outside["rentals"]
        diff = round((p_in - p_out) * 100, 2)
        e_in = cluster_ratio([(c[1], c[0]) for c in inside["cells"].values()])
        e_out = cluster_ratio([(c[1], c[0]) for c in outside["cells"].values()])
        if e_in is not None and e_out is not None:
            half = _Z * math.sqrt(e_in[1] + e_out[1])
            lo, hi = (p_in - p_out - half) * 100, (p_in - p_out + half) * 100
            ci = [round(lo, 2), round(hi, 2)]
            # An interval that spans zero is not a difference this data can show.
            distinguishable = lo > 0 or hi < 0

    compared = inside["rentals"] + outside["rentals"]
    unknown = excluded["unknown_origin"]
    degraded = (compared + unknown) > 0 and unknown / (compared + unknown) > DEGRADED_UNKNOWN_SHARE
    return {
        "inside": {**_side(inside), "areas_represented": len(by_area)},
        "outside": _side(outside),
        # Percentage points, inside minus outside; only when BOTH clear the floor.
        "difference_points": diff,
        "difference_points_ci95": ci,
        "difference_distinguishable": distinguishable,
        "by_area": [{"area": a, **_side(v)} for a, v in sorted(by_area.items())],
        "excluded": excluded,
        "_degraded": degraded,
    }


_SQL = """
SELECT equity_area, h3_9, SUM(rentals), SUM(no_gos), SUM(no_gos_max), SUM(max_known),
       SUM(stayed), SUM(stayed_known)
FROM rental_outcomes_hourly
WHERE hour >= %s AND hour < %s AND radius_m = %s
GROUP BY equity_area, h3_9
"""

# The first hour the table recorded areas at this radius (sql/092 onward).
_SQL_SINCE = """
SELECT MIN(hour) FROM rental_outcomes_hourly
WHERE radius_m = %s AND equity_area <> 'unrecorded'
"""

# When sql/098 ran, i.e. when `stayed` started being recorded.
_SQL_STAYED_SINCE = "SELECT applied_at FROM schema_migrations WHERE filename = %s"


def summarize(window: str = "7d", *, now: datetime | None = None) -> dict[str, Any]:
    """The inside/outside comparison over a trailing window. Never raises: a
    failure is `status: "unavailable"` with empty figures, so it can never be
    mistaken for a quiet week; `degraded` when unknown origins exceed
    DEGRADED_UNKNOWN_SHARE of the rentals (e.g. a broken boundary file)."""
    days = WINDOWS[window]
    end = (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    # NUMERIC(6,2) in the table: compare at the same precision.
    radius = round(float(load().device_tracking.stationary_threshold_meters), 2)
    status = "ok"
    since: datetime | None = None
    stayed_since: datetime | None = None
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_SQL, (start, end, radius))
                rows = [(str(r[0]), int(r[1]), int(r[2]), int(r[3]), int(r[4]), int(r[5]),
                         int(r[6] or 0), int(r[7] or 0))
                        for r in cur.fetchall()]
                cur.execute(_SQL_SINCE, (radius,))
                got = cur.fetchone()
                since = got[0] if got and got[0] else None
                cur.execute(_SQL_STAYED_SINCE, (STAYED_COUNTED_SINCE_MIGRATION,))
                got = cur.fetchone()
                stayed_since = got[0] if got and got[0] else None
        out = summarize_areas(rows)
        if out.pop("_degraded"):
            status = "degraded"
    except Exception:  # noqa: BLE001
        log.exception("equity outcomes summary failed")
        status = "unavailable"
        out = summarize_areas([])
        out.pop("_degraded")
    out["inside"]["areas_official"] = official_area_count()

    # Coverage: how much of the named window the table actually holds. A
    # "28-day" figure over two days of data must not be quoted as 28 days.
    covered_from = max(start, since) if since else None
    hours_covered = (
        max(0, int((end - covered_from).total_seconds() // 3600)) if covered_from else 0
    )
    # The same for `stayed`, which started later (sql/098). Whole hours only:
    # the hour the migration ran is partly recorded, and stayed_known (not the
    # clock) is what keeps that hour's rate exact.
    stayed_from = max(start, stayed_since) if stayed_since else None
    stayed_hours = (
        max(0, int((end - stayed_from).total_seconds() // 3600)) if stayed_from else 0
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
        # sql/098: never left the spot.
        "stayed_counted_since": stayed_since.isoformat() if stayed_since else None,
        "stayed_hours_covered": min(stayed_hours, days * 24),
        "stayed_radius_meters": float(STAYED_RADIUS_METERS),
        "stayed_definition": STAYED_DEFINITION,
        "attribution": "unlock_point",
        "boundary": "official_equity_areas",
        "caveats": CAVEATS,
    })
    return out
