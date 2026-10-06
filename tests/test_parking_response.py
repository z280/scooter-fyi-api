"""Veo's response time to parking reports.

The arithmetic is tested through `summarize_rows`, which takes the two row sets
the SQL produces and needs no database. What is pinned here is the judgement in
that function rather than the join: which closes count as a response, what an
empty sample reports, and that the control is computed on the same basis as the
reported side — because a median compared against a differently-computed median
is worse than no comparison at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import parking_response

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
H = 3600.0


def report(
    hours_ago: float,
    *,
    moved_after_h: float | None = None,
    reason: str | None = "moved",
):
    reported_at = NOW - timedelta(hours=hours_ago)
    departed = (
        None if moved_after_h is None else reported_at + timedelta(hours=moved_after_h)
    )
    return {
        "report_id": int(hours_ago * 100),
        "reported_at": reported_at,
        "vehicle_identifier": f"v{int(hours_ago)}",
        "parked_since": reported_at - timedelta(hours=1),
        "departed_at": departed,
        "departure_reason": None if departed is None else reason,
        "seconds_to_move": None if moved_after_h is None else moved_after_h * H,
    }


def control(hours: float, reason: str = "moved"):
    return {"seconds_to_move": hours * H, "departure_reason": reason}


def test_counts_each_outcome_separately():
    out = parking_response.summarize_rows(
        [
            report(10, moved_after_h=4),
            report(20, moved_after_h=2, reason="absent"),
            report(30),
        ],
        [],
        NOW,
    )
    assert out["n"] == 3
    assert out["moved"] == 1
    # Pulled from the feed is a real operator action but NOT a reposition;
    # folding it into "moved" would flatter the number.
    assert out["absent"] == 1
    assert out["unresolved"] == 1


def test_a_pulled_vehicle_is_not_counted_as_a_reposition_in_the_median():
    out = parking_response.summarize_rows(
        [report(50, moved_after_h=1), report(50, moved_after_h=99, reason="absent")],
        [],
        NOW,
    )
    assert out["median_hours"] == 1.0


def test_a_report_filed_minutes_ago_is_not_a_failure_yet():
    # A rider files at 11pm; nobody is dispatching anything before morning.
    out = parking_response.summarize_rows([report(0.5)], [], NOW)
    assert out["n"] == 1
    assert out["unresolved"] == 0
    assert out["reports"][0]["age_hours"] == 0.5


def test_an_empty_sample_reports_nothing_rather_than_zero():
    # A median of 0 h reads as "instant", which is the opposite of "no data".
    out = parking_response.summarize_rows([], [], NOW)
    assert out["median_hours"] is None
    assert out["p90_hours"] is None
    assert out["control_median_hours"] is None
    assert out["n"] == 0


def test_percentiles_over_a_known_sample():
    # NEAREST-RANK, not interpolated: every value reported is a duration some
    # real vehicle actually took, which is the right property for a figure
    # that may end up in an argument with an operator. On 1..10 that gives 5 h
    # and 9 h rather than a textbook 5.5 and 9.1.
    rows = [report(100 + i, moved_after_h=float(i + 1)) for i in range(10)]
    out = parking_response.summarize_rows(rows, [], NOW)
    assert out["median_hours"] == 5.0
    assert out["p90_hours"] == 9.0


def test_the_control_excludes_pulled_vehicles_too():
    # Same basis on both sides, or the comparison is meaningless.
    out = parking_response.summarize_rows(
        [report(10, moved_after_h=6)],
        [control(2), control(4), control(1000, reason="absent")],
        NOW,
    )
    assert out["control_n"] == 2
    assert out["control_median_hours"] == 2.0


def test_the_comparison_is_the_finding():
    # The shape that matters: reported vehicles no faster than unreported ones
    # means the reports are going nowhere. The module does not editorialise —
    # it just has to make both numbers available on the same basis.
    out = parking_response.summarize_rows(
        [report(50, moved_after_h=30), report(60, moved_after_h=32)],
        [control(29), control(31), control(30)],
        NOW,
    )
    assert out["median_hours"] == 30.0
    assert out["control_median_hours"] == 30.0


def test_resolved_rows_carry_hours_and_open_rows_carry_age():
    out = parking_response.summarize_rows(
        [report(10, moved_after_h=4.25), report(9)],
        [],
        NOW,
    )
    resolved, still_there = out["reports"]
    assert resolved["hours_to_move"] == 4.2 or resolved["hours_to_move"] == 4.3
    assert "age_hours" not in resolved
    assert still_there["age_hours"] == 9.0
    assert "hours_to_move" not in still_there


def test_summarize_never_raises_without_a_database(monkeypatch):
    # One panel on a diagnostic page must not be able to take the page down.
    def boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(parking_response, "connection", boom)
    out = parking_response.summarize(NOW - timedelta(days=7), NOW)
    assert out["n"] == 0
    assert out["reports"] == []
    assert out["median_hours"] is None
