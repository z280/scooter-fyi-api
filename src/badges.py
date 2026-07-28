"""Server-computed profile badges (API_REQUIREMENTS.md §4.3).

Recomputed on every profile read — no stored badge state, so thresholds
can be tuned without migrations and retroactively apply. Earned badges
are available to every account. Nothing here is tied to payment —
the app has no paid tier (sql/036).

DEFINITIONS
-----------
    first_report        ≥ 1 device report filed
    reporter_10         ≥ 10 device reports filed
    ghost_hunter        ≥ 1 of your device reports corroborated — another
                        reporter (different account, or anonymous from a
                        different IP) reported the same vehicle within 7
                        days of yours
    discount_watchdog   ≥ 1 missed-discount report filed
    miles_10            ≥ 10 miles of logged rides (16 093 m)
    miles_100           ≥ 100 miles (160 934 m)
    streak_7            rides on 7 consecutive UTC days

earned_at is derived from the data (the row that crossed the threshold),
so recomputation is stable across reads.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

_MILES_10_M = 16_093
_MILES_100_M = 160_934
_STREAK_DAYS = 7


def _badge(badge_id: str, label: str, earned_at) -> dict[str, Any]:
    return {
        "id": badge_id,
        "label": label,
        "earned_at": earned_at.isoformat() if earned_at is not None else None,
    }


def _report_badges(cur, account_id: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cur.execute(
        "SELECT reported_at FROM device_reports WHERE account_id = %s "
        "ORDER BY reported_at ASC",
        (account_id,),
    )
    times = [r[0] for r in cur.fetchall()]
    if times:
        out.append(_badge("first_report", "Filed a report", times[0]))
    if len(times) >= 10:
        out.append(_badge("reporter_10", "10 reports filed", times[9]))

    # Corroboration: someone else reported the same vehicle within 7 days
    # of one of ours. Earliest corroborating report wins as earned_at.
    cur.execute(
        """
        SELECT MIN(other.reported_at)
        FROM device_reports mine
        JOIN device_reports other
          ON other.vehicle_identifier = mine.vehicle_identifier
         AND other.id <> mine.id
         AND other.reported_at BETWEEN mine.reported_at - INTERVAL '7 days'
                                   AND mine.reported_at + INTERVAL '7 days'
         AND (other.account_id IS DISTINCT FROM mine.account_id)
         AND (other.account_id IS NOT NULL
              OR other.reporter_ip IS DISTINCT FROM mine.reporter_ip)
        WHERE mine.account_id = %s
        """,
        (account_id,),
    )
    row = cur.fetchone()
    if row and row[0]:
        out.append(_badge("ghost_hunter", "Ghost scooter confirmed", row[0]))

    cur.execute(
        "SELECT MIN(created_at) FROM discount_reports WHERE account_id = %s",
        (account_id,),
    )
    row = cur.fetchone()
    if row and row[0]:
        out.append(_badge("discount_watchdog", "Discount watchdog", row[0]))
    return out


def _ride_badges(cur, account_id: int) -> list[dict[str, Any]]:
    """Mileage + streak badges over BOTH ride mechanisms, unioned and
    re-sorted by end time:

      - tracked_rides (sql/027) — GBFS-detected rides on Veo vehicles,
        distance from sql/034.
      - rides (sql/035) — off-feed rides on vehicles we don't track,
        distance from the rider's track or their own client.

    A rider's mileage is the miles they actually rode; which mechanism
    recorded a given ride is an implementation detail they never chose.
    Splitting the badges by table would mean someone who rides a personal
    scooter half the time needs twice the distance to earn the same badge.

    Both filters below drop rides nobody ever ended — off-feed rides the
    sql/040 sweep expired after 24h (status <> 'completed'), and tracked
    rides whose watch window elapsed (user_reported_ended_at IS NULL). Same
    rule from both sides: an abandoned ride is not evidence of a distance
    ridden, however far its waypoints got before the phone stopped talking.

    Distance quality varies by source (see both migrations): a ride with no
    waypoints carries a start->end straight line, and a one-shot log carries
    whatever the client claimed. Every source counts here on purpose —
    excluding the weak ones would mean a rider who doesn't hand us GPS earns
    nothing, which reads as the badge being broken. Undercounting is the
    kinder failure, since it only ever delays a badge.
    """
    out: list[dict[str, Any]] = []
    cur.execute(
        """
        SELECT ended_at, distance_meters FROM (
            SELECT user_reported_ended_at AS ended_at,
                   distance_meters
              FROM tracked_rides
             WHERE account_id = %s AND user_reported_ended_at IS NOT NULL
            UNION ALL
            SELECT ended_at AS ended_at,
                   distance_m::double precision
              FROM rides
             WHERE account_id = %s AND ended_at IS NOT NULL
               AND status = 'completed'
        ) all_rides
        ORDER BY ended_at ASC
        """,
        (account_id, account_id),
    )
    rows = cur.fetchall()
    if not rows:
        return out

    total = 0
    hit_10 = hit_100 = None
    for ended_at, distance_m in rows:
        total += int(distance_m or 0)
        if hit_10 is None and total >= _MILES_10_M:
            hit_10 = ended_at
        if hit_100 is None and total >= _MILES_100_M:
            hit_100 = ended_at
    if hit_10:
        out.append(_badge("miles_10", "10 miles logged", hit_10))
    if hit_100:
        out.append(_badge("miles_100", "100 miles logged", hit_100))

    streak_earned = _streak_earned_at(
        sorted({r[0].date() for r in rows}), _STREAK_DAYS
    )
    if streak_earned:
        out.append(_badge("streak_7", "7-day ride streak",
                          next(r[0] for r in rows if r[0].date() == streak_earned)))
    return out


def _streak_earned_at(days: list[date], needed: int) -> date | None:
    """First day on which a run of `needed` consecutive days completed."""
    run = 1
    for prev, cur_day in zip(days, days[1:]):
        run = run + 1 if cur_day - prev == timedelta(days=1) else 1
        if run >= needed:
            return cur_day
    return days[0] if needed <= 1 and days else None


def compute_badges(cur, account_id: int) -> list[dict[str, Any]]:
    badges = _report_badges(cur, account_id)
    badges += _ride_badges(cur, account_id)

    return badges
