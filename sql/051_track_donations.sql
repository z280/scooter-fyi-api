-- Track donations (PLAN_RIDE_MODE_API.md phase A2 / master
-- RIDE_MODE_OVERHAUL_PLAN.md Part 2): the bulk-upload target for a ride's
-- locally-recorded, hash-chained, HMAC-signed waypoint chain, verified once
-- server-side at donation (src/track_verify.py) and never transmitted
-- mid-ride.
--
-- Raw JWS strings are DISCARDED after verification — only chain_root_hash
-- (the final rolling H_n, an audit anchor) and the `verification` summary
-- (per-check results) persist. The DECODED waypoints, by contrast, ARE kept
-- in donated_track_points below: they are what battery ingestion and the
-- de-id sweep's minute-coarsening both need, and they are the thing consent
-- was actually given to donate.
--
-- ---------------------------------------------------------------------------
-- MIGRATION SHAPE
-- ---------------------------------------------------------------------------
-- src/pg.py replays every file in sql/ on a fresh boot and the _pg test
-- fixtures execute the whole directory on every run, so every statement here
-- is idempotent (CREATE TABLE/INDEX IF NOT EXISTS, guarded DO $$ blocks for
-- the two enumerated-value widenings) per sql/041's header rule.

CREATE TABLE IF NOT EXISTS track_donations (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tracked_ride_id    UUID   REFERENCES tracked_rides(id) ON DELETE CASCADE,  -- nulled by de-id
    account_id         BIGINT REFERENCES accounts(id)      ON DELETE CASCADE,  -- nulled by de-id
    vehicle_model      TEXT,                        -- kept post-de-id (battery model needs it)
    chain_root_hash    TEXT NOT NULL,               -- final rolling hash, audit anchor
    batch_count        INTEGER NOT NULL,
    waypoint_count     INTEGER NOT NULL,
    distance_meters    DOUBLE PRECISION,            -- clamped haversine, server-computed
    verification       JSONB NOT NULL DEFAULT '{}', -- per-check results + reasons
    points_awarded     INTEGER NOT NULL DEFAULT 0,
    donated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    points_settled_at  TIMESTAMPTZ,
    deidentified_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_track_donations_ride
    ON track_donations (tracked_ride_id) WHERE tracked_ride_id IS NOT NULL;  -- one donation per ride
CREATE INDEX IF NOT EXISTS idx_track_donations_deid
    ON track_donations (points_settled_at) WHERE deidentified_at IS NULL;

CREATE TABLE IF NOT EXISTS donated_track_points (
    donation_id  UUID NOT NULL REFERENCES track_donations(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    recorded_ms  BIGINT NOT NULL,        -- client epoch ms; de-id coarsens to minute
    lat          DOUBLE PRECISION NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon          DOUBLE PRECISION NOT NULL CHECK (lon BETWEEN -180 AND 180),
    accuracy_m   REAL,
    PRIMARY KEY (donation_id, seq)
);

-- ---------------------------------------------------------------------------
-- battery_trip_observations.source: which pipeline produced a row, so the
-- double-count guard (src/battery_model.py, both directions) can tell a
-- donated-track observation from a nightly feed-mined one.
-- ---------------------------------------------------------------------------
-- Column is bare (no inline CHECK on ADD COLUMN IF NOT EXISTS — sql/041's
-- header rule); the CHECK follows as an explicit named, guarded constraint.
-- NULL means "predates this migration" and is treated as feed-mined
-- everywhere that matters (src/battery_model.py's `IS DISTINCT FROM
-- 'donated_ride'` guard), so there is nothing to backfill before adding the
-- CHECK — every existing row (NULL) already satisfies it.
ALTER TABLE battery_trip_observations
    ADD COLUMN IF NOT EXISTS source TEXT;

-- Value-checked guard (sql/040 shape): a replay after a later migration
-- widens this list further must be a no-op, not a regression that narrows
-- it back.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'battery_trip_observations_source_allowed'
       AND conrelid = 'battery_trip_observations'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('donated_ride' in current_def) = 0 THEN
        ALTER TABLE battery_trip_observations DROP CONSTRAINT IF EXISTS battery_trip_observations_source_allowed;
        ALTER TABLE battery_trip_observations
            ADD CONSTRAINT battery_trip_observations_source_allowed
            CHECK (source IS NULL OR source IN ('feed_mined', 'donated_ride'));
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- tracked_rides.track_donated_at: the durable "this ride has been donated"
-- marker.
-- ---------------------------------------------------------------------------
-- idx_track_donations_ride (above) stops binding the moment de-id nulls
-- track_donations.tracked_ride_id, so POST .../track's 409 already_donated
-- check needs a stamp that survives the sweep — a hard-delete of the ride
-- itself still removes this column along with the rest of the row, so the
-- delete commitment is untouched.
ALTER TABLE tracked_rides
    ADD COLUMN IF NOT EXISTS track_donated_at TIMESTAMPTZ;
