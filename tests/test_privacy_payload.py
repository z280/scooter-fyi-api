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


def test_vehicle_state_fields_are_documented():
    """sql/087 added five stored device_state fields. They are about the
    vehicle, not a rider, and are overwritten rather than accumulated, but
    a new stored field is a retention rule (src/api_meta.py), so both the
    payload and the published policy say so."""
    entry = _ENTRIES["vehicle_state"]
    assert "no personal data" in entry["retention"]
    for fact in ("last three rentals", "cleared when the rental ends", "overwritten"):
        assert fact in entry["detail"], fact
    assert "<td>Vehicle state</td>" in _POLICY_HTML


# ---------------------------------------------------------------------------
# What changed on 2026-10-07: the plan screenshot went away, and reading an
# uploaded image is disclosed for the first time.
#
# These are the two disclosures most likely to go stale, for opposite reasons.
# The plan screenshot is a thing we STOPPED collecting, and a policy that still
# claims to collect it is over-disclosing — harmless to a reader but a sign the
# document is not maintained. Server-side reading is a thing we may START doing,
# and a policy that does not mention it is UNDER-disclosing, which is the kind
# that matters. `sql/093` added the plan screenshot and `sql/095` removed it
# without the published policy ever mentioning either; that gap is what these
# tests exist to stop recurring.
# ---------------------------------------------------------------------------




def _prose(html: str) -> str:
    """The policy as a reader sees it: tags removed, entities decoded, runs of
    whitespace collapsed, lowercased.

    Phrase assertions CANNOT be made against raw HTML. A sentence in this
    document is wrapped at the source width and carries `<strong>` tags, so
    "the rate plan you say you were on" is really "the rate plan you say you
    were\non</strong>" in the file. Asserting on the raw markup means either a
    false failure or a test weakened to single words until it stops saying
    anything. Single-word checks above still read the raw HTML, which is fine
    for a word; anything longer comes through here."""
    import html as _html
    import re

    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", _html.unescape(text)).strip().lower()


_POLICY_PROSE = _prose(_POLICY_HTML)


def test_the_policy_says_no_plan_screenshot_is_asked_for():
    assert "do not ask for a screenshot of your veo plan" in _POLICY_PROSE
    # And it says what stands in its place, so "we don't collect it" does not
    # read as "we don't need to know".
    assert "the rate plan you say you were on" in _POLICY_PROSE


def test_the_policy_describes_what_a_receipt_claim_stores():
    """The claim fields reached the payload in #117 but never the policy."""
    for stored in ("scooter code", "trip minutes", "charge date"):
        assert stored in _POLICY_PROSE, stored


def test_the_policy_discloses_reading_an_uploaded_image():
    """On-device or on our servers — and the policy must not promise one.

    Phase 8 of the frontend plan once said the image never leaves the device.
    The owner's rule (2026-10-07) is that either is allowed for something a
    rider explicitly uploads, preferring on-device. A policy asserting the
    stronger promise would be a promise the software does not keep.

    As of 2026-10-08 no reading runs anywhere (receipt-precheck.ts does no
    OCR; no server code calls OpenRouter), so the policy says so, and says
    what will apply if it starts."""
    assert "do not currently read receipt images automatically" in _POLICY_PROSE
    assert "on our servers" in _POLICY_PROSE
    # The limit that makes it acceptable: only what the rider sent.
    assert "only to an image you chose to upload" in _POLICY_PROSE


def test_the_policy_names_the_provider_that_reads_receipts():
    """A sub-processor that sees a rider's receipt has to be named BEFORE it
    sees one. `OPENROUTER_RECEIPTS_API_KEY` is wired into the deploy already."""
    assert "OpenRouter" in _POLICY_HTML


def test_the_payload_agrees_with_the_policy_about_plan_screenshots():
    detail = " ".join(_ENTRIES["receipts"]["detail"].lower().split())
    assert "no longer ask for a screenshot of your veo plan" in detail
    # And the payload carries the same reading disclosure, so a reader of
    # either document learns the same thing.
    assert "on your device or on our servers" in detail
