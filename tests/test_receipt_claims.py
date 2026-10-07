"""Equity receipt claims, Phase 1 (docs/PLAN_EQUITY_RECEIPTS.md): the gate,
the arithmetic, and the endpoint's ordering guarantees.

The arithmetic is checked against the owner's labelled receipts
(tests/fixtures/receipts/labels.json), not invented numbers.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src import receipt_claims as rc
from tests.test_discount_report_upload_order import ctx  # noqa: F401  (fixture)

LABELS = json.loads(
    (Path(__file__).parent / "fixtures" / "receipts" / "labels.json").read_text()
)["receipts"]
RECEIPTS = [r for r in LABELS if r["kind"] == "receipt" and r.get("finding")]


# --- pure rules --------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("1018354", "1018354"), (" 102 5640 ", "1025640"), ("1021645\n", "1021645"),
    ("101835", None), ("12345678901", None), ("10a8354", None), ("", None), (None, None),
])
def test_plate_normalisation(raw, want):
    assert rc.normalize_plate(raw) == want


def test_the_gate_names_what_is_missing():
    full = rc.Claim("1018354", 16, 500, None, date(2026, 9, 29))
    assert rc.missing_for_rate_check(full) == []
    assert rc.missing_for_rate_check(rc.Claim(None, None, None, None, None)) == [
        "vehicle_plate", "trip_minutes", "cost", "charge_date"]
    # Either cost is enough.
    assert rc.missing_for_rate_check(rc.Claim("1018354", 16, None, 546, date(2026, 9, 29))) == []


@pytest.mark.parametrize("r", RECEIPTS, ids=lambda r: r["file"])
def test_arithmetic_matches_the_labelled_receipts(r):
    e, f = r["expected"], r["finding"]
    got = rc.arithmetic(
        rc.Claim(e["plate"], e["minutes"], e["subtotal_cents"], e["total_cents"],
                 date.fromisoformat(e["charge_date"])),
        0.0915,
    )
    assert got["rate_signature"] == f["rate_signature"]
    if f.get("equity_price_cents") is not None:
        assert got["expected_cents"] == f["equity_price_cents"]
    want_tax = f["tax_finding"]
    # "tax_ok_either_rounding": half-up and ceiling agree, which is tax_ok.
    assert got["tax"]["finding"] == ("tax_ok" if want_tax == "tax_ok_either_rounding" else want_tax)


def test_a_short_ride_prefers_the_published_rate():
    # $2.00 for 4 min fits $1 + 25c and $0 + 50c; Veo has no 50c rate.
    assert rc.rate_signatures(200, 4) == ["$1 + 25c/min"]


def test_a_price_no_plan_explains_has_no_signature():
    assert rc.rate_signatures(517, 16) == []


def test_tax_findings():
    assert rc.tax_finding(500, 546, 0.0915)["finding"] == "tax_ok"          # 45.75 -> 46 either way
    up = rc.tax_finding(200, 219, 0.0915)                                    # 18.3 -> 19
    assert up["finding"] == "tax_rounded_up" and up["excess_cents"] == 1
    odd = rc.tax_finding(500, 600, 0.0915)
    assert odd["finding"] == "tax_unexplained" and odd["implied_rate"] == 0.2


def test_subtotal_backed_out_of_a_total_is_flagged_and_not_signed():
    got = rc.arithmetic(rc.Claim("1018354", 16, None, 546, date(2026, 9, 29)), 0.0915)
    assert got["charged_subtotal_cents"] == 500 and got["subtotal_derived_from_total"] is True
    assert got["rate_error_cents"] == 500 - 308
    assert got["rate_signature"] is None and got["tax"] is None


# --- the endpoint --------------------------------------------------------------

CLAIM = {
    "vehicle_plate": "1018354", "trip_minutes": "16", "subtotal_cents": "500",
    "total_cents": "546", "charge_date": "2026-09-29", "declared_rate_plan": "resident",
}
IMAGES = {"receipt": ("r.png", b"\x89PNG-receipt", "image/png")}
# What an OLDER CLIENT still sends. sql/095 dropped the plan screenshot, and the
# endpoint ignores the part rather than rejecting it, so a client built before
# that keeps working — these tests prove the bytes are neither read nor stored.
LEGACY_IMAGES = {**IMAGES, "plan_evidence": ("p.png", b"\x89PNG-plan", "image/png")}


def _insert(state):
    rows = [p for sql, p in state["sql"] if sql.startswith("INSERT INTO discount_reports")]
    assert len(rows) == 1, state["sql"]
    return rows[0]


def test_a_complete_claim_is_stored_with_its_arithmetic(ctx):
    client, state = ctx
    r = client.post("/api/v1/reports/discount", data=CLAIM, files=IMAGES)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "received" and body["receipt_stored"] is True
    assert "plan_evidence_stored" not in body
    p = _insert(state)
    assert p[1] == "1018354" and p[2] and p[2] != "1018354"     # plate + its HMAC
    assert p[3:7] == (16, 500, 546, date(2026, 9, 29))
    assert p[12] == "resident"
    # Indices shifted down one when `plan_evidence_r2_key` left the INSERT
    # (sql/095): receipt_r2_key is now the last key column.
    analysis = json.loads(p[19])
    assert (p[14], p[15], p[16]) == (308, 192, "$1 + 25c/min")
    assert (p[17], p[18]) == (46, "tax_ok")
    assert analysis["expected_cents"] == 308
    assert p[13] == "receipt.jpg"                              # the one key (stubbed)
    # Rate limit before the upload, and exactly ONE image stored: no plan
    # screenshot is asked for or kept (sql/095).
    assert state["order"] == ["RATELIMIT", "R2_PUT"]


def test_an_unusable_claim_keeps_nothing(ctx):
    client, state = ctx
    r = client.post("/api/v1/reports/discount",
                    data={"vehicle_plate": "1018354", "charge_date": "2026-09-29"}, files=IMAGES)
    assert r.status_code == 422
    assert r.json()["detail"] == {"error": "not_rate_checkable", "missing": ["trip_minutes", "cost"]}
    assert state["order"] == [] and state["sql"] == []


def test_no_plan_screenshot_is_asked_for(ctx):
    client, state = ctx
    # The receipt alone is a complete claim now: the rider tells us the plan and
    # we take their word for it (owner, 2026-10-07). This used to be a 422
    # `plan_evidence_required`.
    r = client.post("/api/v1/reports/discount", data=CLAIM, files=IMAGES)
    assert r.status_code == 200, r.text
    assert state["order"] == ["RATELIMIT", "R2_PUT"]


def test_an_older_client_still_works_and_its_plan_image_is_discarded(ctx):
    client, state = ctx
    r = client.post("/api/v1/reports/discount", data=CLAIM, files=LEGACY_IMAGES)
    assert r.status_code == 200, r.text
    # Ignored, not rejected — and never uploaded. One PUT, not two: an image we
    # stored but never read would be the privacy cost sql/095 exists to remove.
    assert state["order"] == ["RATELIMIT", "R2_PUT"]
    p = _insert(state)
    assert p[13] == "receipt.jpg"
    # Exactly one image key in the row: the plan part produced no second key.
    assert p.count("receipt.jpg") == 1


def test_the_receipt_is_required(ctx):
    client, state = ctx
    # A part the endpoint never looks for, purely to make the request multipart
    # (which is what marks it a claim) while carrying no receipt.
    r = client.post("/api/v1/reports/discount", data=CLAIM,
                    files={"unused": ("u.txt", b"x", "text/plain")})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "receipt_required"
    assert state["order"] == []


@pytest.mark.parametrize("field,value", [
    ("vehicle_plate", "10183"), ("trip_minutes", "0"), ("trip_minutes", "abc"),
    ("charge_date", "2999-01-01"), ("charge_date", "2023-12-31"), ("charge_date", "29/09/2026"),
    ("declared_rate_plan", "gold"), ("total_cents", "400"),
    ("vehicle_plate", "\u0661\u0660\u0661\u0668\u0663\u0665\u0664"),  # Arabic-Indic digits
    ("trip_minutes", "1_6"), ("approx_started_at", "0001-01-01T00:00:00"),
    ("approx_started_at", "2026-10-05T15:00:00-06:00"),
])
def test_malformed_fields_are_named(ctx, field, value):
    client, state = ctx
    r = client.post("/api/v1/reports/discount", data={**CLAIM, field: value}, files=IMAGES)
    assert r.status_code == 422, r.text
    d = r.json()["detail"]
    assert d["error"] == "invalid_field" and field in d["fields"]
    assert state["order"] == []


def test_a_pin_needs_both_coordinates(ctx):
    client, _ = ctx
    r = client.post("/api/v1/reports/discount",
                    data={**CLAIM, "pin_start_lat": "39.75"}, files=IMAGES)
    assert r.status_code == 422 and "pin_start" in r.json()["detail"]["fields"]


def test_pins_and_approximate_time_are_stored(ctx):
    client, state = ctx
    r = client.post("/api/v1/reports/discount", data={
        **CLAIM, "pin_start_lat": "39.7511", "pin_start_lng": "-105.0012",
        "pin_end_lat": "39.7402", "pin_end_lng": "-104.9733",
        "approx_started_at": "2026-09-29T15:15:00-06:00"}, files=IMAGES)
    assert r.status_code == 200, r.text
    p = _insert(state)
    assert p[7].isoformat() == "2026-09-29T15:15:00-06:00"
    assert p[8:12] == (39.7511, -105.0012, 39.7402, -104.9733)


def test_a_failed_insert_deletes_the_stored_image(ctx, monkeypatch):
    client, state = ctx
    from contextlib import contextmanager
    from src import api_frontend_reports
    calls = {"n": 0}

    class _Boom:
        def cursor(self):
            class C:
                def execute(self_, sql, params=()):
                    if sql.lstrip().startswith("INSERT"):
                        raise RuntimeError("db down")
                def __enter__(self_): return self_
                def __exit__(self_, *a): return False
            return C()
        def commit(self): pass

    @contextmanager
    def _conn():
        calls["n"] += 1
        yield _Boom()

    monkeypatch.setattr(api_frontend_reports, "connection", _conn)
    with pytest.raises(RuntimeError):
        client.post("/api/v1/reports/discount", data=CLAIM, files=IMAGES)
    # One image now, so one delete — and it must still happen: cleanup_receipts
    # finds images only through table rows, so an orphan outlives its 18 months.
    assert state["order"].count("R2_DELETE") == 1


def test_the_legacy_shape_still_works(ctx):
    client, state = ctx
    from tests.test_discount_report_upload_order import _NOW
    r = client.post("/api/v1/reports/discount", json={
        "ride_ended_at": _NOW.isoformat(), "zone_version": "equity"})
    assert r.status_code == 200, r.text


def test_a_storage_outage_keeps_nothing(ctx, monkeypatch):
    # Replaces a test for a failure on the SECOND image: there is no second
    # image since sql/095. An R2 outage (not an unreadable file — that is the
    # next test) must leave no row and nothing in the bucket.
    client, state = ctx
    from src import api_frontend_reports

    def down(account_id, data):
        raise ConnectionError("R2 timed out")

    monkeypatch.setattr(api_frontend_reports, "store_receipt", down)
    r = client.post("/api/v1/reports/discount", data=CLAIM, files=IMAGES)
    assert r.status_code == 502 and r.json()["detail"]["error"] == "storage_unavailable"
    # Nothing was ever stored, so there is nothing to delete.
    assert state["order"] == ["RATELIMIT"]
    assert not [sql for sql, _ in state["sql"] if sql.startswith("INSERT")]


def test_an_unreadable_image_names_the_field(ctx, monkeypatch):
    client, state = ctx
    from src import api_frontend_reports
    from src.receipts import ReceiptError

    def bad(account_id, data):
        if data == b"\x89PNG-receipt":
            raise ReceiptError("upload is not a readable image")
        state["order"].append("R2_PUT")
        return "ok.png"

    monkeypatch.setattr(api_frontend_reports, "store_receipt", bad)
    r = client.post("/api/v1/reports/discount", data=CLAIM, files=IMAGES)
    assert r.status_code == 400
    assert r.json()["detail"] == {"error": "unreadable_image", "field": "receipt"}
    # Nothing stored, so nothing to clean up — and no row.
    assert state["order"] == ["RATELIMIT"]
    assert state["sql"] == []
