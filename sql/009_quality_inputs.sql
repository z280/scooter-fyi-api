-- Inputs needed by the quality_designation calculation.
--
-- max_range_meters_for_type is the per-vehicle-type rated range pulled
-- from GBFS vehicle_types.json at ingest time. Stored on each row so
-- the quality computation at /api/v1/devices/current can do
-- "≥75% of max" without needing a separate type-info lookup. NULL for
-- human-powered vehicle types (no battery, no range).
--
-- quality_designation itself is computed at QUERY time in src/quality.py
-- — not stored — so we can iterate on the rule set without re-ingesting.

ALTER TABLE raw_telemetry_points
    ADD COLUMN IF NOT EXISTS max_range_meters_for_type INTEGER;
