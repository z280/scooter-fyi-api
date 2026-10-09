-- "Never left the spot": a 50 m counter beside the 25 m no-go.
--
-- DECIDED BY THE OWNER, 2026-10-09, after a production measurement rebuilt
-- from raw_telemetry_points (2026-10-07 08:02Z .. 2026-10-08 17:12Z):
--   * of rentals counted in rentals_no_go (drop within 25 m of the unlock
--     point), about 57% are genuine ROUND TRIPS: their furthest point is more
--     than 50 m away (median 540 m);
--   * only about 43% never left the spot (furthest point <= 50 m): 660 of
--     30,727 rentals, 2.1% of all rentals;
--   * GPS jitter barely matters: only 2.5 points of no-gos have a furthest
--     point between 25 m and 50 m.
--
-- rentals_no_go KEEPS ITS DEFINITION EXACTLY (end displacement within
-- stationary_threshold_meters, round trips included; "ended where they
-- began"). This migration adds a second, narrower count beside it.
--
-- THE DEFINITION. A rental "stayed" when the vehicle never got farther than
-- IN_PLACE_RADIUS_M (50 m) from where it was unlocked, counting the final fix,
-- and was released inside that circle. That is exactly src/device_state.py's
-- IN-PLACE RELEASE condition (sql/087), counted for EVERY in-place release:
-- a failed start (rotated bike_id) and a reservation blip (no rotation)
-- alike, because the question is "did it leave the spot", not "was it an
-- attempt". A rental whose maximum is unknown (first seen mid-rental, or
-- begun before sql/087) is not in place, so it is not counted as stayed.
--
-- WHY A SECOND DENOMINATOR (rentals_observed_stayed_era). rentals_observed
-- has been counting since the sql/089 reset (2026-10-07); rentals_stayed
-- starts at 0 today. Dividing one by the other would read every vehicle as
-- far better than it is until months of history built up. So every release
-- from here on increments rentals_observed_stayed_era, in the same UPDATE as
-- rentals_observed, and rentals_stayed / rentals_observed_stayed_era is the
-- rate over one window. smart_ride_grade gates on that n. rentals_observed
-- and rentals_no_go are NOT reset again (the owner already reset them once,
-- sql/089).
--
-- INVARIANTS. Every release increments rentals_observed and the era counter
-- in the same UPDATE, and an in-place release is a release (in_place requires
-- `released`; blips are in rentals_observed too), so in practice
-- rentals_stayed <= rentals_observed_stayed_era <= rentals_observed.
-- ENFORCED: rentals_stayed <= rentals_observed_stayed_era, the invariant over
-- one window, which is what every rate divides. NOT ENFORCED, deliberately:
-- the two comparisons against rentals_observed. A reset of rentals_observed
-- alone, which is exactly what sql/089 is, would violate them, and
-- tests/test_ghost_stops_pg.py replays every migration (sql/089 included) on
-- every test; a CHECK that makes an existing migration un-replayable is a
-- trap, not a guarantee. Existing rows start at 0/0.
--
-- rental_outcomes_hourly gets the same split: `stayed` (in-place releases)
-- and `stayed_known` (rentals for which `stayed` was recorded, i.e. every
-- rental written by code that knows the column). Rows written before this
-- migration, and any rows an old ingest writes during the deploy, have
-- stayed = 0 AND stayed_known = 0, which means NOT RECORDED, not "none
-- stayed". A rate is stayed / stayed_known, never stayed / rentals, so a
-- partial deploy hour cannot dilute it. The published start of the window is
-- this file's schema_migrations.applied_at (`stayed_counted_since`).
--
-- NO PERSONAL DATA: two integers per vehicle, two per existing rollup row.

-- LOCKING, as sql/089: the ingest holds ~9k device_state row locks for most
-- of its 2-minute cycle. ADD COLUMN with a constant default is metadata-only
-- (PG 11+), and the CHECKs scan ~9k rows, but ALTER TABLE still needs the
-- table lock, so wait up to one full cycle for it.
SET lock_timeout = '150s';

ALTER TABLE device_state
    ADD COLUMN IF NOT EXISTS rentals_stayed INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS rentals_observed_stayed_era INTEGER NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'device_state_rentals_stayed_check') THEN
        ALTER TABLE device_state ADD CONSTRAINT device_state_rentals_stayed_check
            CHECK (rentals_stayed >= 0
                   AND rentals_stayed <= rentals_observed_stayed_era);
    END IF;
END $$;

COMMENT ON COLUMN device_state.rentals_stayed IS
    'Completed rentals, since sql/099, that never left the spot: the vehicle '
    'never got more than 50 m (IN_PLACE_RADIUS_M) from where it was unlocked, '
    'final fix included, and was released there. Every in-place release '
    '(failed start or reservation blip). A subset of rentals_observed_stayed_era.';

COMMENT ON COLUMN device_state.rentals_observed_stayed_era IS
    'Completed rentals since sql/099 (when it ran: schema_migrations.applied_at): '
    'the denominator for rentals_stayed and the n smart_ride_grade gates on. '
    'Incremented with rentals_observed, which counts from the earlier sql/089 reset.';

ALTER TABLE rental_outcomes_hourly
    ADD COLUMN IF NOT EXISTS stayed INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS stayed_known INTEGER NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'rental_outcomes_hourly_stayed_check') THEN
        ALTER TABLE rental_outcomes_hourly ADD CONSTRAINT rental_outcomes_hourly_stayed_check
            CHECK (stayed >= 0 AND stayed_known >= 0
                   AND stayed <= rentals
                   AND stayed <= stayed_known
                   AND stayed_known <= rentals);
    END IF;
END $$;

COMMENT ON COLUMN rental_outcomes_hourly.stayed IS
    'Of rentals, how many never left the spot (never more than 50 m from the '
    'unlock point, released there): device_state.rentals_stayed''s definition. '
    'Recorded since sql/099; 0 with stayed_known = 0 means not recorded.';

COMMENT ON COLUMN rental_outcomes_hourly.stayed_known IS
    'Rentals for which stayed was recorded (every rental written since sql/099). '
    'The denominator for stayed: rate = stayed / stayed_known.';

RESET lock_timeout;
