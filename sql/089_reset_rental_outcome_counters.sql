-- Reset the lifetime rental-outcome counters to zero.
--
-- DECIDED BY THE OWNER, 2026-10-07. sql/088 moved the ingest's movement ring
-- (stationary_threshold_meters) from 16 m to 25 m on 2026-10-06 and left
-- device_state.rentals_observed / rentals_no_go exactly as they were, because
-- whether to reset or stamp them "is a data decision and not one a schema file
-- should make quietly". It has now been made: reset.
--
-- WHY. The counters are not retroactive, so every lifetime total blended two
-- definitions of a no-go: rentals counted at 16 m until 2026-10-06 and at 25 m
-- since, with no way to tell which share was which. A published rate built on
-- that is a number whose own definition cannot be stated. From this migration
-- on, both counters describe ONE ring (25 m, the radius sql/072's validation
-- was computed at), and /api/v1/fleet/outcomes names this file and the time it
-- ran as the start of its window.
--
-- WHAT IS DISCARDED (read off production immediately before this was written,
-- 2026-10-07): 1,482,896 observed rentals and 63,848 no-gos across 9,127
-- vehicles, i.e. a blended lifetime rate of 4.3% that no single radius
-- produced.
--
-- WHAT IS LEFT ALONE, DELIBERATELY.
--   * recent_no_go_mask (sql/087): the outcome of a vehicle's last 3 completed
--     rentals. A rolling signal that self-heals within three rentals, not a
--     lifetime total, and the reliability tier reads it as a rule of its own.
--     Zeroing it would silently clear every vehicle's "two of its last three
--     rentals failed" high_risk flag (331 vehicles carried a non-zero mask when
--     this was written). src/api_public.py's outcomes query no longer requires
--     rentals_observed > 0 for that reason: under the old filter every one of
--     those masks would have disappeared from /devices/current until the
--     vehicle's next rental.
--   * number_failed_starts, rental_max_distance_m and everything else.
--
-- WHAT RIDERS SEE. smart_ride_grade needs GRADE_MIN_RENTALS (5) observed
-- rentals before it returns a grade at all, so every vehicle reads "no grade
-- yet" (null, never a low grade) until it has been ridden five times: busy
-- vehicles within days, quiet ones longer. reliability_tier does not read
-- these counters and is unaffected.
--
-- Idempotent in effect: re-running it would zero counters again, but
-- migrations run once by filename (src/pg.py), and this one exists precisely
-- so that a database which applied sql/072 learns of the reset.

-- LOCKING. Migrations run at API boot, in one transaction, while the ingest
-- runs every 2 minutes and holds ~9k device_state row locks (SELECT ... FOR
-- UPDATE) for most of its cycle. A plain UPDATE would contend with it row by
-- row in a different order: a timeout fails the boot, a deadlock aborts one
-- side. Taking the TABLE lock first instead lets reads through, waits for the
-- in-flight cycle to commit, makes the next cycle queue behind the reset, and
-- cannot deadlock. The timeout covers one full ingest cycle. An ingest that
-- queued behind the reset re-reads each row and adds to 0, so no rental
-- completed after the reset is lost.
SET lock_timeout = '150s';

LOCK TABLE device_state IN EXCLUSIVE MODE;

UPDATE device_state
   SET rentals_observed = 0,
       rentals_no_go    = 0
 WHERE rentals_observed <> 0 OR rentals_no_go <> 0;

COMMENT ON COLUMN device_state.rentals_observed IS
    'Rentals seen to completion since the reset in sql/089 (when it ran: '
    'schema_migrations.applied_at). Counts up from zero: a vehicle with few '
    'observations has no grade rather than a flattering one.';

COMMENT ON COLUMN device_state.rentals_no_go IS
    'Of those, how many ended within the ingest''s movement radius (25 m, '
    'stationary_threshold_meters; sql/088) of where the rider unlocked it: the '
    'scooter did not go. END displacement, not maximum distance, so a round trip '
    'back to the same rack counts. One definition since the sql/089 reset.';

RESET lock_timeout;
