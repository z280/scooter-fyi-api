"""docs/SERVICING_PLAN.md 2a: what the payload tells a rider (plan D2-D4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import servicing
from src.api_public import _service_fields, _serviced_since

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def f(serviced=None, parked=None, pct=90, depot=None):
    return _service_fields(serviced, parked, pct, depot, NOW)


def test_fresh_battery_needs_recent_unmoved_and_charged():
    s = NOW - timedelta(hours=2)
    assert f(s, parked=s - timedelta(hours=5))["fresh_battery"] is True       # in-place swap
    assert f(s, parked=s + timedelta(minutes=5))["fresh_battery"] is True     # van relocation
    assert f(s, parked=s + timedelta(hours=1))["fresh_battery"] is False      # moved since
    assert f(NOW - timedelta(hours=13), parked=NOW - timedelta(days=1))["fresh_battery"] is False
    assert f(s, parked=s - timedelta(hours=1), pct=79)["fresh_battery"] is False
    assert f(None, parked=NOW)["fresh_battery"] is False


def test_hide_risk_at_or_below_the_threshold():
    assert f(pct=servicing.HIDE_RISK_PERCENT)["hide_risk"] is True
    assert f(pct=servicing.HIDE_RISK_PERCENT + 1)["hide_risk"] is False
    assert f(pct=None)["hide_risk"] is False


def test_back_from_the_shop_after_a_long_stay_for_a_week():
    long = (NOW - timedelta(days=6), NOW - timedelta(days=2))
    out = f(depot=long)
    assert out["back_from_shop_at"] == long[1].isoformat() and out["back_from_shop_hours"] == 96
    assert f(depot=(NOW - timedelta(hours=30), NOW - timedelta(hours=20)))["back_from_shop_at"] is None
    old = (NOW - timedelta(days=20), NOW - timedelta(days=8))
    assert f(depot=old)["back_from_shop_at"] is None


def test_serviced_since_the_report():
    filed = NOW - timedelta(days=3)
    lr = {"reported_at": filed.isoformat()}
    assert _serviced_since(lr, filed - timedelta(hours=1), None) is None
    assert _serviced_since(lr, NOW - timedelta(days=1), None) == (NOW - timedelta(days=1)).isoformat()
    visit = (filed + timedelta(hours=2), NOW - timedelta(hours=3))
    assert _serviced_since(lr, None, visit) == visit[1].isoformat()
    assert _serviced_since(lr, None, (filed - timedelta(hours=1), NOW)) is None
