"""The /api/v1/compliance/daily/latest 'pending' contract.

Before the first daily SLA row exists, the endpoint must return a 200 with a
null-filled body — NOT a 503 — so the front-end gauge (which does
`v1Pct === null ? "pending" : v1Pct.toFixed(1)`) renders a pending state
instead of crashing on an undefined field. See docs/reference/API.md → Common patterns.

We drive the public handler with an empty result set (monkeypatched
`connection`) rather than poking at internal helpers, so the test is coupled to
the HTTP contract, not the implementation.
"""

import src.api_public as api_public


class _FakeCursor:
    def execute(self, *args, **kwargs):
        pass

    def fetchone(self):
        return None  # empty table → no latest row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_latest_returns_pending_payload_when_no_rows(monkeypatch):
    monkeypatch.setattr(api_public, "connection", lambda: _FakeConn())

    payload = api_public.daily_compliance_latest()

    # The two fields the documented gauge reads directly must be present and
    # null (not absent), so the front end sees `null`, not `undefined`.
    assert payload["avg_percent_all_devices_v1"] is None
    assert payload["compliance_v1_pass"] is None
    # sla_date is nullable in the pending shape.
    assert payload["sla_date"] is None
    # snapshot_count is the one honest non-null value: zero snapshots.
    assert payload["snapshot_count"] == 0
    # Everything else is null.
    for key, value in payload.items():
        if key != "snapshot_count":
            assert value is None, f"{key} should be null in the pending payload"


def test_pending_payload_carries_the_unmeasurable_verdict_field(monkeypatch):
    """sql/084 added `equity_unmeasurable_reason` to the stored row, so the
    null-filled shape has to carry it too — present and null, like every
    other field, or the two shapes of this endpoint disagree."""
    monkeypatch.setattr(api_public, "connection", lambda: _FakeConn())
    payload = api_public.daily_compliance_latest()
    assert "equity_unmeasurable_reason" in payload
    assert payload["equity_unmeasurable_reason"] is None


class _Col:
    def __init__(self, name):
        self.name = name


class _RowCursor:
    def __init__(self, names):
        self.description = [_Col(n) for n in names]


def test_the_unmeasurable_reason_serialises_as_its_code_not_a_number():
    """Every other non-null value in the row is NUMERIC and goes through
    float(). A TEXT reason code must come back as the string it is."""
    from datetime import date
    from decimal import Decimal

    cur = _RowCursor(
        ["sla_date", "snapshot_count", "avg_percent_all_devices_equity",
         "compliance_equity_pass", "equity_unmeasurable_reason"]
    )
    out = api_public._daily_row_to_dict(
        cur, (date(2026, 8, 9), 91, None, None, "low_fidelity")
    )
    assert out["equity_unmeasurable_reason"] == "low_fidelity"
    assert out["avg_percent_all_devices_equity"] is None
    assert out["compliance_equity_pass"] is None
    assert out["snapshot_count"] == 91

    measured = api_public._daily_row_to_dict(
        cur, (date(2026, 8, 11), 91, Decimal("16.81"), False, None)
    )
    assert measured["avg_percent_all_devices_equity"] == 16.81
    assert measured["compliance_equity_pass"] is False
    assert measured["equity_unmeasurable_reason"] is None
