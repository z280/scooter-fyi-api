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
(`total_devices_<g>`, `avg_percent_all_devices_<g>`, etc.) must already
exist (see sql/015 for er1..er6's). Also add a matching entry to
config.json's `boundaries` list — a group with metric tracking but no
boundary geometry just computes zero for every count.

`core_metric_columns()` is the single source of truth for column names
in BOTH `snapshot_metadata_core` (src/compute.py) and the corresponding
`avg_*` fields in `daily_sla_compliance` (src/daily_sla.py) — the two
lists must stay identical since the daily SLA row is a straight average
over these same snapshot columns.

`COMPLIANCE_GROUPS` is a DELIBERATELY SMALLER subset: only these get a
`compliance_<g>_pass` boolean. er1..er6 are tracked as raw averages only
— no individual rank tier is itself a compliance boundary, so no
pass/fail flag is computed for one. The frontend combines whichever
er-groups make up a candidate cutoff (e.g. er1 + er2) and computes
pass/fail itself from the averages; see API_REQUIREMENTS.md §1.1a.

SPLIT DIMENSIONS -------------------------------------------------------
Every tracked group also gets a binary breakdown along each dimension in
`SPLIT_DIMENSIONS` — today: form_factor (bicycle/scooter, the original
22 RFP metrics) and vehicle_use_type (sitting/standing, added
2026-07-05). The two are independent axes: bicycle/scooter is Veo's GBFS
vocabulary (itself already corrected in places — see
src/ingest.py._KNOWN_VEHICLE_TYPES), while sitting/standing is the
accessibility-relevant distinction for compliance purposes. They happen
to agree for every vehicle observed so far, but are tracked separately
in case a future vehicle class doesn't follow the current pattern (e.g.
a seated scooter).

Adding a dimension here is the wiring that makes compute.py compute it
for every tracked group; the Postgres columns for it must already exist
(see sql/017 for the sitting/standing columns) same as for a new group.
"""

from __future__ import annotations

from dataclasses import dataclass

TRACKED_GROUPS: tuple[str, ...] = ("v1", "v2", "er1", "er2", "er3", "er4", "er5", "er6")

COMPLIANCE_GROUPS: tuple[str, ...] = ("v1", "v2")


@dataclass(frozen=True)
class SplitDimension:
    """One binary split computed for every tracked group (plus citywide).

    `db_column` is the source column on the DuckDB `points` table (and
    `raw_telemetry_points`). `name_a`/`name_b` are the metric-name
    suffixes used in `total_<name>_<g>`; `percent_name_a`/`percent_name_b`
    are the (possibly different — see form_factor's "bike" vs "bikes")
    suffixes used in the percent fields, matching the original 22 RFP
    field names exactly for the form_factor dimension.
    """
    db_column: str
    value_a: str
    name_a: str
    percent_name_a: str
    value_b: str
    name_b: str
    percent_name_b: str


SPLIT_DIMENSIONS: tuple[SplitDimension, ...] = (
    SplitDimension(
        db_column="form_factor",
        value_a="bicycle", name_a="bike", percent_name_a="bikes",
        value_b="scooter", name_b="scooter", percent_name_b="scooters",
    ),
    SplitDimension(
        db_column="vehicle_use_type",
        value_a="sitting", name_a="sitting", percent_name_a="sitting",
        value_b="standing", name_b="standing", percent_name_b="standing",
    ),
)


def core_metric_columns(groups: tuple[str, ...] = TRACKED_GROUPS) -> list[str]:
    """Column names for the per-group metrics, in the fixed order used by
    both `snapshot_metadata_core` and `daily_sla_compliance` (as
    `avg_<name>`). Grouped by metric family (all device totals, then all
    of each split dimension's totals, ...) rather than by group."""
    cols = ["total_devices_denver"]
    cols += [f"total_devices_{g}" for g in groups]
    for dim in SPLIT_DIMENSIONS:
        cols += [f"total_{dim.name_a}_denver"]
        cols += [f"total_{dim.name_a}_{g}" for g in groups]
        cols += [f"total_{dim.name_b}_denver"]
        cols += [f"total_{dim.name_b}_{g}" for g in groups]
    cols += ["total_not_in_denver"]
    cols += [f"percent_all_devices_{g}" for g in groups]
    for dim in SPLIT_DIMENSIONS:
        cols += [f"percent_all_{dim.percent_name_a}_{g}" for g in groups]
        cols += [f"percent_all_{dim.percent_name_b}_{g}" for g in groups]
    for dim in SPLIT_DIMENSIONS:
        cols += [f"percent_{dim.percent_name_a}_denver", f"percent_{dim.percent_name_b}_denver"]
        cols += [f"percent_{dim.percent_name_a}_{g}" for g in groups]
        cols += [f"percent_{dim.percent_name_b}_{g}" for g in groups]
    return cols


def compliance_pass_column(group: str) -> str:
    return f"compliance_{group}_pass"
