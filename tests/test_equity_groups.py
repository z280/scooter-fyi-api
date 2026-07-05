"""The equity-group registry: the single source of truth compute.py,
daily_sla.py, and api_public.py all key off of."""

from __future__ import annotations

from src.equity_groups import TRACKED_GROUPS, compliance_pass_column, core_metric_columns


def test_tracked_groups_are_v1_v2_and_six_equity_ranks():
    assert TRACKED_GROUPS == ("v1", "v2", "er1", "er2", "er3", "er4", "er5", "er6")


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
