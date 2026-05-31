-- Per-scooter persistent state + append-only stop log.
--
-- Driven by src/device_state.py during each ingest cycle. The 48-hour
-- raw_telemetry_points table records every observation; these two tables
-- carry forward the cross-cycle identity needed to answer:
--   * how long has THIS physical scooter been at THIS spot?
--   * how many times has its bike_id rotated without it actually moving?
--   * where has THIS scooter been over the past N days?
--
-- IDENTITY MODEL ------------------------------------------------------------
-- We key everything on `vehicle_identifier` (the 16-hex-char hash of the
-- raw plate). The unhashed plate is also stored on each row so privileged
-- users can be shown the visible scooter number in admin tooling. The
-- public API exposes only the hash. See src/identity.py.

-- 1. Materialized per-device state ------------------------------------------
CREATE TABLE IF NOT EXISTS device_state (
    vehicle_identifier          TEXT PRIMARY KEY,            -- sha256(plate)[:16]
    vehicle_plate               TEXT,                        -- raw plate (internal-only)
    current_device_id           TEXT,                        -- last rotating GBFS bike_id
    current_lat                 DOUBLE PRECISION,
    current_lon                 DOUBLE PRECISION,
    current_spatial_status      TEXT,
    current_form_factor         TEXT,
    first_observed_at_location  TIMESTAMPTZ NOT NULL,        -- reset on movement
    number_failed_starts        INTEGER NOT NULL DEFAULT 0,  -- reset on movement
    first_ever_observed_at      TIMESTAMPTZ NOT NULL,        -- never reset
    last_observed_at            TIMESTAMPTZ NOT NULL,
    last_cycle_id               UUID
);
CREATE INDEX IF NOT EXISTS idx_device_state_last_observed
    ON device_state (last_observed_at DESC);

-- 2. Append-only stop log ---------------------------------------------------
-- One row is written when a scooter is first seen, and one more each time
-- it moves more than the configured stationary_threshold_meters. A scooter
-- that sits unused for a week generates exactly one row.
CREATE TABLE IF NOT EXISTS device_history (
    id                   BIGSERIAL PRIMARY KEY,
    vehicle_identifier   TEXT NOT NULL,
    vehicle_plate        TEXT,                    -- denormalized for authed reads
    cycle_id             UUID REFERENCES observation_cycles(cycle_id),
    snapshot_time        TIMESTAMPTZ NOT NULL,    -- when we observed the new position
    departed_at          TIMESTAMPTZ,             -- when this position was vacated; null = still here
    lat                  DOUBLE PRECISION NOT NULL,
    lon                  DOUBLE PRECISION NOT NULL,
    spatial_status       TEXT NOT NULL,
    form_factor          TEXT,
    device_id_observed   TEXT NOT NULL,           -- the rotating bike_id at first observation
    dwell_failed_starts  INTEGER NOT NULL DEFAULT 0   -- failed starts that occurred at this stop
);
CREATE INDEX IF NOT EXISTS idx_device_history_lookup
    ON device_history (vehicle_identifier, snapshot_time DESC);
CREATE INDEX IF NOT EXISTS idx_device_history_open_stops
    ON device_history (vehicle_identifier) WHERE departed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_device_history_snapshot_time
    ON device_history (snapshot_time DESC);
