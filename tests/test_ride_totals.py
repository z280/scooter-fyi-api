"""A rider's lifetime ride totals (frontend plan §11.8) — fake cursor, no DB.

What matters here is what the SQL asks for, because the failures are all silent:
a count that omits off-feed rides is wrong in the direction the rider notices
("your 12th ride" when they know it is their 30th), and a NULL distance summed
as zero drags a lifetime figure down invisibly as their history grows.
"""

from __future__ import annotations

from src.ride_totals import compute_ride_totals


class _FakeCursor:
    """Records the SQL and replays one canned row."""

    def __init__(self, row):
        self._row = row
        self.sql = ""
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchone(self):
        return self._row


def test_returns_the_three_figures():
    cur = _FakeCursor((12, 61_154.4, 9))
    assert compute_ride_totals(cur, 7) == {
        "rides": 12,
        "distance_meters": 61_154,
        "distance_from_rides": 9,
    }


def test_distance_is_rounded_to_whole_metres():
    """Sub-metre precision is noise from a straight-line fallback, and a lifetime
    figure carrying fifteen decimals invites a reader to believe all of them."""
    assert compute_ride_totals(_FakeCursor((1, 1609.3440000001, 1)), 7)[
        "distance_meters"
    ] == 1609


def test_no_rides_is_zeroes_rather_than_an_error():
    assert compute_ride_totals(_FakeCursor((0, 0, 0)), 7) == {
        "rides": 0,
        "distance_meters": 0,
        "distance_from_rides": 0,
    }


def test_a_missing_row_is_zeroes_too():
    """COUNT(*) always returns a row, so this is unreachable through Postgres —
    and a helper that returns None from a dict-shaped contract would surface as a
    TypeError inside a profile read, which is a worse failure than a zero."""
    assert compute_ride_totals(_FakeCursor(None), 7)["rides"] == 0


def test_spans_both_ride_mechanisms():
    """Both tables, like badges.py. A count that omitted either would be wrong in
    the direction the rider would notice."""
    cur = _FakeCursor((0, 0, 0))
    compute_ride_totals(cur, 7)
    assert "FROM tracked_rides" in cur.sql
    assert "FROM rides" in cur.sql
    assert "UNION ALL" in cur.sql
    # The account is asked for on BOTH sides of the union, or one side returns
    # every rider's history.
    assert cur.params == (7, 7)
    assert cur.sql.count("account_id = %s") == 2


def test_counts_only_ended_rides():
    """An abandoned ride is not evidence of a ride taken, however far its
    waypoints got before the phone stopped talking — badges.py's own rule."""
    cur = _FakeCursor((0, 0, 0))
    compute_ride_totals(cur, 7)
    assert "user_reported_ended_at IS NOT NULL" in cur.sql
    assert "ended_at IS NOT NULL" in cur.sql
    assert "status = 'completed'" in cur.sql


def test_distance_denominator_is_a_non_null_count_not_a_row_count():
    """`COUNT(distance_meters)` and not `COUNT(*)`: the denominator exists to say
    how many rides the distance was actually drawn from, so counting rows would
    make it equal `rides` always and the figure would claim to be complete."""
    cur = _FakeCursor((0, 0, 0))
    compute_ride_totals(cur, 7)
    assert "COUNT(distance_meters)" in cur.sql


def test_sum_is_coalesced_but_the_count_is_not():
    """SUM over no rows is NULL, which would crash the int(); COUNT never is. And
    COALESCE on the count would hide a real zero behind a defensive one."""
    cur = _FakeCursor((0, 0, 0))
    compute_ride_totals(cur, 7)
    assert "COALESCE(SUM(distance_meters), 0)" in cur.sql
