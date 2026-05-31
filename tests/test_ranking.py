"""Range ranking math — pure-function, no DB."""

from __future__ import annotations

from src import ranking
from src.ingest import TaggedDevice


def _dev(id_: str, range_m: int | None, form_factor: str = "scooter",
         h3_8: int = 1, h3_9: int = 1, h3_10: int = 1) -> TaggedDevice:
    return TaggedDevice(
        device_id=id_,
        vehicle_type_id="1",
        form_factor=form_factor,
        lat=39.74,
        lon=-104.99,
        spatial_status="denver_core",
        current_range_meters=range_m,
        h3_8_index=h3_8,
        h3_9_index=h3_9,
        h3_10_index=h3_10,
    )


def test_no_eligible_devices_returns_nones():
    out = ranking.compute_range_rankings([_dev("a", None), _dev("b", None)])
    for did in ("a", "b"):
        for f in (
            "range_rank_all_devices", "range_rank_all_by_type",
            "range_rank_unique_by_type", "range_percentile_by_type",
            "range_rank_h3_8_peers", "range_rank_h3_9_peers", "range_rank_h3_10_peers",
        ):
            assert out[did][f] is None


def test_single_device_alone_in_h3_10_is_one_of_one():
    """User's stated case: 'If there's no other vehicles, reporting 1/1 is fine.'"""
    out = ranking.compute_range_rankings([_dev("a", 25000)])
    assert out["a"]["range_rank_h3_10_peers"] == "1/1"
    assert out["a"]["range_rank_all_devices"] == "1/1"


def test_tie_to_top_semantics():
    """20 devices tied for highest range out of 100 → all show 100/100.
    Three devices tied at the bottom → all show 3/100 (top of THEIR tie group)."""
    # Simplified: 5 devices, 2 tied at top (range 50), 3 tied at bottom (range 10)
    devs = [
        _dev("a", 10), _dev("b", 10), _dev("c", 10),  # tied at bottom (range 10)
        _dev("d", 50), _dev("e", 50),                 # tied at top (range 50)
    ]
    out = ranking.compute_range_rankings(devs)
    # Bottom tie group (rank-ascending positions 1,2,3) → all get "3/5"
    for did in ("a", "b", "c"):
        assert out[did]["range_rank_all_devices"] == "3/5"
    # Top tie group (positions 4,5) → all get "5/5"
    for did in ("d", "e"):
        assert out[did]["range_rank_all_devices"] == "5/5"


def test_rank_unique_by_type_uses_distinct_count():
    """6 devices but only 3 unique range values → y_unique = 3."""
    devs = [
        _dev("a", 10), _dev("b", 10),
        _dev("c", 20), _dev("d", 20),
        _dev("e", 30), _dev("f", 30),
    ]
    out = ranking.compute_range_rankings(devs)
    # y in unique-by-type should be 3 (the distinct count), not 6
    for did in ("a", "b"):
        assert out[did]["range_rank_unique_by_type"] == "1/3"
    for did in ("c", "d"):
        assert out[did]["range_rank_unique_by_type"] == "2/3"
    for did in ("e", "f"):
        assert out[did]["range_rank_unique_by_type"] == "3/3"


def test_percentile_labels_for_eight_unique_values():
    """8 distinct values → quartile labels split 2/2/2/2."""
    devs = [_dev(f"d{i}", v) for i, v in enumerate([10, 20, 30, 40, 50, 60, 70, 80])]
    out = ranking.compute_range_rankings(devs)
    labels = [out[f"d{i}"]["range_percentile_by_type"] for i in range(8)]
    assert labels == ["0", "0", "25", "25", "50", "50", "75", "75"]


def test_percentile_single_value_is_top_bucket():
    """y_unique == 1: only one value exists, label it as 'best' bucket."""
    out = ranking.compute_range_rankings([_dev("a", 25000)])
    assert out["a"]["range_percentile_by_type"] == "75"


def test_by_type_groups_separately():
    """Scooter and bicycle devices form independent ranking groups."""
    devs = [
        _dev("s1", 10, form_factor="scooter"),
        _dev("s2", 20, form_factor="scooter"),
        _dev("b1", 100, form_factor="bicycle"),
        _dev("b2", 200, form_factor="bicycle"),
    ]
    out = ranking.compute_range_rankings(devs)
    # Scooter group has y=2; bicycle has y=2
    assert out["s1"]["range_rank_all_by_type"] == "1/2"
    assert out["s2"]["range_rank_all_by_type"] == "2/2"
    assert out["b1"]["range_rank_all_by_type"] == "1/2"
    assert out["b2"]["range_rank_all_by_type"] == "2/2"
    # range_rank_all_devices spans both types: 4 total
    assert out["s1"]["range_rank_all_devices"] == "1/4"
    assert out["b2"]["range_rank_all_devices"] == "4/4"


def test_h3_peer_groups_use_only_same_cell():
    """h3_10 peers ranks only against devices in the same cell."""
    devs = [
        _dev("a", 10, h3_10=100),  # alone in cell 100
        _dev("b", 20, h3_10=200),  # cell 200, two devices
        _dev("c", 30, h3_10=200),
    ]
    out = ranking.compute_range_rankings(devs)
    assert out["a"]["range_rank_h3_10_peers"] == "1/1"
    assert out["b"]["range_rank_h3_10_peers"] == "1/2"
    assert out["c"]["range_rank_h3_10_peers"] == "2/2"


def test_device_with_null_range_excluded_from_groups():
    """A device with no range data shouldn't shift other devices' y counts."""
    devs = [_dev("a", 10), _dev("b", 20), _dev("no_range", None)]
    out = ranking.compute_range_rankings(devs)
    # Only 2 eligible → y=2 in all-devices group
    assert out["a"]["range_rank_all_devices"] == "1/2"
    assert out["b"]["range_rank_all_devices"] == "2/2"
    # The null-range device gets None everywhere
    assert out["no_range"]["range_rank_all_devices"] is None
