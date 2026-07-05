"""Registry of equity-area boundary groups tracked in the 22-metric core
snapshot (`snapshot_metadata_core`) and the 6am-9am daily SLA compliance
window (`daily_sla_compliance`).

    v1, v2   — the legacy Disadvantaged-Areas boundary (v1 is today's
               contractual compliance metric) and its parallel-tracked
               companion. See API_REQUIREMENTS.md §1.1a.
    er1..er6 — one group per exact `EquityGroupRank` tier (1 = highest
               need) from Denver DOTI's authoritative census-block-group
               Equity Index. Tracked individually and atomically — not
               pre-combined — so that whatever cutoff DOTI eventually
               confirms as contractually authoritative can be
               reconstructed from history (e.g. rank<=2 = er1 ∪ er2)
               without having had to guess the right combination up
               front. See config.json's `boundaries` list for the
               backing GeoJSON files.

Adding a group here is the wiring that makes `compute.py` compute it and
`daily_sla.py` average it — but the Postgres columns for it
(`total_devices_<g>`, `avg_percent_all_devices_<g>`,
`compliance_<g>_pass`, etc.) must already exist (see sql/015 for
er1..er6's). Also add a matching entry to config.json's `boundaries`
list — a group with metric tracking but no boundary geometry just
computes zero for every count.

`core_metric_columns()` is the single source of truth for column names
in BOTH `snapshot_metadata_core` (src/compute.py) and the corresponding
`avg_*` fields in `daily_sla_compliance` (src/daily_sla.py) — the two
lists must stay identical since the daily SLA row is a straight average
over these same snapshot columns.
"""

from __future__ import annotations

TRACKED_GROUPS: tuple[str, ...] = ("v1", "v2", "er1", "er2", "er3", "er4", "er5", "er6")


def core_metric_columns(groups: tuple[str, ...] = TRACKED_GROUPS) -> list[str]:
    """Column names for the per-group metrics, in the fixed order used by
    both `snapshot_metadata_core` and `daily_sla_compliance` (as
    `avg_<name>`). Grouped by metric (all device totals, then all bike
    totals, ...) rather than by group, matching the existing v1/v2
    convention this generalizes."""
    cols = ["total_devices_denver"]
    cols += [f"total_devices_{g}" for g in groups]
    cols += ["total_bike_denver"]
    cols += [f"total_bike_{g}" for g in groups]
    cols += ["total_scooter_denver"]
    cols += [f"total_scooter_{g}" for g in groups]
    cols += ["total_not_in_denver"]
    cols += [f"percent_all_devices_{g}" for g in groups]
    cols += [f"percent_all_bikes_{g}" for g in groups]
    cols += [f"percent_all_scooters_{g}" for g in groups]
    cols += ["percent_bikes_denver", "percent_scooters_denver"]
    cols += [f"percent_bikes_{g}" for g in groups]
    cols += [f"percent_scooters_{g}" for g in groups]
    return cols


def compliance_pass_column(group: str) -> str:
    return f"compliance_{group}_pass"
