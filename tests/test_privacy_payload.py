"""GET /api/v1/meta/privacy is the enforced-policy source of truth, and
src/api_meta.py's own docstring says to change it in the same commit as
any retention rule. sql/038 stored model-report photos and changed
neither it nor the published HTML policy, so the objects were retained
forever while all three documents were silent.

These are drift guards, not a schema test: they check that the things the
code actually stores and deletes are named in the payload the frontend
privacy page renders, and in the policy a rider reads.
"""

from __future__ import annotations

from pathlib import Path

from src.api_meta import _PRIVACY

_POLICY_HTML = (
    Path(__file__).resolve().parents[1]
    / "src" / "templates" / "legal" / "privacy_policy.html"
).read_text()

_ENTRIES = {e["data"]: e for e in _PRIVACY["retention"]}


def test_every_entry_has_the_three_required_keys():
    for entry in _PRIVACY["retention"]:
        assert entry.keys() == {"data", "retention", "detail"}, entry


def test_model_reports_are_documented():
    entry = _ENTRIES["model_reports"]
    assert "18 months" in entry["retention"]
    # The finding was not only the photo: reporter_ip, reporter_user_agent,
    # lat/lng and the free-text description are all newly stored and were
    # undocumented in both the payload and the published policy.
    for stored in ("IP", "user agent", "description", "coordinates"):
        assert stored in entry["detail"], stored


def test_every_stored_binary_names_its_deletion_window():
    """Anything we hold a rider's image for has a stated window, because
    'indefinite' for a photo of where someone was standing is not a
    default anyone chose."""
    for key in ("receipts", "model_reports", "ride_transaction_screenshots"):
        assert "18 months" in _ENTRIES[key]["retention"]


def test_the_published_policy_covers_model_report_photos():
    assert "model-report photo" in _POLICY_HTML.lower()
    assert "Model-report photos" in _POLICY_HTML


def test_the_published_policy_admits_what_a_report_stores():
    """The old 'Reports' row enumerated only 'optional receipt images'."""
    for stored in ("user-agent", "IP address", "description", "model"):
        assert stored in _POLICY_HTML, stored


def test_the_policy_and_the_payload_carry_the_same_date():
    """Not a fixed date — the point is that the two can't drift apart. The
    policy said July 5 while the payload said July 27, which is how a
    reader could tell one of them had stopped being maintained."""
    import re
    from datetime import datetime

    match = re.search(r'class="updated">Last updated: ([^<]+)</p>', _POLICY_HTML)
    assert match, "the policy lost its Last updated line"
    html_date = datetime.strptime(match.group(1).strip(), "%B %d, %Y").date()
    assert html_date.isoformat() == _PRIVACY["updated"]


def test_telemetry_entries_are_documented():
    """sql/061 stores usage events and request metrics; the payload must
    name them and the retention the cleanup_telemetry cron enforces."""
    events = _ENTRIES["telemetry_events"]
    assert "90 days" in events["retention"]
    for promise in ("No account id", "salt", "Opt out"):
        assert promise in events["detail"], promise

    metrics = _ENTRIES["request_metrics"]
    assert "30 days" in metrics["retention"]
    assert "route template" in metrics["detail"]

    rollups = _ENTRIES["analytics_rollups"]
    assert "indefinite" in rollups["retention"].lower()
    assert "no identifiers" in rollups["detail"].lower()


def test_payload_retention_matches_cleanup_code():
    from src.analytics import (
        REQUEST_METRICS_RETENTION_DAYS,
        SALT_RETENTION_DAYS,
        TELEMETRY_RAW_RETENTION_DAYS,
    )

    assert TELEMETRY_RAW_RETENTION_DAYS == 90
    assert REQUEST_METRICS_RETENTION_DAYS == 30
    assert SALT_RETENTION_DAYS == 2
    assert "90 days" in _ENTRIES["telemetry_events"]["retention"]
    assert "30 days" in _ENTRIES["request_metrics"]["retention"]
    assert "2 days" in _ENTRIES["telemetry_events"]["detail"]


def test_the_published_policy_covers_usage_analytics():
    lower = _POLICY_HTML.lower()
    assert "usage analytics" in lower
    assert "90 days" in _POLICY_HTML
    assert "global privacy control" in lower
