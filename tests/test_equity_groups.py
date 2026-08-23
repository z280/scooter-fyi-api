"""The equity-group registry: the single source of truth compute.py,
daily_sla.py, and api_public.py all key off of."""

from __future__ import annotations

from src.equity_groups import (
    COMPLIANCE_GROUPS,
    OFFICIAL_GROUP,
    TRACKED_GROUPS,
    compliance_pass_column,
    core_metric_columns,
)


def test_tracked_groups_are_the_legacy_maps_the_ranks_and_the_official_map():
    assert TRACKED_GROUPS == (
        "v1", "v2", "er1", "er2", "er3", "er4", "er5", "er6", "equity",
    )


def test_official_group_is_last_so_existing_columns_keep_their_positions():
    """core_metric_columns() drives both the snapshot INSERT list and the
    positional zip in compute.run_cycle(). A group inserted anywhere but
    the END shifts every later column one place — which writes real
    numbers into the wrong columns, silently. So the official map's
    position in the tuple is itself the invariant."""
    assert TRACKED_GROUPS[-1] == OFFICIAL_GROUP
    assert OFFICIAL_GROUP == "equity"


def test_compliance_groups_are_the_official_map_plus_the_two_legacy_ones():
    """The official map is what the contract binds; v1/v2 keep their
    pass/fail flags so the pre-clarification series stays readable beside
    it. er1..er6 get metric tracking (TRACKED_GROUPS) but no boolean — no
    individual rank tier was ever itself a compliance boundary."""
    assert COMPLIANCE_GROUPS == ("v1", "v2", "equity")
    assert OFFICIAL_GROUP in COMPLIANCE_GROUPS
    assert set(COMPLIANCE_GROUPS) <= set(TRACKED_GROUPS)
    assert not any(g.startswith("er") for g in COMPLIANCE_GROUPS)


def test_core_metric_columns_has_no_duplicates():
    cols = core_metric_columns()
    assert len(cols) == len(set(cols))


def test_core_metric_columns_covers_every_group_and_metric():
    cols = set(core_metric_columns())
    for g in TRACKED_GROUPS:
        for metric in (
            "total_devices", "total_bike", "total_scooter",
            "percent_all_devices", "percent_all_bikes", "percent_all_scooters",
            "percent_bikes", "percent_scooters",
        ):
            assert f"{metric}_{g}" in cols, f"missing {metric}_{g}"


def test_core_metric_columns_includes_denver_wide_fields():
    cols = core_metric_columns()
    for base in (
        "total_devices_denver", "total_bike_denver", "total_scooter_denver",
        "total_not_in_denver", "percent_bikes_denver", "percent_scooters_denver",
    ):
        assert base in cols


def test_core_metric_columns_respects_custom_group_subset():
    cols = core_metric_columns(("er3",))
    assert "total_devices_er3" in cols
    assert "total_devices_er1" not in cols
    assert "total_devices_v1" not in cols
    # denver-wide fields are always present regardless of the group subset
    assert "total_devices_denver" in cols


def test_compliance_pass_column_naming():
    assert compliance_pass_column("v1") == "compliance_v1_pass"
    assert compliance_pass_column("er4") == "compliance_er4_pass"
    assert compliance_pass_column(OFFICIAL_GROUP) == "compliance_equity_pass"


def test_official_group_columns_match_the_migration():
    """sql/079 spells its ALTER TABLE columns out by hand; this is the
    list they must equal. A group column added in Python with no matching
    migration computes fine and then fails at INSERT time, in production,
    at 9 AM."""
    assert [c for c in core_metric_columns((OFFICIAL_GROUP,)) if c.endswith("_equity")] == [
        "total_devices_equity",
        "total_bike_equity",
        "total_scooter_equity",
        "total_sitting_equity",
        "total_standing_equity",
        "percent_all_devices_equity",
        "percent_all_bikes_equity",
        "percent_all_scooters_equity",
        "percent_all_sitting_equity",
        "percent_all_standing_equity",
        "percent_bikes_equity",
        "percent_scooters_equity",
        "percent_sitting_equity",
        "percent_standing_equity",
    ]
