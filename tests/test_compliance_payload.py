"""The /api/v1/compliance/daily/latest 'pending' payload.

Before the first daily SLA row exists, the endpoint must return a 200 with
a null-filled body — NOT a 503 — so the front-end gauge (which does
`v1Pct === null ? "pending" : v1Pct.toFixed(1)`) renders a pending state
instead of crashing on an undefined field. See API.md → Common patterns.
"""

from src.api_public import _empty_daily_payload
from src.daily_sla import _AVG_FIELDS


def test_pending_payload_has_every_documented_key():
    p = _empty_daily_payload()
    expected = (
        {"sla_date", "window_start_ts", "window_end_ts", "snapshot_count",
         "compliance_v1_pass", "compliance_v2_pass", "computed_at"}
        | {f"avg_{f}" for f in _AVG_FIELDS}
    )
    assert set(p.keys()) == expected


def test_pending_payload_fields_are_null_so_frontend_sees_null_not_undefined():
    p = _empty_daily_payload()
    # The two fields the documented gauge reads directly.
    assert p["avg_percent_all_devices_v1"] is None
    assert p["compliance_v1_pass"] is None
    # snapshot_count is the one non-null field: an honest "0 snapshots".
    assert p["snapshot_count"] == 0
    # Everything else is null.
    for k, v in p.items():
        if k != "snapshot_count":
            assert v is None, f"{k} should be null in the pending payload"
