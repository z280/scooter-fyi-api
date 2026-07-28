-- Repurpose `rides` (sql/014) from a supporter-only manual log into the
-- OFF-FEED ride tracker: rides on vehicles that are not in the public GBFS
-- feed and therefore have no vehicle_identifier — a personal scooter, a
-- competitor's rental, a friend's e-bike. Contrast tracked_rides (sql/027),
-- which is GBFS-detected against a specific Veo vehicle.
--
-- Two things change:
--
--   1. Logging is no longer supporter-gated (src/api_rides.py). Nothing
--      else in the rider surface is, and paywalling data collection just
--      means less data.
--   2. A ride is now a LIFECYCLE, not a single POST. The old one-shot
--      "here is a finished ride" endpoint still works, but a rider can also
--      start a ride, stream waypoints, and report the end — mirroring
--      tracked_rides so one client code path drives both.
--
-- The privacy commitment from sql/014 is UNCHANGED and still binding:
-- polylines are the most sensitive data here, deletes are hard deletes, and
-- no module may read this table for analytics. The new waypoint table
-- cascades on delete so that commitment still holds end to end.

-- ---------------------------------------------------------------------------
-- Lifecycle
-- ---------------------------------------------------------------------------
-- DEFAULT 'completed' is what makes this migration safe on existing rows:
-- every ride logged under the old one-shot API was, by definition, finished.
ALTER TABLE rides ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'completed'
    CHECK (status IN ('active', 'completed'));

-- ---------------------------------------------------------------------------
-- What was ridden. There is no vehicle_identifier by definition — this is
-- the whole point of the table — so the rider describes it instead.
-- ---------------------------------------------------------------------------
ALTER TABLE rides ADD COLUMN IF NOT EXISTS vehicle_kind TEXT
    CHECK (vehicle_kind IN ('scooter', 'bicycle', 'other'));
-- Free text on purpose: "Lime", "Bird", "personal", "my Segway". Enumerating
-- operators would need a migration every time a new one enters Denver, and
-- this field is descriptive, never a join key.
ALTER TABLE rides ADD COLUMN IF NOT EXISTS operator TEXT
    CHECK (operator IS NULL OR char_length(operator) <= 64);

-- ---------------------------------------------------------------------------
-- Endpoints of the ride. An active ride knows where it started before it has
-- any waypoints at all; a ride that ends with no waypoints still has two
-- points to measure between.
-- ---------------------------------------------------------------------------
ALTER TABLE rides ADD COLUMN IF NOT EXISTS start_lat DOUBLE PRECISION
    CHECK (start_lat BETWEEN -90 AND 90);
ALTER TABLE rides ADD COLUMN IF NOT EXISTS start_lon DOUBLE PRECISION
    CHECK (start_lon BETWEEN -180 AND 180);
ALTER TABLE rides ADD COLUMN IF NOT EXISTS end_lat DOUBLE PRECISION
    CHECK (end_lat BETWEEN -90 AND 90);
ALTER TABLE rides ADD COLUMN IF NOT EXISTS end_lon DOUBLE PRECISION
    CHECK (end_lon BETWEEN -180 AND 180);

-- Same provenance rule as tracked_rides.distance_source (sql/034), plus a
-- third source this table needs and that one doesn't: 'client', where the
-- rider's own app computed the distance and handed us the finished number.
ALTER TABLE rides ADD COLUMN IF NOT EXISTS distance_source TEXT
    CHECK (distance_source IN ('client', 'waypoints', 'straight_line'));

-- ---------------------------------------------------------------------------
-- Relax the NOT NULLs that assumed every row was born finished. An active
-- ride has no end time, no duration, no distance and no path yet.
-- ---------------------------------------------------------------------------
ALTER TABLE rides ALTER COLUMN ended_at        DROP NOT NULL;
ALTER TABLE rides ALTER COLUMN duration_s      DROP NOT NULL;
ALTER TABLE rides ALTER COLUMN distance_m      DROP NOT NULL;
ALTER TABLE rides ALTER COLUMN polyline        DROP NOT NULL;
ALTER TABLE rides ALTER COLUMN started_in_zone DROP NOT NULL;
ALTER TABLE rides ALTER COLUMN ended_in_zone   DROP NOT NULL;
-- rate_plan describes VEO's pricing tiers. On a Lime scooter or a personal
-- e-bike it is meaningless, so it becomes optional rather than a required
-- lie. The CHECK on its allowed values (sql/014) still applies when present.
ALTER TABLE rides ALTER COLUMN rate_plan       DROP NOT NULL;

-- Dropping those NOT NULLs would otherwise let a *completed* ride be stored
-- half-filled, which is exactly the integrity the original schema had. Move
-- the guarantee to where it still holds: completed rides must be complete.
-- (rate_plan and est_cost_cents stay genuinely optional — see above.)
ALTER TABLE rides DROP CONSTRAINT IF EXISTS rides_completed_is_complete;
ALTER TABLE rides ADD CONSTRAINT rides_completed_is_complete CHECK (
    status <> 'completed' OR (
        ended_at IS NOT NULL
        AND duration_s IS NOT NULL
        AND distance_m IS NOT NULL
        AND polyline IS NOT NULL
        AND started_in_zone IS NOT NULL
        AND ended_in_zone IS NOT NULL
    )
);

-- One active off-feed ride per account, enforced by the database rather than
-- by a read-then-write in the app: you can only be on one vehicle at a time.
-- Independent of tracked_rides' own active-ride rule — the two mechanisms
-- don't know about each other.
CREATE UNIQUE INDEX IF NOT EXISTS idx_rides_one_active_per_account
    ON rides (account_id) WHERE status = 'active';

-- Badge reads scan an account's completed rides oldest-first by end time
-- (src/badges.py:_ride_badges); idx_rides_account_started is on started_at.
CREATE INDEX IF NOT EXISTS idx_rides_account_ended
    ON rides (account_id, ended_at ASC) WHERE ended_at IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Route tracks
-- ---------------------------------------------------------------------------
-- Separate from ride_waypoints (sql/027), which is foreign-keyed to
-- tracked_rides. Same shape, different parent — one table can't reference
-- two. ON DELETE CASCADE is load-bearing: it is what keeps DELETE
-- /api/v1/rides/{id} a true hard delete of the whole route.
CREATE TABLE IF NOT EXISTS off_feed_ride_waypoints (
    id           BIGSERIAL PRIMARY KEY,
    ride_id      UUID NOT NULL REFERENCES rides(id) ON DELETE CASCADE,
    account_id   BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    waypoint_at  TIMESTAMPTZ NOT NULL,
    lat          DOUBLE PRECISION NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon          DOUBLE PRECISION NOT NULL CHECK (lon BETWEEN -180 AND 180),
    metadata     JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The polyline rebuild reads the whole ordered set on every append.
CREATE INDEX IF NOT EXISTS idx_off_feed_ride_waypoints_ride
    ON off_feed_ride_waypoints (ride_id, waypoint_at ASC, id ASC);
