"""Per-cycle range rankings for every device.

Computed in Python after the ingest envelope-tag step and before
COPY-writing raw_telemetry_points. Cheap — O(N log N) sorts over ~8k
rows per cycle, runs in < 50 ms.

Seven rank fields per device:

| field                          | comparison group     | value semantics |
|--------------------------------|----------------------|-----------------|
| range_rank_all_devices         | all eligible devices | "x/y", tie→top   |
| range_rank_all_by_type         | same form_factor     | "x/y", tie→top   |
| range_rank_unique_by_type      | unique values, type  | "x/y", no ties  |
| range_percentile_by_type       | unique values, type  | "0"/"25"/"50"/"75" |
| range_rank_h3_8_peers          | same h3_8 cell       | "x/y", tie→top   |
| range_rank_h3_9_peers          | same h3_9 cell       | "x/y", tie→top   |
| range_rank_h3_10_peers         | same h3_10 cell      | "x/y", tie→top   |

Tie semantics ("tie→top"): when several devices share the same range
value, every member of the tie group gets the rank of the LAST member
when sorted ascending. So 20 devices tied for the highest range in a
fleet of 100 all show "100/100".

Devices with current_range_meters IS NULL are excluded from every
group AND get None for every field.
"""

from __future__ import annotations

from typing import Iterable

from .ingest import TaggedDevice


_FIELDS = (
    "range_rank_all_devices",
    "range_rank_all_by_type",
    "range_rank_unique_by_type",
    "range_percentile_by_type",
    "range_rank_h3_8_peers",
    "range_rank_h3_9_peers",
    "range_rank_h3_10_peers",
)


def _rank_tie_to_top(group: list[TaggedDevice]) -> dict[str, str]:
    """Rank each device in the group by ascending range. Ties get the
    HIGHEST position in the tied group ("20 tied at top → all show y/y").

    Returns {device_id: "x/y"}. Empty input → empty dict.
    """
    if not group:
        return {}
    sorted_g = sorted(group, key=lambda d: (d.current_range_meters, d.device_id))
    last_pos_for_val: dict[int, int] = {}
    for pos, d in enumerate(sorted_g, start=1):
        last_pos_for_val[d.current_range_meters] = pos  # overwrite ⇒ last wins
    y = len(group)
    return {
        d.device_id: f"{last_pos_for_val[d.current_range_meters]}/{y}"
        for d in group
    }


def _percentile_label(rank_in_unique: int, y_unique: int) -> str:
    """Map a (rank, y_unique) into one of "0"/"25"/"50"/"75".

    Quartile by position in the sorted distinct-value list:
        bucket = floor((rank - 1) / y * 4), clamped to [0, 3]
    With y_unique == 1, returns "75" (the only value is also the best).
    """
    if y_unique <= 1:
        return "75"
    bucket = int((rank_in_unique - 1) / y_unique * 4)
    if bucket > 3:
        bucket = 3
    return ["0", "25", "50", "75"][bucket]


def compute_range_rankings(
    devices: Iterable[TaggedDevice],
) -> dict[str, dict[str, str | None]]:
    """Return {device_id: {field_name: value_or_None}} for all 7 ranking
    fields. Devices without current_range_meters get None for every field."""
    devices = list(devices)
    out: dict[str, dict[str, str | None]] = {
        d.device_id: {f: None for f in _FIELDS} for d in devices
    }

    eligible = [d for d in devices if d.current_range_meters is not None]
    if not eligible:
        return out

    # range_rank_all_devices ---------------------------------------------------
    for did, val in _rank_tie_to_top(eligible).items():
        out[did]["range_rank_all_devices"] = val

    # range_rank_all_by_type + unique + percentile ----------------------------
    by_type: dict[str, list[TaggedDevice]] = {}
    for d in eligible:
        by_type.setdefault(d.form_factor, []).append(d)

    for grp in by_type.values():
        for did, val in _rank_tie_to_top(grp).items():
            out[did]["range_rank_all_by_type"] = val

        unique_sorted = sorted({d.current_range_meters for d in grp})
        y_unique = len(unique_sorted)
        val_to_unique_rank = {v: i + 1 for i, v in enumerate(unique_sorted)}
        for d in grp:
            r = val_to_unique_rank[d.current_range_meters]
            out[d.device_id]["range_rank_unique_by_type"] = f"{r}/{y_unique}"
            out[d.device_id]["range_percentile_by_type"] = _percentile_label(r, y_unique)

    # range_rank_h3_{8,9,10}_peers --------------------------------------------
    for res, field in (
        (8, "range_rank_h3_8_peers"),
        (9, "range_rank_h3_9_peers"),
        (10, "range_rank_h3_10_peers"),
    ):
        attr = f"h3_{res}_index"
        by_cell: dict[int, list[TaggedDevice]] = {}
        for d in eligible:
            cell = getattr(d, attr)
            if cell is None:
                continue
            by_cell.setdefault(cell, []).append(d)
        for grp in by_cell.values():
            for did, val in _rank_tie_to_top(grp).items():
                out[did][field] = val

    return out
