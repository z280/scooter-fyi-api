-- Rider-facing, server-detected ride tracking (requirements items 1, 5-9).
--
-- Deliberately separate from the legacy `rides` table (sql/014): that
-- table is supporter-gated, client-POSTed after the fact, hard-delete
-- only, and explicitly walled off from analytics ("nothing else in the
-- codebase may read this table" — though src/badges.py already reads it
-- for mileage badges, a pre-existing inconsistency, not addressed here).
-- This feature is server-detected via the GBFS watch below, open to every
-- rider (no supporter gate), and IS meant to be read by the points
-- system. Do not merge these two mechanisms; src/api_rides.py stays
-- untouched.
--
-- Ride-path storage: NO PostGIS in this codebase (sql/001_init.sql:
-- "narrow tables only, no PostGIS"). path_polyline follows the exact
-- convention already established for the legacy rides table
-- (src/polyline.py): a Google-encoded TEXT polyline, decoded to a
-- GeoJSON LineString only at read/export time. "A field with LINESTRING
-- data" is satisfied via that existing convention, not a native geometry
-- column.
--
-- END-DATA INDEPENDENCE (do not collapse — compared later for point
-- validation):
--   gbfs_*             written ONLY by src/ride_watch.py (GBFS polling).
--   user_reported_ended_at, end_lat/end_lon, reported_battery_percent,
--   total_cost_cents, metadata   written ONLY by PATCH .../end (rider
--   action). Kept independent so a rider can never see the GBFS-observed
--   end location before submitting their own — see src/api_tracked_rides.py.

CREATE TABLE IF NOT EXISTS tracked_rides (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id                  BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,

    -- Vehicle identity at ride start (src/identity.py). Not FK'd to
    -- device_state on purpose — ride history must survive independently
    -- of that table's own row lifecycle.
    vehicle_identifier          TEXT NOT NULL,

    status                      TEXT NOT NULL DEFAULT 'watching'
                                 CHECK (status IN ('watching', 'left_feed', 'completed', 'expired')),

    -- User-declared start (item 5)
    started_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    start_lat                   DOUBLE PRECISION NOT NULL CHECK (start_lat BETWEEN -90 AND 90),
    start_lon                   DOUBLE PRECISION NOT NULL CHECK (start_lon BETWEEN -180 AND 180),

    -- Watch bookkeeping — drives src/ride_watch.py and the active-ride query
    watch_expires_at            TIMESTAMPTZ NOT NULL,

    -- GBFS-observed lifecycle — written ONLY by src/ride_watch.py
    gbfs_left_feed_at           TIMESTAMPTZ,
    gbfs_left_feed_cycle_id     UUID REFERENCES observation_cycles(cycle_id),
    gbfs_reappeared_at          TIMESTAMPTZ,
    gbfs_reappeared_cycle_id    UUID REFERENCES observation_cycles(cycle_id),
    gbfs_end_lat                DOUBLE PRECISION,
    gbfs_end_lon                DOUBLE PRECISION,
    gbfs_end_battery_percent    INTEGER CHECK (gbfs_end_battery_percent BETWEEN 0 AND 100),

    -- User-reported end (item 6) — written ONLY by PATCH .../end
    user_reported_ended_at      TIMESTAMPTZ,
    end_lat                     DOUBLE PRECISION CHECK (end_lat BETWEEN -90 AND 90),
    end_lon                     DOUBLE PRECISION CHECK (end_lon BETWEEN -180 AND 180),
    reported_battery_percent    NUMERIC(4,1) CHECK (reported_battery_percent BETWEEN 0 AND 100),
    total_cost_cents            INTEGER CHECK (total_cost_cents >= 0),
    metadata                    JSONB NOT NULL DEFAULT '{}',

    -- Ride path — see header note. Rebuilt from ride_waypoints on every
    -- waypoint insert (src/api_tracked_rides.py); '' until the first waypoint.
    path_polyline                TEXT NOT NULL DEFAULT '',

    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Pagination (item 7): mirrors idx_rides_account_started (sql/014).
CREATE INDEX IF NOT EXISTS idx_tracked_rides_account_started
    ON tracked_rides (account_id, started_at DESC);

-- Active-ride lookup (item 8). watch_expires_at > NOW() is applied at
-- query time (NOW() can't appear in an index predicate), but this alone
-- narrows a whole account's history down to (normally) one row.
CREATE INDEX IF NOT EXISTS idx_tracked_rides_open
    ON tracked_rides (account_id, started_at DESC)
    WHERE user_reported_ended_at IS NULL AND gbfs_reappeared_at IS NULL;

-- "Recommend a device, only if in the rider's ride history in the last
-- 24h" -> WHERE account_id=? AND vehicle_identifier=? AND
-- started_at > NOW() - INTERVAL '24 hours'.
CREATE INDEX IF NOT EXISTS idx_tracked_rides_account_vehicle
    ON tracked_rides (account_id, vehicle_identifier, started_at DESC);

CREATE TABLE IF NOT EXISTS user_device_watch_list (
    id                      BIGSERIAL PRIMARY KEY,
    tracked_ride_id         UUID NOT NULL UNIQUE REFERENCES tracked_rides(id) ON DELETE CASCADE,
    account_id              BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    vehicle_identifier      TEXT NOT NULL,
    -- Independent of tracked_rides.status: this only tracks the GBFS
    -- detection state machine (never 'completed' — this table doesn't
    -- know about user reports).
    status                  TEXT NOT NULL DEFAULT 'watching'
                             CHECK (status IN ('watching', 'left_feed', 'resolved', 'expired')),
    watch_started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    watch_expires_at        TIMESTAMPTZ NOT NULL,
    last_checked_cycle_id   UUID REFERENCES observation_cycles(cycle_id)
);

-- THE hot index: src/ride_watch.py's every-2-minute query is exactly
-- `WHERE status IN ('watching','left_feed') AND watch_expires_at > %s`.
-- Its size is bounded by rides CURRENTLY in flight, not all-time ride
-- volume, because ride_watch.py (on resolve) and
-- cli.py:expire_stale_watches (on timeout) both promote rows out of this
-- partial index promptly.
CREATE INDEX IF NOT EXISTS idx_watch_list_open_expiry
    ON user_device_watch_list (watch_expires_at)
    WHERE status IN ('watching', 'left_feed');

CREATE INDEX IF NOT EXISTS idx_watch_list_vehicle
    ON user_device_watch_list (vehicle_identifier);

CREATE TABLE IF NOT EXISTS ride_waypoints (
    id                 BIGSERIAL PRIMARY KEY,
    tracked_ride_id    UUID NOT NULL REFERENCES tracked_rides(id) ON DELETE CASCADE,
    account_id         BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    waypoint_at        TIMESTAMPTZ NOT NULL,      -- client-reported GPS fix time
    lat                DOUBLE PRECISION NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon                DOUBLE PRECISION NOT NULL CHECK (lon BETWEEN -180 AND 180),
    metadata           JSONB NOT NULL DEFAULT '{}',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()   -- server receipt time
);

-- Path reconstruction (ORDER BY waypoint_at ASC) and "latest position"
-- for in-ride directions (ORDER BY waypoint_at DESC) both use this one
-- index — Postgres scans a btree backwards as cheaply as forwards.
CREATE INDEX IF NOT EXISTS idx_ride_waypoints_ride_time
    ON ride_waypoints (tracked_ride_id, waypoint_at, id);

-- "+2 points per waypoint, only if ride marked complete" -> join to
-- tracked_rides WHERE status='completed', COUNT(*).
CREATE INDEX IF NOT EXISTS idx_ride_waypoints_account
    ON ride_waypoints (account_id, tracked_ride_id);
