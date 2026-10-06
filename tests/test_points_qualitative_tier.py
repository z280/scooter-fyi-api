"""Written route feedback is tiered (owner, 2026-10-06): 6 for a short
note past the caller's 20-character floor, 12 once it is a real
explanation (NAV_QUALITATIVE_DETAILED_MIN_CHARS)."""

from __future__ import annotations

import pytest

from src import points


@pytest.mark.parametrize("length, expected", [
    (20, 6),
    (points.NAV_QUALITATIVE_DETAILED_MIN_CHARS - 1, 6),
    (points.NAV_QUALITATIVE_DETAILED_MIN_CHARS, 12),
    (400, 12),
])
def test_the_award_follows_the_length(monkeypatch, length, expected):
    seen = {}

    def _credit(cur, **kw):
        seen.update(kw)
        return {"action": kw["action"], "points": kw["points"]}

    monkeypatch.setattr(points, "credit_points", _credit)
    out = points.credit_nav_qualitative_feedback(
        None, account_id=1, vehicle_identifier=None, lat=39.7, lng=-104.9,
        ride_id="r1", text_length=length)
    assert out["points"] == expected == seen["points"]
    assert seen["action"] == "nav_qualitative_feedback"


def test_both_tiers_are_even():
    assert points.POINTS_NAV_QUALITATIVE % 2 == 0
    assert points.POINTS_NAV_QUALITATIVE_DETAILED % 2 == 0
