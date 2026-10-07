"""Equity receipt claims, Phase 1: what a submission must carry, and the
arithmetic computed the moment it arrives.

docs/PLAN_EQUITY_RECEIPTS.md is the spec; the owner's decisions (2026-10-06/07)
are recorded there. The rules this module holds:

  * THE GATE. A rate error can only be shown with the scooter code (the
    plate), the trip minutes and a cost (pre-tax or with-tax), plus the charge
    date the receipt prints. All four are on every Veo receipt. Anything less
    is declined and NOTHING is kept: no row, no image (the endpoint checks the
    gate before any upload).

  * NO LOCATION, NO TIME OF DAY. A Veo receipt has neither. Where the ride
    happened comes later, from matching the plate + charge date + duration
    against feed history (Phase 2). The rider's pins and approximate start
    time are optional tie-breakers, never the evidence.

  * ARITHMETIC IS RECORDED, NOT JUDGED. The rate signature, the equity price
    for the minutes and the tax check are stored at submission so Phase 3 can
    decide against them; nothing here awards points or tells a rider they were
    overcharged.

Pure functions only, so every rule is testable without a request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

#: Exhibit C "Equity Area Pricing": $1 unlock + $0.13/min, whatever the tier
#: (Exhibit A §5.2).
EQUITY_UNLOCK_CENTS = 100
EQUITY_PER_MIN_CENTS = 13

#: The frontend's rate-plan keys (src/config.ts RATE_PLANS) plus "not sure".
#: Finer than the profile's stored enum on purpose: a VeoPlus Pass changes the
#: unlock, and the stacked "free unlocks + 25¢" plan is itself worth counting
#: (the owner's "social experiment").
DECLARED_RATE_PLANS = (
    "resident", "resident_plus", "visitor", "visitor_plus", "equity", "unknown",
)

#: Receipts print the raw plate: 7 digits today (101…/102…/103…); room for
#: growth. Spaces are stripped before the check.
_PLATE = re.compile(r"^\d{7,10}$")

MAX_TRIP_MINUTES = 600

#: Per-minute rates Veo publishes in Denver: Equity Area 13¢, Access 15¢,
#: Resident 25¢, Visitor/standard 39¢.
KNOWN_PER_MIN_CENTS = frozenset({13, 15, 25, 39})

#: Before the feed's history starts nothing can be matched, but the claim is
#: still a receipt; the floor only rejects nonsense dates.
EARLIEST_CHARGE_DATE = date(2024, 1, 1)


def normalize_plate(raw: str | None) -> str | None:
    """The plate as digits, or None if it cannot be one."""
    if raw is None:
        return None
    digits = re.sub(r"\s+", "", str(raw))
    return digits if _PLATE.fullmatch(digits) else None


@dataclass(frozen=True)
class Claim:
    plate: str | None
    trip_minutes: int | None
    subtotal_cents: int | None
    total_cents: int | None
    charge_date: date | None


def missing_for_rate_check(c: Claim) -> list[str]:
    """What the gate needs and the claim lacks. Empty = rate-checkable."""
    missing = []
    if not c.plate:
        missing.append("vehicle_plate")
    if c.trip_minutes is None:
        missing.append("trip_minutes")
    if c.subtotal_cents is None and c.total_cents is None:
        missing.append("cost")
    if c.charge_date is None:
        missing.append("charge_date")
    return missing


def equity_price_cents(minutes: int) -> int:
    """What Exhibit C says the ride costs before tax."""
    return EQUITY_UNLOCK_CENTS + EQUITY_PER_MIN_CENTS * minutes


def rate_signatures(subtotal_cents: int, minutes: int) -> list[str]:
    """Every "$U + Nc/min" (unlock $0 or $1, whole cents per minute) that
    produces this subtotal exactly. Usually one; none means the subtotal is
    not a simple unlock + per-minute price (e.g. an Access day past the free
    hour, or a misread)."""
    if minutes <= 0 or subtotal_cents < 0:
        return []
    fits = []
    for unlock in (100, 0):
        rest = subtotal_cents - unlock
        if rest >= 0 and rest % minutes == 0 and rest // minutes <= 100:
            fits.append((unlock, rest // minutes))
    # A short ride can fit two readings ($2.00 for 4 min is $1 + 25c or
    # $0 + 50c). When exactly one uses a per-minute rate Veo actually
    # publishes, that is the reading; otherwise every fit is reported.
    known = [f for f in fits if f[1] in KNOWN_PER_MIN_CENTS]
    if len(known) == 1:
        fits = known
    return [f"${u // 100} + {m}c/min" for u, m in fits]


def tax_finding(subtotal_cents: int, total_cents: int, rate: float) -> dict:
    """Check the receipt's tax (total - subtotal) against the legislated rate.

    tax_ok          matches round-half-up, the legal rule as reported
    tax_rounded_up  matches only rounding UP to the next cent (1¢ over)
    tax_unexplained matches neither; the implied rate is recorded
    """
    tax = total_cents - subtotal_cents
    exact = Decimal(subtotal_cents) * Decimal(str(rate))
    half_up = int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    ceiling = int(exact.quantize(Decimal(1), rounding=ROUND_CEILING))
    if tax == half_up:
        finding = "tax_ok"
    elif tax == ceiling:
        finding = "tax_rounded_up"
    else:
        finding = "tax_unexplained"
    return {
        "finding": finding,
        "tax_cents": tax,
        "expected_half_up_cents": half_up,
        "excess_cents": tax - half_up,
        "implied_rate": round(tax / subtotal_cents, 5) if subtotal_cents else None,
    }


def arithmetic(c: Claim, tax_rate: float) -> dict:
    """Everything computed at submission. The charged subtotal is the
    receipt's own when given, else backed out of the total at the legislated
    rate (flagged as derived, because rounding makes that ±1¢)."""
    assert c.trip_minutes is not None
    expected = equity_price_cents(c.trip_minutes)
    subtotal = c.subtotal_cents
    derived = False
    if subtotal is None and c.total_cents is not None:
        subtotal = int((Decimal(c.total_cents) / (1 + Decimal(str(tax_rate))))
                       .quantize(Decimal(1), rounding=ROUND_HALF_UP))
        derived = True
    out = {
        "expected_cents": expected,
        "charged_subtotal_cents": subtotal,
        "subtotal_derived_from_total": derived,
        "rate_error_cents": subtotal - expected if subtotal is not None else None,
        "rate_signature": None,
        "tax": None,
    }
    if subtotal is not None and not derived:
        sigs = rate_signatures(subtotal, c.trip_minutes)
        out["rate_signature"] = " or ".join(sigs) if sigs else None
    if c.subtotal_cents is not None and c.total_cents is not None:
        out["tax"] = tax_finding(c.subtotal_cents, c.total_cents, tax_rate)
    return out
