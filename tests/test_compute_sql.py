"""Synthetic-payload regression for the DuckDB spatial join.

Loads the real boundary files and verifies that hand-placed points produce
the expected core counts and percentages. Skips when boundary files are
missing (so this can run outside the container too — we resolve from the
repo's local data/ instead of /app/data/).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import uuid

import pytest

from src import compute
from src.config import BoundaryLayer
from src.ingest import IngestPayload, TaggedDevice


REPO_DATA = Path(__file__).resolve().parents[1] / "data"


def _local_boundaries() -> tuple[BoundaryLayer, ...]:
    """Same shape as the production layers, but pointing at the repo's data/."""
    return (
        BoundaryLayer(
            region_category="disadvantaged_areas",
            region_type="v1",
            file=str(REPO_DATA / "v1.json"),
            name_prefix="V1_",
            name_strategy="ordinal",
            name_field=None,
        ),
        BoundaryLayer(
            region_category="disadvantaged_areas",
            region_type="v2",
            file=str(REPO_DATA / "v2.json"),
            name_prefix="V2_",
            name_strategy="field",
            name_field="GEOID20",
        ),
        BoundaryLayer(
            region_category="council_districts",
            region_type="council_district",
            file=str(REPO_DATA / "CD.geojson"),
            name_prefix="CD_",
            name_strategy="field",
            name_field="DIST_NUM",
            filter_nonnull_field="DIST_NUM",
        ),
        BoundaryLayer(
            region_category="community_networks",
            region_type="community_network",
            file=str(REPO_DATA / "CN.geojson"),
            name_prefix="CN_",
            name_strategy="field_alnum",
            name_field="CN_NAME",
        ),
        BoundaryLayer(
            region_category="neighborhoods",
            region_type="neighborhood",
            file=str(REPO_DATA / "NB.geojson"),
            name_prefix="NB_",
            name_strategy="field_alnum",
            name_field="NBHD_NAME",
        ),
    )


def _er_boundaries() -> tuple[BoundaryLayer, ...]:
    """v1/v2 plus all six equity-rank layers — same convention as v2."""
    layers = list(_local_boundaries())
    for n in range(1, 7):
        layers.append(BoundaryLayer(
            region_category="disadvantaged_areas",
            region_type=f"er{n}",
            file=str(REPO_DATA / f"er{n}.json"),
            name_prefix=f"ER{n}_",
            name_strategy="field",
            name_field="GEOID20",
        ))
    return tuple(layers)


def _patched_config(boundaries):
    import src.config
    fake_cfg = src.config.load()
    return src.config.AppConfig(
        gbfs=fake_cfg.gbfs,
        schedule=fake_cfg.schedule,
        denver_core=fake_cfg.denver_core,
        china_glitch=fake_cfg.china_glitch,
        boundaries=boundaries,
        transmission_endpoints=fake_cfg.transmission_endpoints,
        cors_origins=fake_cfg.cors_origins,
        cors_origin_patterns=fake_cfg.cors_origin_patterns,
        r2=fake_cfg.r2,
        auth=fake_cfg.auth,
        map_auth=fake_cfg.map_auth,
        device_tracking=fake_cfg.device_tracking,
        log_level=fake_cfg.log_level,
    )


def _devices() -> list[TaggedDevice]:
    # Hand-chosen points: 5 scooters and 3 bikes inside Denver, 1 China glitch
    # The Denver core coordinates are inside the city's actual footprint.
    inside = [
        # Capitol Hill area, ~39.737, -104.978
        ("s1", "scooter", 39.7372, -104.9785),
        ("s2", "scooter", 39.7400, -104.9810),
        # Central Park / Stapleton area
        ("s3", "scooter", 39.7700, -104.8900),
        # Five Points
        ("s4", "scooter", 39.7570, -104.9760),
        # Far Southeast (deliberately outside opportunity areas)
        ("s5", "scooter", 39.6300, -104.9000),
        ("b1", "bicycle", 39.7360, -104.9900),
        ("b2", "bicycle", 39.7530, -104.9600),
        ("b3", "bicycle", 39.7200, -104.9500),
    ]
    devices = [
        TaggedDevice(
            device_id=did, vehicle_type_id=None, form_factor=ff,
            lat=lat, lon=lon, spatial_status="denver_core",
        )
        for did, ff, lat, lon in inside
    ]
    devices.append(
        TaggedDevice(
            device_id="x1", vehicle_type_id="1", form_factor="scooter",
            lat=22.5, lon=114.0, spatial_status="china_glitch",
        )
    )
    # Repair-shop-style false positive: inside the rough Denver bbox
    # (39.6-39.9 N, -105.2 to -104.6 E) but well outside the actual city
    # polygon. Coordinate is ~Centennial / Greenwood Village area, south of
    # the southern Denver border. Should be re-tagged other_outlier by the
    # polygon refinement step in compute._refine_spatial_status.
    devices.append(
        TaggedDevice(
            device_id="repair", vehicle_type_id="1", form_factor="scooter",
            lat=39.6100, lon=-104.8700, spatial_status="denver_core",
        )
    )
    return devices


@pytest.mark.skipif(
    not (REPO_DATA / "v1.json").exists(),
    reason="v1.json missing",
)
def test_core_totals_match_hand_counts(monkeypatch):
    # Patch the boundary list to use repo-relative paths. Deliberately
    # excludes er1..er6 — proves a tracked group absent from the DuckDB
    # `boundaries` table yields zero (not an error). See
    # test_core_totals_include_all_tracked_equity_groups below for the
    # full-boundary-set case.
    import src.config
    patched = _patched_config(_local_boundaries())
    monkeypatch.setattr(src.config, "load", lambda: patched)
    # Also patch the reference inside the compute module
    monkeypatch.setattr(compute, "load", lambda: patched)

    cycle_id = uuid.uuid4()
    snap = datetime.now(timezone.utc)
    payload = IngestPayload(
        last_updated=1700000000,
        payload_sha256="testhash",
        devices=_devices(),
        raw_count=len(_devices()),
    )

    # Run compute (DuckDB only — no Postgres write)
    result = compute.run_cycle(cycle_id, payload, snap)

    core = result.core_row
    # 10 devices total: 8 actually inside the city polygon, 1 china_glitch,
    # and 1 "repair shop" that's inside the rough bbox but outside the
    # neighborhood union. Polygon refinement must drop the repair-shop
    # device from the Denver count.
    assert core["total_devices_denver"] == 8
    assert core["total_not_in_denver"] == 2   # china_glitch + repair shop
    assert core["total_bike_denver"] == 3
    assert core["total_scooter_denver"] == 5

    # The repair-shop device must be flagged 'other_outlier' in the raw rows.
    repair_row = next(r for r in result.raw_rows if r["device_id"] == "repair")
    assert repair_row["spatial_status"] == "other_outlier", (
        "polygon refinement should re-tag the bbox-but-not-city device"
    )
    # And the china_glitch tag should survive untouched.
    china_row = next(r for r in result.raw_rows if r["device_id"] == "x1")
    assert china_row["spatial_status"] == "china_glitch"

    # The percentages should be sane
    assert core["percent_bikes_denver"] is not None
    assert 0 <= core["percent_bikes_denver"] <= 100

    # Regional rows: at least one per boundary feature
    types = {r["region_type"] for r in result.regional_rows}
    assert {"v1", "v2", "council_district", "community_network", "neighborhood"} <= types

    # The CD At-Large filter should keep CD output at 11 numbered districts
    cd_names = {r["region_name"] for r in result.regional_rows if r["region_type"] == "council_district"}
    assert len(cd_names) == 11
    assert all(n.startswith("CD_") for n in cd_names)


@pytest.mark.skipif(
    not (REPO_DATA / "er1.json").exists(),
    reason="er1.json missing",
)
def test_core_totals_include_all_tracked_equity_groups(monkeypatch):
    """With er1..er6 present in the boundary set, run_cycle's core_row
    must carry every column equity_groups.core_metric_columns() promises
    — this is the thing that would silently break if the SQL SELECT list
    and the Python column list it's zipped against ever drifted apart."""
    import src.config
    from src.equity_groups import TRACKED_GROUPS, core_metric_columns

    patched = _patched_config(_er_boundaries())
    monkeypatch.setattr(src.config, "load", lambda: patched)
    monkeypatch.setattr(compute, "load", lambda: patched)

    cycle_id = uuid.uuid4()
    snap = datetime.now(timezone.utc)
    payload = IngestPayload(
        last_updated=1700000000,
        payload_sha256="testhash",
        devices=_devices(),
        raw_count=len(_devices()),
    )

    result = compute.run_cycle(cycle_id, payload, snap)
    core = result.core_row

    for col in core_metric_columns():
        assert col in core, f"missing {col} in core_row"

    # Every device inside Denver lands in exactly one census block group
    # (the six ranks partition the scored area), so the ranks can't
    # collectively exceed the Denver total.
    er_total = sum(core[f"total_devices_er{n}"] for n in range(1, 7))
    assert 0 <= er_total <= core["total_devices_denver"]

    # At least one of our hand-placed points should land in some rank —
    # otherwise the er1..er6 CTEs are silently matching nothing.
    assert er_total > 0

    # Regional rows should include all six er layers too (generic
    # per-layer breakdown, unrelated to the core-snapshot wiring above).
    types = {r["region_type"] for r in result.regional_rows}
    assert {f"er{n}" for n in range(1, 7)} <= types
