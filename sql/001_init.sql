-- v3.2 schema — narrow tables only, no PostGIS.
-- All migrations are idempotent (CREATE TABLE IF NOT EXISTS) so they may
-- safely re-run on every boot. src/pg.py also tracks application via
-- schema_migrations, but the IF NOT EXISTS guard is a belt-and-suspenders.

CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 1. Lifecycle state machine -------------------------------------------------
CREATE TABLE IF NOT EXISTS observation_cycles (
    cycle_id                  UUID PRIMARY KEY,
    start_ts                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    data_received_ts          TIMESTAMPTZ,
    processing_complete_ts    TIMESTAMPTZ,
    data_storage_complete_ts  TIMESTAMPTZ,
    transmission_ts           TIMESTAMPTZ,
    transmission_status       TEXT CHECK (transmission_status IN ('complete', 'failure', 'partial_failure')),
    job_status                TEXT CHECK (job_status IN ('in_progress', 'complete', 'upstream_failure', 'internal_failure', 'stale_aborted')),
    errors                    TEXT,
    data_json_blob            JSONB,
    gbfs_last_updated         BIGINT,        -- raw GBFS last_updated epoch from this cycle
    gbfs_payload_sha256       TEXT           -- fallback signature when last_updated is unreliable
);
CREATE INDEX IF NOT EXISTS idx_obscycle_start_ts ON observation_cycles (start_ts DESC);
CREATE INDEX IF NOT EXISTS idx_obscycle_job_status ON observation_cycles (job_status);

-- 2. Upstream API failures ---------------------------------------------------
CREATE TABLE IF NOT EXISTS api_failures (
    id                SERIAL PRIMARY KEY,
    cycle_id          UUID REFERENCES observation_cycles(cycle_id),
    attempt_time      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    failure_type      TEXT NOT NULL CHECK (failure_type IN ('unavailable', 'stale_data', 'timeout', 'malformed_payload', 'archive_upload_failed')),
    http_status_code  INTEGER,
    error_details     TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_failures_cycle ON api_failures (cycle_id);
CREATE INDEX IF NOT EXISTS idx_api_failures_time ON api_failures (attempt_time DESC);

-- 3. Raw point retention (48-hour rolling buffer) ----------------------------
CREATE TABLE IF NOT EXISTS raw_telemetry_points (
    id              BIGSERIAL PRIMARY KEY,
    cycle_id        UUID REFERENCES observation_cycles(cycle_id),
    snapshot_time   TIMESTAMPTZ NOT NULL,
    device_id       VARCHAR(64) NOT NULL,
    form_factor     TEXT NOT NULL,
    latitude        NUMERIC(9,6) NOT NULL,
    longitude       NUMERIC(9,6) NOT NULL,
    spatial_status  TEXT NOT NULL CHECK (spatial_status IN ('denver_core', 'china_glitch', 'other_outlier'))
);
CREATE INDEX IF NOT EXISTS idx_raw_spatial_status ON raw_telemetry_points (spatial_status);
CREATE INDEX IF NOT EXISTS idx_raw_cycle ON raw_telemetry_points (cycle_id);
CREATE INDEX IF NOT EXISTS idx_raw_snapshot_time ON raw_telemetry_points (snapshot_time DESC);

-- 4. Core citywide summary metrics (the 22 RFP data points) ------------------
CREATE TABLE IF NOT EXISTS snapshot_metadata_core (
    cycle_id                    UUID PRIMARY KEY REFERENCES observation_cycles(cycle_id),
    snapshot_time               TIMESTAMPTZ NOT NULL,
    total_devices_denver        INTEGER,
    total_devices_v1            INTEGER,
    total_devices_v2            INTEGER,
    total_bike_denver           INTEGER,
    total_bike_v1               INTEGER,
    total_bike_v2               INTEGER,
    total_scooter_denver        INTEGER,
    total_scooter_v1            INTEGER,
    total_scooter_v2            INTEGER,
    total_not_in_denver         INTEGER,
    percent_all_devices_v1      NUMERIC(5,2),
    percent_all_devices_v2      NUMERIC(5,2),
    percent_all_bikes_v1        NUMERIC(5,2),
    percent_all_bikes_v2        NUMERIC(5,2),
    percent_all_scooters_v1     NUMERIC(5,2),
    percent_all_scooters_v2     NUMERIC(5,2),
    percent_bikes_denver        NUMERIC(5,2),
    percent_scooters_denver     NUMERIC(5,2),
    percent_bikes_v1            NUMERIC(5,2),
    percent_scooters_v1         NUMERIC(5,2),
    percent_bikes_v2            NUMERIC(5,2),
    percent_scooters_v2         NUMERIC(5,2)
);
CREATE INDEX IF NOT EXISTS idx_core_snapshot_time ON snapshot_metadata_core (snapshot_time DESC);

-- 5. Granular per-region narrow rows ----------------------------------------
CREATE TABLE IF NOT EXISTS regional_metrics_narrow (
    cycle_id         UUID REFERENCES observation_cycles(cycle_id),
    snapshot_time    TIMESTAMPTZ NOT NULL,
    region_category  TEXT NOT NULL,
    region_type      TEXT NOT NULL,
    region_name      TEXT NOT NULL,
    count_total      INTEGER DEFAULT 0,
    count_bikes      INTEGER DEFAULT 0,
    count_scooters   INTEGER DEFAULT 0,
    PRIMARY KEY (cycle_id, region_type, region_name)
);
CREATE INDEX IF NOT EXISTS idx_metrics_slider_lookup
    ON regional_metrics_narrow (region_category, region_type, snapshot_time DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_region_lookup
    ON regional_metrics_narrow (region_type, region_name, snapshot_time DESC);

-- 6. Transmission attempts (one row per endpoint per cycle) ------------------
CREATE TABLE IF NOT EXISTS transmission_attempts (
    id               SERIAL PRIMARY KEY,
    cycle_id         UUID REFERENCES observation_cycles(cycle_id),
    endpoint_name    TEXT NOT NULL,
    url              TEXT NOT NULL,
    method           TEXT NOT NULL,
    path             TEXT,
    ts_transmission  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    http_status_code INTEGER,
    error_details    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tx_cycle ON transmission_attempts (cycle_id);

-- 7. Tiny key/value system state (last_archive_ts, etc.) ---------------------
CREATE TABLE IF NOT EXISTS system_state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
