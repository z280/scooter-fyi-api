"""Did the rental go anywhere? — the fleet's headline number, aggregated.

WHAT THIS ANSWERS. A rider planning a trip deserves to know what share of
rentals never move, the same way they deserve to know a battery level. This is
consumer information about a service, and the copy that renders it says what
was counted and nothing about whose fault it is — see the frontend's
`docs/ANALYTICS_PLAN.md`, THE VOICE.

WHERE THE NUMBER COMES FROM, AND WHY IT IS TRUSTWORTHY. `sql/072` added
`device_state.rentals_observed` / `rentals_no_go`, incremented by
`device_state.py` at the moment each rental completes — "counted at the source,
not by scanning", because `raw_telemetry_points` is truncated at ~48 h and this
could not be derived later. Its validation is in that migration's header:
measured over 214,846 reservation episodes, 9.1% never left the kerb; the rate
persists per vehicle week to week (r=+0.275 over 7,534 vehicles); the worst 10%
of vehicles account for 32.4% of all no-gos; and the shipped reliability_tier
separates it 7.4% / 13.1% / 50.0%.

`is_reserved` on this feed means IN USE rather than a held booking
(`ride_watch.py`'s own measurement), so a no-go is an ATTEMPT — somebody
unlocked a vehicle and it did not take them anywhere — rather than a change of
mind. That is what makes the number worth publishing at all.

WHAT THIS IS NOT, AND CANNOT BE MADE INTO.

* **It is cumulative, not a time series.** These counters count up from zero
  since sql/072 and never reset, so this module can report the fleet's lifetime
  rate and cannot report last Tuesday's. The day-by-day story needs an hourly
  rollup written at the same moment the counters increment
  (`ANALYTICS_PLAN.md` §1, tier 2). Every figure here is explicitly labelled
  lifetime for that reason — a number whose window is unstated will be read as
  "now".
* **It is not a cause.** A no-go is an attempt that went nowhere. The cause
  might be the vehicle, the app, the weather, or a rider changing their mind
  after unlocking. This module counts; it does not attribute.

THE RADIUS, AND WHY THE RESPONSE CARRIES IT. The counter asks whether a rental
ended within the ingest's `stationary_threshold_meters` of where it was
unlocked. That was 16 m until 2026-10-06 and is 25 m since (sql/088) — the
radius sql/072's own validation was computed at.

So the counters SPAN TWO DEFINITIONS and the response's `radius_meters` reports
the CURRENT one, which is not the one most of the existing total was collected
under. That is a known hole, recorded on the column comment in sql/088, and it
closes when the counters are either reset or stamped with the radius they were
counted at. Until then a lifetime rate is a blend, and this field tells a
reader which circle the ingest is using today rather than which one produced
the number — a distinction worth stating plainly rather than papering over.

ONE MORE THING THE NUMBER IS NOT. `device_state.py` computes a no-go from END
DISPLACEMENT — unlock point to drop point — while sql/072's header describes
its validation as "never get 25 m from the kerb", a MAXIMUM distance. A round
trip that returns to the same rack therefore counts as a no-go. That is
deliberate (sql/087 kept `rentals_no_go` on the old definition so
`smart_ride_grade` stays calibrated), but it means copy of the form "never left
the kerb" overstates what is counted by however many loop rides there are. That
quantity has not been measured.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import load
from .pg import connection

log = logging.getLogger(__name__)

#: Fewest rentals before a group gets a published rate.
#:
#: A model or a fleet slice with a handful of rentals produces a rate that is
#: mostly noise, and a confident-looking percentage over n=7 is the kind of
#: figure that gets quoted back at you. Groups under the floor are returned
#: WITH their counts and a null rate, so the caller can say "not enough rides
#: yet" rather than silently dropping a model from the list.
MIN_RENTALS_FOR_RATE = 200


def _rate(no_gos: int, rentals: int) -> float | None:
    """No-go share, or None under the floor. Never divides by zero."""
    if rentals < MIN_RENTALS_FOR_RATE or rentals <= 0:
        return None
    return round(no_gos / rentals, 4)


def _radius_meters() -> float:
    """The circle these counters were actually measured at.

    Read from config rather than restated, because the whole point of
    publishing it is that it matches what was counted.
    """
    return float(load().device_tracking.stationary_threshold_meters)


_SQL = """
SELECT COALESCE(current_vehicle_model_name, 'Unknown') AS model,
       COALESCE(SUM(rentals_observed), 0)      AS rentals,
       COALESCE(SUM(rentals_no_go), 0)         AS no_gos,
       COUNT(*)                                AS vehicles
FROM device_state
WHERE rentals_observed > 0
GROUP BY 1
ORDER BY rentals DESC
"""


def summarize() -> dict[str, Any]:
    """Lifetime rental outcomes, fleet-wide and per model.

    Never raises: this backs a public endpoint whose failure should be an empty
    dashboard rather than a 500, and the caller distinguishes the two by the
    `rentals` count being zero.
    """
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_SQL)
                rows = [
                    {
                        "model": r[0],
                        "rentals": int(r[1]),
                        "no_gos": int(r[2]),
                        "vehicles": int(r[3]),
                    }
                    for r in cur.fetchall()
                ]
    except Exception:  # noqa: BLE001
        log.exception("fleet outcomes summary failed")
        rows = []

    return summarize_rows(rows, _radius_meters())


def summarize_rows(
    rows: list[dict[str, Any]], radius_meters: float
) -> dict[str, Any]:
    """The arithmetic, split out so it is testable without a database."""
    rentals = sum(r["rentals"] for r in rows)
    no_gos = sum(r["no_gos"] for r in rows)

    by_model = [
        {
            "model": r["model"],
            "rentals": r["rentals"],
            "no_gos": r["no_gos"],
            "vehicles": r["vehicles"],
            "no_go_rate": _rate(r["no_gos"], r["rentals"]),
        }
        for r in rows
    ]
    # Worst first among those with a publishable rate, then the rest by volume.
    # A list ordered by volume buries the finding; one that ranks unpublishable
    # rates alongside real ones invents a finding.
    by_model.sort(
        key=lambda m: (m["no_go_rate"] is None, -(m["no_go_rate"] or 0), -m["rentals"])
    )

    return {
        # Stated, not implied. These counters have no window.
        "window": "lifetime",
        "counted_since": "sql/072",
        # The circle a "no-go" was measured against — see the module header on
        # why this is in the payload rather than assumed by the reader.
        "radius_meters": radius_meters,
        "rentals": rentals,
        "no_gos": no_gos,
        "no_go_rate": _rate(no_gos, rentals),
        "min_rentals_for_rate": MIN_RENTALS_FOR_RATE,
        "vehicles": sum(r["vehicles"] for r in rows),
        "by_model": by_model,
    }
