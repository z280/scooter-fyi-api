"""GET /api/v1/compliance/calendar — per-day pass/fail for whole months.

The calendar's whole job is to be honest about days it has nothing for, so
that is what this covers: every day of every requested month is present,
and the five states a day can be in (`pass`, `fail`, `no_data`, `pending`,
`unmeasurable`) are kept distinct rather than collapsing into "not green".

Postgres is faked: the handler runs one parameterised SELECT, so a cursor
that records the query and returns canned rows exercises the real month
math, the real status derivation, and the real shape of the response.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime

import pytest

from src import api_public


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed: list[tuple] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_db(monkeypatch, rows):
    cur = _FakeCursor(rows)

    @contextmanager
    def _conn():
        yield _FakeConn(cur)

    monkeypatch.setattr(api_public, "connection", _conn)
    return cur


def _declared_default(name: str):
    """The value FastAPI would inject for a query param.

    These tests call the handler directly (the repo's pattern — see
    tests/test_api_public_no_plate.py), which bypasses FastAPI's dependency
    resolution, so an unpassed argument arrives as the raw `Query(...)`
    object. Reading the declared default keeps the tests exercising the
    real defaults instead of a second copy of them.
    """
    import inspect

    return inspect.signature(api_public.compliance_calendar).parameters[name].default.default


#: Collects the Cache-Control the handler sets, so a test can assert on it.
_LAST_RESPONSE: list = []


def _call(**kwargs):
    from fastapi import Response

    resp = Response()
    _LAST_RESPONSE[:] = [resp]
    defaults = {n: _declared_default(n) for n in ("month", "count", "group")}
    defaults.update(kwargs)
    return api_public.compliance_calendar(resp, **defaults)


def _freeze_today(monkeypatch, d: date):
    """Pin "today" without touching the module's real datetime import."""
    real = api_public.datetime

    class _DT(real):
        @classmethod
        def now(cls, tz=None):
            return real(d.year, d.month, d.day, 12, 0, tzinfo=tz)

    monkeypatch.setattr(api_public, "datetime", _DT)


# A stored row: (sla_date, avg_percent, pass_flag, snapshot_count,
# unmeasurable_reason) — the SELECT's column order.
def _row(d, pct, passed, n=90, reason=None):
    return (d, pct, passed, n, reason)


def test_returns_the_current_and_prior_month_by_default(monkeypatch):
    _patch_db(monkeypatch, [])
    _freeze_today(monkeypatch, date(2026, 8, 21))
    out = _call()
    assert [m["month"] for m in out["months"]] == ["2026-07", "2026-08"]
    # Oldest first, so the response reads in calendar order.
    assert out["months"][0]["first_date"] == "2026-07-01"
    assert out["months"][1]["last_date"] == "2026-08-31"


def test_every_day_of_the_month_is_present_even_with_no_data(monkeypatch):
    """A calendar grid needs a cell per day. Omitting the empty ones would
    make "the job never ran" and "the day failed" render the same."""
    _patch_db(monkeypatch, [])
    _freeze_today(monkeypatch, date(2026, 8, 21))
    out = _call(month="2026-02", count=1)
    days = out["months"][0]["days"]
    assert len(days) == 28                      # 2026 is not a leap year
    assert {d["status"] for d in days} == {"no_data"}
    assert days[0]["date"] == "2026-02-01"
    assert days[-1]["date"] == "2026-02-28"


def test_leap_february_has_twenty_nine_days(monkeypatch):
    _patch_db(monkeypatch, [])
    _freeze_today(monkeypatch, date(2028, 3, 1))
    out = _call(month="2028-02", count=1)
    assert len(out["months"][0]["days"]) == 29


def test_pass_and_fail_come_from_the_stored_flag(monkeypatch):
    _patch_db(monkeypatch, [
        _row(date(2026, 8, 1), 41.2, True),
        _row(date(2026, 8, 2), 18.9, False),
    ])
    _freeze_today(monkeypatch, date(2026, 8, 21))
    out = _call(month="2026-08", count=1)
    days = {d["date"]: d for d in out["months"][0]["days"]}
    assert days["2026-08-01"]["status"] == "pass"
    assert days["2026-08-01"]["percent"] == 41.2
    assert days["2026-08-02"]["status"] == "fail"
    assert days["2026-08-03"]["status"] == "no_data"
    assert out["months"][0]["pass_days"] == 1
    assert out["months"][0]["fail_days"] == 1


def test_a_row_with_no_equity_average_is_pending_not_failing(monkeypatch):
    """Days that predate the official map have an SLA row but a NULL equity
    average until the reprocessing job reaches them. Colouring that red
    would accuse Veo of failing a day nobody has measured yet."""
    _patch_db(monkeypatch, [_row(date(2026, 8, 1), None, None, n=90)])
    _freeze_today(monkeypatch, date(2026, 8, 21))
    out = _call(month="2026-08", count=1)
    day = out["months"][0]["days"][0]
    assert day["status"] == "pending"
    assert day["percent"] is None
    assert out["months"][0]["fail_days"] == 0


def test_future_days_are_flagged(monkeypatch):
    """The current month includes days that have not happened. The client's
    clock may not be in Denver, so the server says which they are."""
    _patch_db(monkeypatch, [])
    _freeze_today(monkeypatch, date(2026, 8, 21))
    out = _call(month="2026-08", count=1)
    days = {d["date"]: d for d in out["months"][0]["days"]}
    assert days["2026-08-20"]["in_future"] is False
    assert days["2026-08-21"]["in_future"] is False    # today is not future
    assert days["2026-08-22"]["in_future"] is True


def test_count_walks_backwards_across_a_year_boundary(monkeypatch):
    _patch_db(monkeypatch, [])
    _freeze_today(monkeypatch, date(2027, 1, 15))
    out = _call(month="2027-01", count=3)
    assert [m["month"] for m in out["months"]] == ["2026-11", "2026-12", "2027-01"]


def test_the_query_spans_exactly_the_requested_months(monkeypatch):
    """One SELECT for the whole range, not one per month."""
    cur = _patch_db(monkeypatch, [])
    _freeze_today(monkeypatch, date(2026, 8, 21))
    _call(month="2026-08", count=2)
    assert len(cur.executed) == 1
    _, params = cur.executed[0]
    assert params == (date(2026, 7, 1), date(2026, 8, 31))


def test_the_official_map_is_the_default_group(monkeypatch):
    from src.equity_groups import OFFICIAL_GROUP

    cur = _patch_db(monkeypatch, [])
    _freeze_today(monkeypatch, date(2026, 8, 21))
    # The DECLARED default is the thing under test: a caller that asks for
    # nothing must get the map the contract binds, not the legacy one.
    assert _declared_default("group") == OFFICIAL_GROUP == "equity"
    assert _declared_default("count") == 2   # this month and last
    out = _call()
    assert out["group"] == OFFICIAL_GROUP
    sql, _ = cur.executed[0]
    assert "avg_percent_all_devices_equity" in sql
    assert "compliance_equity_pass" in sql


def test_the_legacy_maps_are_still_reachable(monkeypatch):
    """v1/v2 keep their flags so the pre-clarification series stays
    readable; the calendar can render either."""
    cur = _patch_db(monkeypatch, [])
    _freeze_today(monkeypatch, date(2026, 8, 21))
    out = _call(group="v1")
    assert out["group"] == "v1"
    assert "avg_percent_all_devices_v1" in cur.executed[0][0]


def test_an_unknown_group_is_rejected_rather_than_interpolated(monkeypatch):
    """`group` reaches a column name, so it is checked against the registry
    rather than trusted — an unvalidated value here is an injection."""
    from fastapi import HTTPException

    _patch_db(monkeypatch, [])
    _freeze_today(monkeypatch, date(2026, 8, 21))
    for bad in ("er1", "devices_v1; DROP TABLE daily_sla_compliance --", ""):
        with pytest.raises(HTTPException) as e:
            _call(group=bad)
        assert e.value.status_code == 400


def test_a_malformed_month_is_a_400(monkeypatch):
    from fastapi import HTTPException

    _patch_db(monkeypatch, [])
    _freeze_today(monkeypatch, date(2026, 8, 21))
    with pytest.raises(HTTPException) as e:
        _call(month="August 2026")
    assert e.value.status_code == 400


def test_threshold_is_the_one_the_job_stamps_with(monkeypatch):
    """The calendar draws pass/fail against a number; if it were a second
    copy it could drift from the one daily_sla actually applied."""
    from src.daily_sla import COMPLIANCE_THRESHOLD

    _patch_db(monkeypatch, [])
    _freeze_today(monkeypatch, date(2026, 8, 21))
    assert _call()["threshold"] == COMPLIANCE_THRESHOLD


def test_the_response_is_briefly_cacheable(monkeypatch):
    """Five minutes: the rows behind this move once a day, but the day the
    number lands is the day somebody is refreshing the page."""
    _patch_db(monkeypatch, [])
    _freeze_today(monkeypatch, date(2026, 8, 21))
    _call()
    assert _LAST_RESPONSE[0].headers["Cache-Control"] == "public, max-age=300"


# ---------------------------------------------------------------------------
# unmeasurable (sql/084)
# ---------------------------------------------------------------------------
def test_a_day_the_job_could_not_measure_is_unmeasurable_not_pending(monkeypatch):
    """2026-08-09/10: snapshots exist, every one failed the reconstruction
    fidelity gate, so the latest attempt could defend no figure. `pending`
    would say no attempt has been made; `fail` would accuse Veo of a miss
    nobody measured. The stored verdict makes it its own status, until a
    later successful reconstruction supersedes it."""
    _patch_db(monkeypatch, [
        _row(date(2026, 8, 9), None, None, n=91, reason="low_fidelity"),
        _row(date(2026, 8, 10), None, None, n=93, reason="no_history"),
        _row(date(2026, 8, 11), None, None, n=91),
    ])
    _freeze_today(monkeypatch, date(2026, 9, 29))
    out = _call(month="2026-08", count=1)
    days = {d["date"]: d for d in out["months"][0]["days"]}
    assert days["2026-08-09"]["status"] == "unmeasurable"
    assert days["2026-08-10"]["status"] == "unmeasurable"
    # No verdict stored: still just not reprocessed yet.
    assert days["2026-08-11"]["status"] == "pending"
    # No figure is invented, and the day's snapshots are still reported.
    assert days["2026-08-09"]["percent"] is None
    assert days["2026-08-09"]["snapshot_count"] == 91
    # Unmeasurable is not a verdict on Veo either way.
    assert out["months"][0]["pass_days"] == 0
    assert out["months"][0]["fail_days"] == 0


def test_a_stored_figure_beats_a_stale_verdict(monkeypatch):
    """daily_sla clears the verdict when it writes a figure; the calendar
    ALSO lets the figure win, so a verdict that somehow survived can never
    hide a real pass or fail."""
    _patch_db(monkeypatch, [
        _row(date(2026, 8, 9), 31.0, True, reason="low_fidelity"),
        _row(date(2026, 8, 10), 12.0, False, reason="low_fidelity"),
    ])
    _freeze_today(monkeypatch, date(2026, 9, 29))
    days = {d["date"]: d for d in _call(month="2026-08", count=1)["months"][0]["days"]}
    assert days["2026-08-09"]["status"] == "pass"
    assert days["2026-08-10"]["status"] == "fail"


def test_the_official_map_reads_its_verdict_column(monkeypatch):
    cur = _patch_db(monkeypatch, [])
    _freeze_today(monkeypatch, date(2026, 8, 21))
    _call(group="equity")
    assert "equity_unmeasurable_reason" in cur.executed[0][0]


def test_live_recorded_maps_can_never_be_unmeasurable(monkeypatch):
    """v1/v2 were recorded live and are never reconstructed, so they have
    no verdict column: the query selects a typed NULL in its place (rather
    than borrowing the official map's) and a NULL average stays pending."""
    for group in ("v1", "v2"):
        cur = _patch_db(monkeypatch, [_row(date(2026, 8, 9), None, None)])
        _freeze_today(monkeypatch, date(2026, 8, 21))
        out = _call(group=group, month="2026-08", count=1)
        sql = cur.executed[0][0]
        assert "unmeasurable_reason" not in sql
        assert "NULL::TEXT" in sql
        assert out["months"][0]["days"][8]["status"] == "pending"


def test_status_precedence_covers_every_documented_value():
    """The five values docs/reference/API.md documents, and the order they are decided in."""
    status = api_public._calendar_status
    assert status(None) == "no_data"
    assert status({"pass": True, "unmeasurable_reason": None}) == "pass"
    assert status({"pass": False, "unmeasurable_reason": None}) == "fail"
    assert status({"pass": None, "unmeasurable_reason": None}) == "pending"
    assert status({"pass": None, "unmeasurable_reason": "low_fidelity"}) == "unmeasurable"
    assert status({"pass": True, "unmeasurable_reason": "low_fidelity"}) == "pass"
