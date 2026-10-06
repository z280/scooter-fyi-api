"""How long Veo takes to deal with a vehicle somebody reported as badly parked.

WHAT THIS MEASURES, AND WHY IT IS WORTH MEASURING. A rider who files an
improperly-parked report on Veo's own form has no way of knowing whether
anything happened. The operator's SLA talk is about response times; this is the
only side of that claim anybody outside the company can actually check — the
vehicle is in a public feed, and we already record every time it moves.

HOW IT IS DERIVED, AND WHY THERE IS NO NEW TABLE OR WATCHER.

The obvious build is a `parking_report_outcomes` row per report plus a
per-cycle job that watches for the move. We already have both halves:

  * `device_reports` (sql/013) stores the report with `reported_at` and the
    `vehicle_identifier`. That IS the timer starting.
  * `device_history` (sql/004) is an append-only stop log: one row per stop,
    `snapshot_time` when the vehicle arrived, `departed_at` when it left, and
    since sql/083 a `departure_reason` of 'moved' or 'absent'. That IS the log
    entry when it next moves — written every two minutes for the whole fleet,
    whether or not anybody reported anything.

So the measurement is a join, not a new pipeline. A derived figure is also the
honest one here: when the backfill corrects history, this number corrects with
it, where a materialised row would keep quoting a reconstruction we no longer
believe. If this page gets slow, or the figure starts being published outside
the admin panel (where a stable citation matters more than a live one),
materialising per report is the next step — and it can be backfilled from
exactly this query, over every report ever filed.

THE AMBIGUITY, STATED RATHER THAN HIDDEN. We cannot see WHO moved a vehicle.
A scooter blocking a ramp that is gone four hours later may have been
repositioned by an operator or simply rented by somebody who wanted a ride.
Three things keep that from making the number meaningless:

  * `departure_reason = 'absent'` is separated out. A vehicle that left the
    feed entirely was pulled — repair, retirement, re-keying — which is a
    different operator action from a reposition, and lumping it in with
    "resolved" would overstate responsiveness.
  * The CONTROL GROUP is the whole point. A median time-to-move of 31 hours
    for reported vehicles says nothing on its own; it says a great deal beside
    the median for vehicles nobody reported, over the same window. If the two
    are the same, the reports are going nowhere, and that is the finding.
  * A report on a vehicle that was ALREADY moving is excluded: there has to be
    an open stop covering the moment of the report, or there is no "before" to
    measure from.
"""

from __future__ import annotations

import logging
from typing import Any

from .pg import connection

log = logging.getLogger(__name__)

#: Reports younger than this are not counted as unresolved yet — a rider files
#: at 11pm and nobody is dispatching anything before morning. Purely a
#: presentation guard: the raw rows still carry their real age.
FRESH_REPORT_HOURS = 2

#: The report type the frontend sends when a rider opens Veo's parking form.
PARKING_REPORT_TYPE = "improperly_parked"


def _rows(cur) -> list[dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


#: Every reported vehicle, paired with the stop it was standing in when the
#: report was filed.
#:
#: The join is on "the stop that covers `reported_at`": arrived at or before
#: the report, and either still open or vacated after it. A vehicle with no
#: such stop is excluded by the inner join — it was mid-rental, or we had not
#: seen it yet, and either way there is no parked moment to measure from.
_REPORTED_SQL = """
SELECT dr.id                AS report_id,
       dr.reported_at,
       dr.vehicle_identifier,
       dh.snapshot_time     AS parked_since,
       dh.departed_at,
       dh.departure_reason,
       CASE WHEN dh.departed_at IS NULL THEN NULL
            ELSE EXTRACT(EPOCH FROM (dh.departed_at - dr.reported_at))
       END                  AS seconds_to_move
FROM device_reports dr
JOIN device_history dh
  ON dh.vehicle_identifier = dr.vehicle_identifier
 AND dh.snapshot_time <= dr.reported_at
 AND (dh.departed_at IS NULL OR dh.departed_at > dr.reported_at)
WHERE dr.report_type = %s
  AND dr.reported_at >= %s
ORDER BY dr.reported_at DESC
"""

#: The control: stops that were OPEN at some point in the same window on
#: vehicles nobody reported, and how long they lasted from the same kind of
#: starting point.
#:
#: `window_start` is used twice on purpose. A stop only counts if it was still
#: open when the window began (so we are comparing against vehicles that were
#: sitting parked at the same time the reports were being filed, not against
#: every brief stop in the period), and the clock runs from the later of the
#: window's start and the stop's own start — the same "time already parked is
#: not time waiting for a response" rule the reported side gets by measuring
#: from `reported_at` rather than from `parked_since`.
_CONTROL_SQL = """
SELECT EXTRACT(EPOCH FROM (dh.departed_at - GREATEST(dh.snapshot_time, %s)))
         AS seconds_to_move,
       dh.departure_reason
FROM device_history dh
WHERE dh.departed_at IS NOT NULL
  AND dh.departed_at >= %s
  AND dh.snapshot_time <= %s
  AND NOT EXISTS (
      SELECT 1 FROM device_reports dr
      WHERE dr.vehicle_identifier = dh.vehicle_identifier
        AND dr.report_type = %s
        AND dr.reported_at >= %s
  )
"""


def summarize(since, now) -> dict[str, Any]:
    """Time-to-move for parking-reported vehicles, against the control.

    `since` is the window start, `now` the clock (injected so the tests do not
    depend on wall time). Never raises: the admin page is a diagnostic, and a
    broken panel on it must not take the page down — a failure returns the
    empty shape and logs.
    """
    empty: dict[str, Any] = {
        "reports": [],
        "n": 0,
        "moved": 0,
        "absent": 0,
        "unresolved": 0,
        "median_hours": None,
        "p90_hours": None,
        "control_n": 0,
        "control_median_hours": None,
    }
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_REPORTED_SQL, (PARKING_REPORT_TYPE, since))
                reports = _rows(cur)
                cur.execute(
                    _CONTROL_SQL,
                    (since, since, now, PARKING_REPORT_TYPE, since),
                )
                control = _rows(cur)
    except Exception as e:  # noqa: BLE001
        log.exception("parking response summary failed")
        del e
        return empty

    return summarize_rows(reports, control, now)


def summarize_rows(
    reports: list[dict[str, Any]],
    control: list[dict[str, Any]],
    now,
) -> dict[str, Any]:
    """The arithmetic, split out so it is testable without a database.

    'Resolved' counts only `departure_reason = 'moved'`. An 'absent' close is
    the operator pulling the vehicle, which is a real response but a different
    one, and counting it as a reposition would flatter the number.
    """
    moved = [
        r for r in reports
        if r.get("departed_at") is not None and r.get("departure_reason") != "absent"
    ]
    absent = [
        r for r in reports
        if r.get("departed_at") is not None and r.get("departure_reason") == "absent"
    ]
    open_rows = [r for r in reports if r.get("departed_at") is None]

    secs = sorted(float(r["seconds_to_move"]) for r in moved if r.get("seconds_to_move"))
    control_secs = sorted(
        float(r["seconds_to_move"])
        for r in control
        if r.get("seconds_to_move") and r.get("departure_reason") != "absent"
    )

    for r in reports:
        # Age is what the unresolved rows are judged on, and it is the only
        # figure on this page that depends on the clock.
        if r.get("departed_at") is None:
            r["age_hours"] = round(
                (now - r["reported_at"]).total_seconds() / 3600.0, 1
            )
        else:
            r["hours_to_move"] = round(float(r["seconds_to_move"] or 0) / 3600.0, 1)

    return {
        "reports": reports,
        "n": len(reports),
        "moved": len(moved),
        "absent": len(absent),
        # Fresh reports are not failures yet — see FRESH_REPORT_HOURS.
        "unresolved": sum(
            1 for r in open_rows if (r.get("age_hours") or 0) >= FRESH_REPORT_HOURS
        ),
        "median_hours": _pct_hours(secs, 0.5),
        "p90_hours": _pct_hours(secs, 0.9),
        "control_n": len(control_secs),
        "control_median_hours": _pct_hours(control_secs, 0.5),
    }


def _pct_hours(sorted_secs: list[float], q: float) -> float | None:
    """Nearest-rank percentile, in hours. None for an empty sample — a median
    of zero reads as "instant" and would be a lie about having no data."""
    if not sorted_secs:
        return None
    i = min(len(sorted_secs) - 1, max(0, int(round(q * (len(sorted_secs) - 1)))))
    return round(sorted_secs[i] / 3600.0, 1)
