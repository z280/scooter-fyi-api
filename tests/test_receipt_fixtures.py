"""The receipt gold set (tests/fixtures/receipts) must stay self-consistent:
a label that does not add up would train or score a reader against a wrong
answer. Every receipt satisfies charge - discount = subtotal, subtotal + tax
= total, and the charge is Veo's base price $1 + 39c/min."""

from __future__ import annotations

import json
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

DIR = Path(__file__).resolve().parent / "fixtures" / "receipts"
LABELS = json.loads((DIR / "labels.json").read_text())["receipts"]
RECEIPTS = [r for r in LABELS if r["kind"] == "receipt"]


def test_every_labelled_file_exists():
    for r in LABELS:
        assert (DIR / r["file"]).is_file(), r["file"]


def test_receipt_arithmetic_adds_up():
    for r in RECEIPTS:
        e = r["expected"]
        assert e["charge_cents"] - e["discount_cents"] == e["subtotal_cents"], r["file"]
        assert e["subtotal_cents"] + e["tax_cents"] == e["total_cents"], r["file"]


def test_the_charge_line_is_the_base_price():
    for r in RECEIPTS:
        e = r["expected"]
        assert e["charge_cents"] == 100 + 39 * e["minutes"], r["file"]


def test_tax_is_the_legislated_rate_rounded_up():
    """The finding these receipts established (docs/PLAN_EQUITY_RECEIPTS.md, Tax)."""
    for r in RECEIPTS:
        e = r["expected"]
        tax = (Decimal(e["subtotal_cents"]) * Decimal("0.0915")).quantize(Decimal("1"), ROUND_CEILING)
        assert int(tax) == e["tax_cents"], r["file"]
