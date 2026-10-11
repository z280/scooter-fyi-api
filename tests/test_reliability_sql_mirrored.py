"""The negative-report rule exists ONCE, and every rendering uses it.

`has_negative_report` / the reliability label is rendered per device on
/devices/current (api_public.py) and per cell on the /h3 aggregate
(api_h3.py); the identify path, the condition checks and the admin pages read
the same rule through src/fleet_reports.py. A rider who sees a cell shaded
high-risk and then taps the scooter inside it is owed the same answer twice.

Until 2026-10-09 the predicate was copied into each file and these tests
checked the copies agreed. The owner's rules (move >= 100 m + charge rise,
off-the-map + full battery, anonymous fade to unknown) are now built once,
in fleet_reports.uncleared_negative_sql, and this guard checks that every
consumer embeds the builder rather than a copy, and that the builder carries
each load-bearing clause. tests/test_negative_report_hold_pg.py proves the
rule behaves against a real database.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import fleet_reports

ROOT = Path(__file__).resolve().parents[1]

CONSUMERS = ("src/api_public.py", "src/api_h3.py",
             "tests/test_negative_report_hold_pg.py")

#: Each one silently changes behaviour if lost:
#:   * the 100 m straight-line move ("a move of <100m should not reset");
#:   * the RISE over the baseline charge (never a level — §2.4);
#:   * the off-the-map path through device_history's 'absent' stops;
#:   * the baseline override a reconfirmation writes, and its pending hold;
#:   * resolution (admin / rider check) — the default filter;
#:   * the anonymous 24 h high-risk window.
CLAUSES = (
    f"geo_distance_m(n.base_lat, n.base_lon, ds.current_lat, ds.current_lon) >= "
    f"{fleet_reports.CLEAR_MOVE_METERS}",
    f">= n.base_range + {fleet_reports.charge_rise_meters()}",
    "h.departure_reason = 'absent'",
    "COALESCE(dr.baseline_lat, dr.vehicle_lat_at_report)",
    "COALESCE(dr.baseline_range_meters, dr.range_at_report_meters)",
    "WHERE n.pending OR NOT",
    "AND dr.resolved_at IS NULL",
    f"INTERVAL '{fleet_reports.ANONYMOUS_HIGH_RISK_HOURS} hours'",
    # sql/104: servicing in the history, and the legacy 3-move clear.
    "ds.last_serviced_at > n.base_at",
    f"n.reported_at < TIMESTAMPTZ '{fleet_reports.BATTERY_CAPTURE_SINCE}'",
    f"LIMIT {fleet_reports.LEGACY_CLEAR_MOVES}) mv) >= {fleet_reports.LEGACY_CLEAR_MOVES}",
    # sql/106: the settled reading, and a completed depot visit (plan D1).
    "THEN GREATEST(ds.settled_range_meters,",
    "AND dv.entered_at > n.base_at AND dv.exited_at IS NOT NULL",
)


def _builder_sql() -> str:
    return " ".join(fleet_reports.uncleared_negative_sql(
        vid="r.vehicle_identifier", current_range="r.current_range_meters",
        now="NOW()").split())


@pytest.mark.parametrize("clause", CLAUSES)
def test_the_builder_carries_the_clause(clause: str) -> None:
    assert clause in _builder_sql(), clause


@pytest.mark.parametrize("rel", CONSUMERS)
def test_every_consumer_embeds_the_builder(rel: str) -> None:
    text = (ROOT / rel).read_text()
    if rel in ("src/api_public.py", "src/api_h3.py"):
        # /devices/current reads every vehicle's state from ONE pass over the
        # builder (fleet_reports.negative_states), never a per-row subquery:
        # that subquery cost ~2.2 s of every map load (2026-10-09).
        assert "fleet_reports.negative_states(" in text, rel
        assert "negative_state_sql(" not in text, rel
        return
    assert "negative_state_sql(" in text, rel


def test_negative_states_pass_is_built_from_the_builder() -> None:
    """The single pass /devices/current uses must itself come from the
    builder, so it can never drift from the rules."""
    src = (ROOT / "src/fleet_reports.py").read_text()
    body = src[src.index("def _fleet_rows_sql"):]
    body = body[:body.index("\ndef ", 10)]
    assert "uncleared_negative_sql(" in body or "negative_state_sql(" in body


@pytest.mark.parametrize("rel", CONSUMERS + ("src/fleet_reports.py",))
def test_no_copy_of_a_retired_rule_survives(rel: str) -> None:
    text = " ".join((ROOT / rel).read_text().split())
    # The stationary-threshold "moved" clear (first_observed_at_location
    # against the report), the 24 h h3-cell anonymous expiry, the charge-
    # rise-alone clear and the full-charge LEVEL test are all retired.
    assert "first_observed_at_location <= dr.reported_at" not in text, rel
    assert "nr.h3_10_index = r.h3_10_index" not in text, rel
    assert "dr.h3_10_index = r.h3_10_index" not in text, rel
    assert "r.current_range_meters < dr.range_at_report_meters" not in text, rel
    assert "r.current_range_meters < %(full)s" not in text, rel


def test_improperly_parked_is_the_only_non_negative_type() -> None:
    from src.api_frontend_reports import NON_RELIABILITY_REPORT_TYPES, _REPORT_TYPES

    assert NON_RELIABILITY_REPORT_TYPES == ("improperly_parked",)
    assert fleet_reports.NON_NEGATIVE_REPORT_TYPES == ("improperly_parked",)
    assert set(fleet_reports.NEGATIVE_REPORT_PRIORITY) | {"improperly_parked"} == set(
        _REPORT_TYPES)
    assert "improperly_parked" not in fleet_reports.uncleared_negative_sql(
        vid="v", current_range="c", now="NOW()")
