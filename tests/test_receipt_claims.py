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
IMAGES = {
    "receipt": ("r.png", b"\x89PNG-receipt", "image/png"),
    "plan_evidence": ("p.png", b"\x89PNG-plan", "image/png"),
}


def _insert(state):
    rows = [p for sql, p in state["sql"] if sql.startswith("INSERT INTO discount_reports")]
    assert len(rows) == 1, state["sql"]
    return rows[0]


def test_a_complete_claim_is_stored_with_its_arithmetic(ctx):
    client, state = ctx
    r = client.post("/api/v1/reports/discount", data=CLAIM, files=IMAGES)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "received" and body["plan_evidence_stored"] is True
    p = _insert(state)
    assert p[1] == "1018354" and p[2] and p[2] != "1018354"     # plate + its HMAC
    assert p[3:7] == (16, 500, 546, date(2026, 9, 29))
    assert p[12] == "resident"
    analysis = json.loads(p[20])
    assert (p[15], p[16], p[17]) == (308, 192, "$1 + 25c/min")
    assert (p[18], p[19]) == (46, "tax_ok")
    assert analysis["expected_cents"] == 308
    assert p[13] == "receipt.jpg" and p[14] == "receipt.jpg"   # both keys (stubbed)
    # Rate limit before either upload; both images stored.
    assert state["order"] == ["RATELIMIT", "R2_PUT", "R2_PUT"]


def test_an_unusable_claim_keeps_nothing(ctx):
    client, state = ctx
    r = client.post("/api/v1/reports/discount",
                    data={"vehicle_plate": "1018354", "charge_date": "2026-09-29"}, files=IMAGES)
    assert r.status_code == 422
    assert r.json()["detail"] == {"error": "not_rate_checkable", "missing": ["trip_minutes", "cost"]}
    assert state["order"] == [] and state["sql"] == []


def test_plan_evidence_is_required(ctx):
    client, state = ctx
    r = client.post("/api/v1/reports/discount", data=CLAIM, files={"receipt": IMAGES["receipt"]})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "plan_evidence_required"
    assert state["order"] == []


def test_the_receipt_is_required(ctx):
    client, state = ctx
    r = client.post("/api/v1/reports/discount", data=CLAIM,
                    files={"plan_evidence": IMAGES["plan_evidence"]})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "receipt_required"


@pytest.mark.parametrize("field,value", [
    ("vehicle_plate", "10183"), ("trip_minutes", "0"), ("trip_minutes", "abc"),
    ("charge_date", "2999-01-01"), ("charge_date", "2023-12-31"), ("charge_date", "29/09/2026"),
    ("declared_rate_plan", "gold"), ("total_cents", "400"),
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


def test_a_failed_insert_deletes_both_images(ctx, monkeypatch):
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
    assert state["order"].count("R2_DELETE") == 2


def test_the_legacy_shape_still_works(ctx):
    client, state = ctx
    from tests.test_discount_report_upload_order import _NOW
    r = client.post("/api/v1/reports/discount", json={
        "ride_ended_at": _NOW.isoformat(), "zone_version": "equity"})
    assert r.status_code == 200, r.text
