-- Ridden distance on tracked_rides, so the mileage/streak badges can read
-- from THIS table instead of the retired legacy `rides` log (sql/014,
-- dropped in 035). src/badges.py is the only consumer today.
--
-- Two columns on purpose. distance_meters alone would be a number with no
-- stated confidence, and the two ways we can derive it are not comparable:
--
--   'waypoints'     — summed over the rider's uploaded GPS fixes
--                     (src/geo.py:path_length_meters). Good. Still a lower
--                     bound, since sampling measures each curve as a chord.
--   'straight_line' — start -> reported end, as the crow flies. This is
--                     what we get when the rider uploaded no waypoints at
--                     all, and it UNDERCOUNTS badly on any real route.
--
-- Keeping the provenance means a later backfill, a badge threshold change,
-- or an analytics query can exclude the weak measurement instead of
-- silently averaging it in with the good one. NULL = ride never ended, so
-- distance was never computed.
ALTER TABLE tracked_rides
    ADD COLUMN IF NOT EXISTS distance_meters DOUBLE PRECISION
        CHECK (distance_meters >= 0);

ALTER TABLE tracked_rides
    ADD COLUMN IF NOT EXISTS distance_source TEXT
        CHECK (distance_source IN ('waypoints', 'straight_line'));

-- Badge reads scan an account's completed rides oldest-first to find the
-- row that crossed each mileage threshold (src/badges.py:_ride_badges).
CREATE INDEX IF NOT EXISTS idx_tracked_rides_account_ended
    ON tracked_rides (account_id, user_reported_ended_at ASC)
    WHERE user_reported_ended_at IS NOT NULL;
