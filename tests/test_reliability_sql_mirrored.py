"""The signed-in reliability rule exists in two places, and must stay one rule.

`has_negative_report` is rendered twice — per device on /devices/current
(api_public.py) and per cell on the /h3 aggregate (api_h3.py). A rider who sees
a cell shaded high-risk and then taps the scooter inside it is owed the same
answer twice, so the two predicates have to agree.

This is a drift guard, not a correctness test: it asserts that both queries
carry each load-bearing clause of the rule, and that the Postgres suite's own
copy does too. `tests/test_negative_report_hold_pg.py` is what proves the rule
actually behaves, against a real database.

Cheap, and it runs everywhere — which matters because the behavioural test
skips without VEO_TEST_PG_DSN, and a skipped test guards nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: The three clauses that make the signed-in rule what it is. Each is here
#: because losing it silently changes the behaviour rather than breaking:
#:
#:   * account_id IS NOT NULL — drop it and every anonymous report becomes
#:     permanent, which is the stalkable-by-strangers version of this feature.
#:   * first_observed_at_location <= dr.reported_at — drop it and a report
#:     never clears, so a repaired scooter stays condemned forever.
#:   * current_range_meters < range_at_report + rise — drop it and a
#:     recharged scooter does too. It is a RISE test against the reading
#:     stored with the report (sql/100), never a level: the level test it
#:     replaced cleared a report on a 100% scooter the moment it was filed.
#:   * range_at_report_meters IS NULL — a report with no reading must hold.
#:   * resolved_at IS NULL — drop it and an admin's void does nothing.
CLAUSES = (
    "dr.account_id IS NOT NULL",
    "ds.first_observed_at_location <= dr.reported_at",
    "r.current_range_meters < dr.range_at_report_meters +",
    "dr.range_at_report_meters IS NULL",
    "dr.resolved_at IS NULL",
)

SOURCES = (
    "src/api_public.py",
    "src/api_h3.py",
    "tests/test_negative_report_hold_pg.py",
    # The suppression flag uses the same hold rule (minus the reliability type
    # filter) — a vehicle must not be hidden by a report the tier has already
    # cleared, or vice versa.
    "src/fleet_reports.py",
)


def test_no_rendering_still_uses_the_full_charge_level_test() -> None:
    # docs/FLEET_REPORTS_PLAN.md §2.4. `current_range_meters < <full>` made a
    # fully charged scooter unreportable.
    for rel in SOURCES:
        text = " ".join((ROOT / rel).read_text().split())
        assert "r.current_range_meters < %(full)s" not in text, rel
        assert "OR r.current_range_meters < %s)" not in text, rel


@pytest.mark.parametrize("rel", SOURCES)
@pytest.mark.parametrize("clause", CLAUSES)
def test_every_rendering_carries_the_clause(rel: str, clause: str) -> None:
    text = (ROOT / rel).read_text()
    # Whitespace is normalised because one copy is a Python-concatenated
    # string and another is a triple-quoted block with different indentation.
    assert clause in " ".join(text.split()), f"{rel} has lost: {clause}"


def test_the_anonymous_window_is_still_24_hours() -> None:
    # The other half of the trade. If this disappears, anonymous reports have
    # quietly inherited the signed-in rule — which would make a report from
    # nobody-in-particular permanent.
    for rel in ("src/api_public.py", "src/api_h3.py"):
        text = (ROOT / rel).read_text()
        assert "INTERVAL '24 hours'" in text, rel


def test_the_full_charge_threshold_has_one_definition() -> None:
    # Two constants for "100%" would be two definitions that drift the first
    # time the vendor's lookup table does.
    from src.quality import _soc_lut, compute_battery_percent, full_charge_range_meters

    lut = _soc_lut()
    full = full_charge_range_meters()
    assert full == lut[-1]
    assert compute_battery_percent(full) == 100
    # The value directly below it IN THE TABLE reads 99%. Asserted against the
    # table rather than `full - 1`: a range that is not in the table falls
    # through to linear scaling and rounds to 100 a little below the top, and
    # the threshold deliberately does not accept that — see
    # `full_charge_range_meters`'s own note on which way to err.
    assert compute_battery_percent(lut[-2]) == 99
