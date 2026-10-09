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
  since they were reset by sql/089 (2026-10-07), so this module can report the
  rate since then and cannot report last Tuesday's. The day-by-day story needs an hourly
  rollup written at the same moment the counters increment
  (`ANALYTICS_PLAN.md` §1, tier 2). Every figure here is explicitly labelled
  "since the reset", with the reset's own timestamp, for that reason — a number
  whose window is unstated will be read as "now".
* **It is not a cause.** A no-go is an attempt that went nowhere. The cause
  might be the vehicle, the app, the weather, or a rider changing their mind
  after unlocking. This module counts; it does not attribute.

THE RADIUS, AND WHY THE RESPONSE CARRIES IT. The counter asks whether a rental
ended within the ingest's `stationary_threshold_meters` of where it was
unlocked. That was 16 m until 2026-10-06 and is 25 m since (sql/088) — the
radius sql/072's own validation was computed at.

Until 2026-10-07 the counters SPANNED TWO DEFINITIONS, with no way to tell
which share was which. The owner decided to reset them, and sql/089 did, so
everything counted since describes ONE ring. `radius_meters` is therefore now
the radius that produced the number, and `counted_since` / `counted_since_at`
name the reset and when it ran (read from schema_migrations, not assumed).

ONE MORE THING THE NUMBER IS NOT. `device_state.py` computes a no-go from END
DISPLACEMENT — unlock point to drop point — while sql/072's header describes
its validation as "never get 25 m from the kerb", a MAXIMUM distance. A round
trip that returns to the same rack therefore counts as a no-go. That is
deliberate (sql/087 kept `rentals_no_go` on the old definition so
`smart_ride_grade` stays calibrated), but it means copy of the form "never left
the kerb" overstates what is counted by however many loop rides there are.

NEVER LEFT THE SPOT (sql/098). That quantity has since been measured
(2026-10-07 08:02Z .. 2026-10-08 17:12Z, rebuilt from raw_telemetry_points):
about 57% of no-gos are round trips whose furthest point is more than 50 m
away (median 540 m); about 43% never left the spot, 660 of 30,727 rentals
(2.1%). So the response now ALSO carries `stayed` / `stayed_rate`: rentals
whose vehicle never got more than 50 m (`stayed_radius_meters`,
IN_PLACE_RADIUS_M) from where it was unlocked and was released there. It is
counted from `device_state.rentals_stayed` over its OWN denominator,
`rentals_observed_stayed_era`, because it started at the sql/098 deploy
(`stayed_counted_since`) while `rentals` counts from sql/089. The no-go figure,
its radius and its wording are unchanged. smart_ride_grade now reads the
stayed pair.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import load
# sql/098's radius. Imported, not restated: it is the in-place circle the
# ingest counts against.
from .device_state import IN_PLACE_RADIUS_M as STAYED_RADIUS_METERS
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


#: The migration that started the stayed counter; its applied_at is published
#: as `stayed_counted_since`.
STAYED_COUNTED_SINCE_MIGRATION = "098_rentals_stayed.sql"

STAYED_DEFINITION = (
    "never left the spot: the vehicle never got more than 50 m from where it "
    "was unlocked, and was released there"
)


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


#: The migration that started the counters' current window. Its applied_at,
#: read from schema_migrations, is published as `counted_since_at`.
COUNTED_SINCE_MIGRATION = "089_reset_rental_outcome_counters.sql"


def _counted_since_at(cur, migration: str = COUNTED_SINCE_MIGRATION) -> str | None:
    """When `migration` actually ran in THIS database, as ISO 8601, or None."""
    cur.execute(
        "SELECT applied_at FROM schema_migrations WHERE filename = %s",
        (migration,),
    )
    row = cur.fetchone()
    return row[0].isoformat() if row and row[0] else None


_SQL = """
SELECT COALESCE(current_vehicle_model_name, 'Unknown') AS model,
       COALESCE(SUM(rentals_observed), 0)      AS rentals,
       COALESCE(SUM(rentals_no_go), 0)         AS no_gos,
       COUNT(*)                                AS vehicles,
       COALESCE(SUM(rentals_observed_stayed_era), 0) AS stayed_rentals,
       COALESCE(SUM(rentals_stayed), 0)        AS stayed
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
    since_at: str | None = None
    stayed_since: str | None = None
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                since_at = _counted_since_at(cur)
                stayed_since = _counted_since_at(cur, STAYED_COUNTED_SINCE_MIGRATION)
                cur.execute(_SQL)
                rows = [
                    {
                        "model": r[0],
                        "rentals": int(r[1]),
                        "no_gos": int(r[2]),
                        "vehicles": int(r[3]),
                        "stayed_rentals": int(r[4]),
                        "stayed": int(r[5]),
                    }
                    for r in cur.fetchall()
                ]
    except Exception:  # noqa: BLE001
        log.exception("fleet outcomes summary failed")
        rows = []

    return summarize_rows(rows, _radius_meters(), counted_since_at=since_at,
                          stayed_counted_since=stayed_since)


def summarize_rows(
    rows: list[dict[str, Any]], radius_meters: float,
    counted_since_at: str | None = None,
    stayed_counted_since: str | None = None,
) -> dict[str, Any]:
    """The arithmetic, split out so it is testable without a database."""
    rentals = sum(r["rentals"] for r in rows)
    no_gos = sum(r["no_gos"] for r in rows)
    stayed_rentals = sum(r.get("stayed_rentals", 0) for r in rows)
    stayed = sum(r.get("stayed", 0) for r in rows)

    by_model = [
        {
            "model": r["model"],
            "rentals": r["rentals"],
            "no_gos": r["no_gos"],
            "vehicles": r["vehicles"],
            "no_go_rate": _rate(r["no_gos"], r["rentals"]),
            # sql/098, over its own (shorter) window and denominator.
            "stayed_rentals": r.get("stayed_rentals", 0),
            "stayed": r.get("stayed", 0),
            "stayed_rate": _rate(r.get("stayed", 0), r.get("stayed_rentals", 0)),
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
        # Stated, not implied. The window opens at the sql/089 reset.
        "window": "since_reset",
        "counted_since": "sql/089",
        "counted_since_at": counted_since_at,
        # The circle a "no-go" was measured against — see the module header on
        # why this is in the payload rather than assumed by the reader.
        "radius_meters": radius_meters,
        "rentals": rentals,
        "no_gos": no_gos,
        "no_go_rate": _rate(no_gos, rentals),
        "min_rentals_for_rate": MIN_RENTALS_FOR_RATE,
        "vehicles": sum(r["vehicles"] for r in rows),
        # sql/098: never left the spot. Its own window and denominator: the
        # counter started at the sql/098 deploy, after the sql/089 reset.
        "stayed_window": "since_stayed_counter",
        "stayed_counted_since": stayed_counted_since,
        "stayed_counted_since_migration": "sql/098",
        "stayed_radius_meters": float(STAYED_RADIUS_METERS),
        "stayed_rentals": stayed_rentals,
        "stayed": stayed,
        "stayed_rate": _rate(stayed, stayed_rentals),
        "stayed_definition": STAYED_DEFINITION,
        "by_model": by_model,
    }
