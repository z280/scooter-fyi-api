-- Servicing, recorded (docs/SERVICING_PLAN.md Phase 1; owner, 2026-10-10).
--
--   1. device_state gains where and when its parked low was (so a servicing
--      knows where the swap happened relative to the low), and the SETTLED
--      reading: the range sags under load during a ride and climbs back for
--      minutes after it, so for SETTLE_MINUTES after a rental ends the
--      highest parked reading since is the one shown and compared.
--   2. service_events: one row per servicing (full after a parked low <= 50%,
--      sql/105's rule) — the permanent log; raw telemetry is pruned in ~2 days.
--   3. depot_visits: one row per stay inside an operator depot
--      (data/depots.json), from the first stop inside to the first outside.
--
-- REPLAY SAFETY: re-executed by the _pg fixtures; IF NOT EXISTS throughout.

ALTER TABLE device_state ADD COLUMN IF NOT EXISTS range_low_at TIMESTAMPTZ;
ALTER TABLE device_state ADD COLUMN IF NOT EXISTS range_low_lat DOUBLE PRECISION;
ALTER TABLE device_state ADD COLUMN IF NOT EXISTS range_low_lon DOUBLE PRECISION;
ALTER TABLE device_state ADD COLUMN IF NOT EXISTS settled_range_meters INTEGER;
ALTER TABLE device_state ADD COLUMN IF NOT EXISTS settling_until TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS service_events (
    id                  BIGSERIAL PRIMARY KEY,
    vehicle_identifier  TEXT NOT NULL,
    observed_at         TIMESTAMPTZ NOT NULL,      -- the full reading
    cycle_id            UUID,
    lat                 DOUBLE PRECISION,
    lon                 DOUBLE PRECISION,
    h3_9_index          BIGINT,
    equity_area         TEXT,                      -- of the LOW point (where it waited)
    low_range_meters    INTEGER,
    low_at              TIMESTAMPTZ,
    low_lat             DOUBLE PRECISION,
    low_lon             DOUBLE PRECISION,
    full_range_meters   INTEGER NOT NULL,
    moved_m             DOUBLE PRECISION,          -- low spot -> full spot
    in_place            BOOLEAN,                   -- moved_m < 100
    after_absence       BOOLEAN NOT NULL DEFAULT FALSE,
    depot_id            TEXT,                      -- full reading inside a depot
    vehicle_model_name  TEXT,
    source              TEXT NOT NULL DEFAULT 'ingest',   -- 'ingest' | 'backfill'
    CONSTRAINT service_events_source_allowed CHECK (source IN ('ingest', 'backfill'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_service_events_vehicle_time
    ON service_events (vehicle_identifier, observed_at);
CREATE INDEX IF NOT EXISTS idx_service_events_observed_at
    ON service_events (observed_at DESC);

CREATE TABLE IF NOT EXISTS depot_visits (
    id                  BIGSERIAL PRIMARY KEY,
    vehicle_identifier  TEXT NOT NULL,
    depot_id            TEXT NOT NULL,
    entered_at          TIMESTAMPTZ NOT NULL,      -- first stop inside
    last_inside_at      TIMESTAMPTZ,               -- last time seen inside
    exited_at           TIMESTAMPTZ,               -- first stop outside; NULL = still inside
    picked_up_at        TIMESTAMPTZ,               -- left its last street stop
    pickup_lat          DOUBLE PRECISION,
    pickup_lon          DOUBLE PRECISION,
    pickup_equity_area  TEXT,
    went_dark_first     BOOLEAN,                   -- that street stop closed 'absent'
    deploy_lat          DOUBLE PRECISION,
    deploy_lon          DOUBLE PRECISION,
    deploy_equity_area  TEXT,
    vehicle_model_name  TEXT,
    source              TEXT NOT NULL DEFAULT 'ingest',
    CONSTRAINT depot_visits_source_allowed CHECK (source IN ('ingest', 'backfill'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_depot_visits_vehicle_entered
    ON depot_visits (vehicle_identifier, entered_at);
CREATE INDEX IF NOT EXISTS idx_depot_visits_open
    ON depot_visits (vehicle_identifier) WHERE exited_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_depot_visits_exited
    ON depot_visits (vehicle_identifier, exited_at DESC);
