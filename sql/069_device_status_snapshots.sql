-- Per-cycle fleet status snapshot — the history behind the Tools drawer's
-- "Devices over time" chart.
--
-- WHY A NEW TABLE ------------------------------------------------------------
-- Nothing durable records the fleet's AVAILABILITY over time:
-- snapshot_metadata_core has per-cycle totals (and always has — which is
-- what lets the endpoint backfill the total line for history predating
-- this migration), but is_reserved / is_disabled / the model mix live only
-- in raw_telemetry_points, which the 48-hour R2 flush truncates. One
-- compact row per cycle keeps the status/model breakdown queryable for the
-- 14-day hourly chart at ~1/1000th of the raw table's weight.
--
-- SCOPE: Denver-core devices only (the polygon-corrected spatial_status),
-- matching total_devices_denver's own scope so the backfilled total line
-- and the new rows agree on what "a device" is.
--
-- SEMANTICS: out_of_service = is_disabled (disabled wins over reserved,
-- per GBFS); reserved = is_reserved and not disabled; available = neither.
-- Feeds that omit the booleans (both are nullable at ingest) count the
-- device as available — the same reading the live map takes.
--
-- models carries the SAME three status counts per model —
--   {"Astro": {"available": 200, "reserved": 10, "out_of_service": 5}, …}
-- — so every metric can be broken down by model, and the top-level
-- columns are exactly the per-model sums. Keys are the feed's own display
-- names — server truth, no client-side mapping to drift, and a new model
-- simply appears. A model's total is derivable (the three counts summed),
-- so it is not stored.
--
-- Retention: the writer prunes rows older than 30 days each cycle (the
-- chart needs 14; double covers clock skew and future ranges) — cheap
-- with the snapshot_time index.

CREATE TABLE IF NOT EXISTS device_status_snapshots (
    cycle_id           UUID PRIMARY KEY REFERENCES observation_cycles(cycle_id),
    snapshot_time      TIMESTAMPTZ NOT NULL,
    total              INTEGER NOT NULL,
    available          INTEGER NOT NULL,
    reserved           INTEGER NOT NULL,
    out_of_service     INTEGER NOT NULL,
    models             JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_device_status_snapshots_time
    ON device_status_snapshots (snapshot_time DESC);
