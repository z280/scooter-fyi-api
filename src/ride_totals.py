"""A rider's own lifetime ride totals (frontend plan §11.8).

Why this exists, and why it is NOT just a page of the ride list
--------------------------------------------------------------
The end of a ride is where the app owes the rider the sentence that brings
them back: "that was your 12th ride, 38 miles". The client cannot get the
count from `GET /api/v1/tracked-rides` — that response's `count` is
`len(rides)`, the page size, not the total — so the only client-side answer
is to page all of a rider's history at the moment they are trying to put
their phone away. Counting the local track store instead gives a PER-DEVICE
figure, so a rider on a second phone is handed a confidently wrong ordinal.

So the totals are computed here, where one query answers them.

WHAT IS AND IS NOT SUMMED HERE
------------------------------
Only the LINEAR totals: a count and a distance. The frontend also shows what
a rider has paid above a competitive market, and that figure is NOT linear —
it compares each ride against a pass ladder (`config.ts`'s `COMPARATOR`),
and the cheapest pass covering two 15-minute rides is not the cheapest pass
covering one 30-minute ride. Summing the minutes here and quoting once would
give a different, smaller number than the per-ride truth.

That ladder is also a frontend constant, and duplicating it in Python to sum
it server-side would be a second copy of a pricing table — the failure mode
this codebase keeps deleting. So the premium stays a client computation over
whatever window of rides the client already fetched, and the frontend says
how many rides it covers.

BOTH RIDE TABLES, like `badges.py`
----------------------------------
`tracked_rides` (GBFS-detected) and `rides` (off-feed, rider-logged) are both
rides the rider took, and a count that omitted either would be wrong in the
direction the rider would notice — "your 12th ride" when they know it is
their 30th. The same UNION `badges.py` uses for its mileage badges, with the
same end-reported gate on each side: an abandoned ride is not evidence of a
ride taken, however far its waypoints got.

Distance quality varies by source and every source counts, for `badges.py`'s
reason: excluding the weak ones means a rider who does not hand us GPS gets
nothing, which reads as the feature being broken.
"""

from __future__ import annotations

from typing import Any


def compute_ride_totals(cur, account_id: int) -> dict[str, Any]:
    """`{rides, distance_meters, distance_from_rides}` for one account.

    `distance_from_rides` is the DENOMINATOR, and it is not decoration: a
    lifetime distance drawn from the three rides that happened to carry a
    measurement, presented as covering all twelve, is the kind of figure that
    gets noticed once and then never trusted again. The client needs to be
    able to say which.

    A NULL distance is counted as unknown rather than as zero. Summing it as
    zero would be a silent lie in the one direction that looks plausible, and
    it would drag a rider's lifetime figure down invisibly as their history
    grew.
    """
    cur.execute(
        """
        SELECT COUNT(*) AS rides,
               COALESCE(SUM(distance_meters), 0) AS distance_meters,
               COUNT(distance_meters) AS distance_from_rides
          FROM (
            SELECT distance_meters
              FROM tracked_rides
             WHERE account_id = %s AND user_reported_ended_at IS NOT NULL
            UNION ALL
            SELECT distance_m::double precision AS distance_meters
              FROM rides
             WHERE account_id = %s AND ended_at IS NOT NULL
               AND status = 'completed'
          ) all_rides
        """,
        (account_id, account_id),
    )
    row = cur.fetchone()
    if row is None:
        return {"rides": 0, "distance_meters": 0, "distance_from_rides": 0}
    rides, distance_meters, distance_from_rides = row
    return {
        "rides": int(rides or 0),
        # Rounded to whole metres: the sub-metre precision is noise from a
        # straight-line fallback, and a lifetime figure carrying fifteen
        # decimal places invites a reader to believe all of them.
        "distance_meters": int(round(float(distance_meters or 0))),
        "distance_from_rides": int(distance_from_rides or 0),
    }
