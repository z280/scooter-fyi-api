"""The /api/v1/compliance/daily/latest 'pending' contract.

Before the first daily SLA row exists, the endpoint must return a 200 with a
null-filled body — NOT a 503 — so the front-end gauge (which does
`v1Pct === null ? "pending" : v1Pct.toFixed(1)`) renders a pending state
instead of crashing on an undefined field. See API.md → Common patterns.

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
