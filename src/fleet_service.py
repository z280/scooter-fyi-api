"""Fleet servicing summary — GET /api/v1/fleet/service (docs/SERVICING_PLAN.md 2b).

What the servicing log (sql/107) says about how the operator keeps the fleet
running: in-field battery swaps, the depot, and how many vehicles are on the
street. Public, because it is the operator's service, not anyone's personal
data. Every caveat travels in the response, as in src/fleet_equity.py.

THE EQUITY NUMBER. `swap_wait` is how long a vehicle sat at its lowest charge
(<= SWAP_WAIT_MAX_PERCENT) before it was serviced, split by whether that LOW
POINT is inside one of Denver's official Equity Areas. The point is tested,
never its hexagon (fleet_equity's lesson: classifying hexagons kept 1.5% of
rentals "inside"). Low points outside the City and County of Denver, or with
no position, are excluded and counted.

`low_at` is the first reading at the vehicle's lowest charge since it was last
full; a parked vehicle's reading is frozen, so observed_at - low_at is the
time it sat that empty. It is a lower bound on how long it was unusable.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from . import depots, servicing
from .pg import connection
from .quality import _soc_lut, compute_battery_percent

#: Swap wait counts vehicles serviced from at or below this charge.
SWAP_WAIT_MAX_PERCENT = 10

#: Stops before this date were counted under the earlier stop rules
#: (counting eras), so depot stays from before it are reported apart.
STOP_RULES_ERA = date(2026, 8, 10)

WINDOWS = {"7d": 7, "28d": 28, "90d": 90}


def _pct_meters(percent: int) -> int:
    lut = _soc_lut()
    return int(lut[round(percent / 100 * (len(lut) - 1))])


def _depot_distance_sql(lat: str, lon: str) -> str:
    """Distance from the visit's own depot (CASE over data/depots.json)."""
    cases = " ".join(
        f"WHEN '{d['id']}' THEN geo_distance_m({lat}, {lon}, {d['lat']}, {d['lon']})"
        for d in depots.depots())
    return f"(CASE depot_id {cases} END)" if cases else "NULL"


def summarize(window: str = "28d", now: datetime | None = None,
              days: int | None = None) -> dict[str, Any]:
    """`window` is one of WINDOWS, or any label when `days` is given (the
    advocacy export passes its own window)."""
    days = days if days is not None else WINDOWS[window]
    now = now or datetime.now(timezone.utc)
    p = {"now": now, "days": days, "low": _pct_meters(SWAP_WAIT_MAX_PERCENT)}
    with connection() as conn:
        with conn.cursor() as cur:
            since = "observed_at > %(now)s - make_interval(days => %(days)s) AND observed_at <= %(now)s"
            cur.execute(f"""
                SELECT COUNT(*), AVG(in_place::int),
                       percentile_cont(ARRAY[0.1,0.25,0.5,0.75,0.9])
                         WITHIN GROUP (ORDER BY low_range_meters),
                       AVG(after_absence::int), AVG((depot_id IS NOT NULL)::int),
                       MIN(observed_at)
                  FROM service_events WHERE {since}""", p)
            n, in_place, low_q, absent, at_depot, first = cur.fetchone()
            cur.execute(f"""
                SELECT EXTRACT(HOUR FROM observed_at AT TIME ZONE 'America/Denver')::int, COUNT(*)
                  FROM service_events WHERE {since} GROUP BY 1 ORDER BY 1""", p)
            by_hour = {int(h): int(c) for h, c in cur.fetchall()}
            cur.execute(f"""
                SELECT (observed_at AT TIME ZONE 'America/Denver')::date, COUNT(*)
                  FROM service_events WHERE {since} GROUP BY 1 ORDER BY 1""", p)
            by_day = [{"date": d.isoformat(), "swaps": int(c)} for d, c in cur.fetchall()]
            cur.execute(f"""
                SELECT CASE WHEN equity_area LIKE 'EQ\\_%%' THEN 'equity_areas'
                            WHEN equity_area = 'outside' THEN 'rest_of_denver'
                            ELSE 'excluded' END AS grp,
                       COUNT(*),
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM observed_at - low_at) / 3600),
                       percentile_cont(0.75) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM observed_at - low_at) / 3600)
                  FROM service_events
                 WHERE {since} AND low_range_meters <= %(low)s AND low_at IS NOT NULL
                 GROUP BY 1""", p)
            wait = {g: {"n": int(c), "median_hours": _r(m), "p75_hours": _r(q)}
                    for g, c, m, q in cur.fetchall()}

            dsince = "entered_at > %(now)s - make_interval(days => %(days)s) AND entered_at <= %(now)s"
            cur.execute(f"""
                SELECT COUNT(*), COUNT(*) FILTER (WHERE exited_at IS NOT NULL),
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM exited_at - entered_at) / 3600)
                         FILTER (WHERE exited_at IS NOT NULL),
                       COUNT(*) FILTER (WHERE exited_at - entered_at < INTERVAL '6 hours'),
                       COUNT(*) FILTER (WHERE exited_at - entered_at >= INTERVAL '6 hours'
                                          AND exited_at - entered_at < INTERVAL '24 hours'),
                       COUNT(*) FILTER (WHERE exited_at - entered_at >= INTERVAL '24 hours'
                                          AND exited_at - entered_at < INTERVAL '72 hours'),
                       COUNT(*) FILTER (WHERE exited_at - entered_at >= INTERVAL '72 hours'
                                          AND exited_at - entered_at < INTERVAL '7 days'),
                       COUNT(*) FILTER (WHERE exited_at - entered_at >= INTERVAL '7 days'),
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY {_depot_distance_sql('pickup_lat', 'pickup_lon')}),
                       AVG((geo_distance_m(pickup_lat, pickup_lon, deploy_lat, deploy_lon) < 300)::int)
                         FILTER (WHERE deploy_lat IS NOT NULL AND pickup_lat IS NOT NULL),
                       AVG(went_dark_first::int),
                       MIN(entered_at)
                  FROM depot_visits WHERE {dsince}""", p)
            (visits, completed, med_stay, b6, b24, b72, b7d, b7dp, med_pickup,
             redeployed_near, dark, first_visit) = cur.fetchone()
            cur.execute(f"""
                SELECT COALESCE(vehicle_model_name, 'Unknown'), COUNT(*),
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM exited_at - entered_at) / 3600),
                       AVG((exited_at - entered_at >= INTERVAL '72 hours')::int)
                  FROM depot_visits WHERE {dsince} AND exited_at IS NOT NULL
                 GROUP BY 1 HAVING COUNT(*) >= 20 ORDER BY 2 DESC""", p)
            by_model = [{"model": m, "visits": int(c), "median_stay_hours": _r(med),
                         "share_3_days_plus": _r(share, 3)} for m, c, med, share in cur.fetchall()]
            cur.execute(f"""
                SELECT to_char(entered_at AT TIME ZONE 'America/Denver', 'YYYY-MM'), COUNT(*),
                       COUNT(DISTINCT vehicle_identifier)
                  FROM depot_visits WHERE entered_at <= %(now)s GROUP BY 1 ORDER BY 1""", p)
            by_month = [{"month": m, "visits": int(c), "vehicles": int(v)}
                        for m, c, v in cur.fetchall()]
            fleet = servicing.effective_fleet(cur, now)

    n = int(n or 0)
    return {
        "as_of": now.isoformat(),
        "window": window,
        "swaps": {
            "count": n,
            "per_day": _r(n / days, 0) if n else 0,
            "share_in_place": _r(in_place, 3),
            "share_after_absence": _r(absent, 3),
            "share_at_depot": _r(at_depot, 3),
            "battery_at_swap_percent": (
                dict(zip(("p10", "p25", "p50", "p75", "p90"),
                         (compute_battery_percent(int(v)) for v in low_q)))
                if low_q else None),
            "by_hour_denver": [by_hour.get(h, 0) for h in range(24)],
            "by_day": by_day,
            "data_since": first.isoformat() if first else None,
        },
        "swap_wait": {
            "definition": (f"Hours a vehicle sat at its lowest charge (<= {SWAP_WAIT_MAX_PERCENT}%) "
                           "before it was serviced, by whether that spot is inside one of "
                           "Denver's official Equity Areas."),
            "equity_areas": wait.get("equity_areas"),
            "rest_of_denver": wait.get("rest_of_denver"),
            "excluded": {"outside_denver_or_unknown": (wait.get("excluded") or {}).get("n", 0)},
        },
        "depot": {
            "visits": int(visits or 0),
            "completed": int(completed or 0),
            "median_stay_hours": _r(med_stay),
            "stay": {"under_6h": int(b6 or 0), "6_to_24h": int(b24 or 0),
                     "1_to_3d": int(b72 or 0), "3_to_7d": int(b7d or 0),
                     "7d_plus": int(b7dp or 0)},
            "by_model": by_model,
            "median_pickup_km_from_depot": _r(med_pickup / 1000 if med_pickup else None),
            "share_redeployed_within_300m_of_pickup": _r(redeployed_near, 3),
            "share_went_dark_before_pickup": _r(dark, 3),
            "by_month": by_month,
            "data_since": first_visit.isoformat() if first_visit else None,
        },
        "fleet": fleet,
        "caveats": [
            "The depot is inferred from where vehicles report their position; the feed never says 'depot'.",
            "A depot stay may be charging, repair or retirement; the feed cannot tell them apart. Long stays are the closest proxy for repair.",
            "A swap is a vehicle reading full (95%+) while parked after reading 50% or less parked since it was last full. Charges that stop short of 95% are not counted.",
            f"Depot stays that began before {STOP_RULES_ERA.isoformat()} were recorded under earlier stop rules and are not directly comparable with later months.",
            "Swap history before the raw-telemetry archive (2026-07-10) does not exist.",
        ],
    }


def _r(v: Any, digits: int = 1) -> Any:
    if v is None:
        return None
    return round(float(v), digits) if digits else int(round(float(v)))
