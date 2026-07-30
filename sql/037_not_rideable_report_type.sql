-- Rename the device_reports type 'failed_unlock' -> 'not_rideable', and the
-- matching points action 'report_wont_start' -> 'report_not_rideable'.
--
-- The old name described one specific failure (the unlock didn't work). The
-- rider-facing question is broader and simpler: could you ride it or not?
-- A scooter that unlocks and then won't move, or that is unsafe to ride, is
-- the same answer to the rider. The button now reads "🚫 Not Rideable",
-- matching the "Likely rideable" tier language the map already uses.
--
-- Spelling: "rideable", consistent with the existing "Likely rideable".
--
-- Order matters. Each CHECK is dropped BEFORE its rows are rewritten and
-- re-added after, because the constraint would otherwise reject the very
-- UPDATE that migrates the data.

-- --------------------------------------------------------------------------
-- device_reports.report_type
-- --------------------------------------------------------------------------
ALTER TABLE device_reports
    DROP CONSTRAINT IF EXISTS device_reports_report_type_allowed;
-- The original inline constraint from sql/013, in case an instance predates
-- sql/023 having renamed it.
ALTER TABLE device_reports
    DROP CONSTRAINT IF EXISTS device_reports_report_type_check;

UPDATE device_reports
   SET report_type = 'not_rideable'
 WHERE report_type = 'failed_unlock';

ALTER TABLE device_reports
    ADD CONSTRAINT device_reports_report_type_allowed
    CHECK (report_type IN (
        'not_rideable', 'dead_battery', 'damaged', 'improperly_parked',
        'not_found'
    ));

-- --------------------------------------------------------------------------
-- user_points.action
-- --------------------------------------------------------------------------
-- REPLAY SAFETY (fixed in place, same bug class and same repair shape as
-- sql/029's fix for device_reports_report_type_allowed above -- see that
-- file's header, and the sql/040/041 guarded-rewrite shape
-- PLAN_RIDE_MODE_API.md's house rules point to). This block used to DROP
-- the constraint unconditionally and re-ADD it with only the 8 action
-- values known when this file was written.
--
-- RIDE_MODE_OVERHAUL_PLAN.md's phases A2/A3 (sql/053, sql/052) later widen
-- this SAME constraint to admit five more actions ('battery_contribution',
-- 'nav_route_feedback', 'nav_qualitative_feedback', 'nav_distance_bonus',
-- 'ride_survey') -- see those files' own guarded DO blocks.
--
-- CORRECTED (review fix): the prior wording here claimed production itself
-- replays every migration file on every boot, which is not how
-- src/pg.py:run_migrations() works -- it records each applied filename in
-- schema_migrations and skips it on every later boot, so THIS specific
-- unconditional re-ADD could never re-fire in a normally deployed
-- production database once sql/037 has been applied there once. The real,
-- reachable replay path is test/manual idempotence: several `_pg` test
-- fixtures (e.g. tests/test_daily_trips_rollup_pg.py's `pg_conn`) apply
-- every file in sql/ directly, in filename order, against a PERSISTENT
-- `VEO_TEST_PG_DSN` database on every test run -- bypassing
-- schema_migrations entirely, exactly as that fixture's own comment says
-- ("applying them directly... is safe to repeat across runs"). On a
-- SECOND such run against the same database, after a PRIOR run already
-- executed sql/052/053 and widened this constraint, this file's block
-- would reach the old unconditional re-ADD first (037 sorts before
-- 052/053) and a CheckViolation would fire against any row already
-- carrying one of the five newer actions -- the same failure mode as
-- sql/029's bug, just reached through repeated test/manual replay rather
-- than a production reboot. A hand-run `psql -f sql/037_...sql` against
-- an already-migrated database would hit the identical failure. Guarded
-- the same way sql/029 was repaired: skip the rewrite once
-- 'report_not_rideable' (the value THIS migration installs) is already
-- present, so replaying after a later migration has widened the list
-- further is a no-op instead of a regression that reverts it. This has
-- never shipped to a live production database (this file and its guard
-- both land in the same unreleased PR), so no separate forward migration
-- is needed to repair one -- correcting this comment's attribution is the
-- whole fix.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'user_points_action_allowed'
       AND conrelid = 'user_points'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('report_not_rideable' in current_def) = 0 THEN
        ALTER TABLE user_points
            DROP CONSTRAINT IF EXISTS user_points_action_allowed;

        UPDATE user_points
           SET action = 'report_not_rideable'
         WHERE action = 'report_wont_start';

        ALTER TABLE user_points
            ADD CONSTRAINT user_points_action_allowed
            CHECK (action IN (
                'profile_completion', 'waypoint', 'gbfs_trip_validated',
                'report_not_rideable', 'report_not_found',
                'report_vehicle_issue', 'report_improper_parking',
                'qr_scan'
            ));
    END IF;
END $$;
