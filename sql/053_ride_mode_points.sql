-- Ride Mode points reshape (PLAN_RIDE_MODE_API.md phase A2 / master
-- RIDE_MODE_OVERHAUL_PLAN.md Decision 6): widen user_points.action for the
-- five new ride-mode awards, and make the owner's even-points rule a
-- database fact, not just a Python convention.
--
-- ---------------------------------------------------------------------------
-- 1. user_points.action: widen the vocabulary.
-- ---------------------------------------------------------------------------
-- sql/028 named this constraint at CREATE TABLE time (`CONSTRAINT
-- user_points_action_allowed CHECK (...)`), and sql/037 already rewrote it
-- once (report_wont_start -> report_not_rideable) using a plain
-- DROP/re-ADD of that same name. There is no Postgres-auto-named twin to
-- worry about here — unlike sql/040/041/042, which had to clean up an
-- inline CHECK a CREATE TABLE left unnamed, sql/028 never did that.
--
-- REPLAY SAFETY (src/pg.py replays every file in sql/ on every boot; the
-- _pg test fixtures execute the whole directory on every run): the sql/040
-- guarded shape — read the live definition first, rewrite only when a
-- value this file needs is missing — so a replay after a LATER migration
-- widens the list further is a no-op instead of a regression that reverts
-- it.
--
-- THE GUARD IS KEYED ON 'battery_contribution' ALONE, deliberately not on
-- all five new values: PLAN_RIDE_MODE_API.md phase A3 ships sql/052, which
-- widens this SAME constraint for THREE of these five actions
-- ('ride_survey', 'nav_route_feedback', 'nav_qualitative_feedback') using
-- its own guard keyed on 'ride_survey', because A2 and A3 are independently
-- mergeable and may land in either order. If this file's guard checked for
-- all five, a database that ran sql/052 first (which already added those
-- three) would look "already fine" to the naive read but still be missing
-- 'battery_contribution' and 'nav_distance_bonus' — 'battery_contribution'
-- is the one value ONLY this migration ever adds, so it is the one
-- unambiguous signal that this file's widening has already run. Whichever
-- of 052/053 lands second finds its target list a strict superset of what
-- is live and rewrites to the same union either way — a genuine no-op, not
-- a narrowing.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'user_points_action_allowed'
       AND conrelid = 'user_points'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('battery_contribution' in current_def) = 0 THEN
        ALTER TABLE user_points DROP CONSTRAINT IF EXISTS user_points_action_allowed;
        ALTER TABLE user_points
            ADD CONSTRAINT user_points_action_allowed
            CHECK (action IN (
                -- Pre-existing values. History is forever: 'waypoint' and
                -- 'gbfs_trip_validated' stop being AWARDED as of this phase
                -- (PATCH .../end no longer credits them — see
                -- src/api_tracked_rides.py), but old ledger rows keep the
                -- value that earned them, and credit_waypoint_points /
                -- credit_gbfs_validation_points themselves stay in
                -- src/points.py for history and their existing tests.
                'profile_completion', 'waypoint', 'gbfs_trip_validated',
                'report_not_rideable', 'report_not_found',
                'report_vehicle_issue', 'report_improper_parking',
                'qr_scan',
                -- New in this phase (RIDE_MODE_OVERHAUL_PLAN.md Decision 6 /
                -- Part 1.1 goal 4). Formulas and gating live in
                -- src/points.py and src/api_tracked_rides.py /
                -- src/api_ride_surveys.py; this CHECK only says the value is
                -- a legal thing for a ledger row to record.
                'battery_contribution', 'nav_route_feedback',
                'nav_qualitative_feedback', 'nav_distance_bonus', 'ride_survey'
            ));
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. The even-points invariant, as a database fact.
-- ---------------------------------------------------------------------------
-- RIDE_MODE_OVERHAUL_PLAN.md Decision 6 (owner's rule): "anywhere I offered
-- 5 make it 6. Intentionally points should always be even." Enforced THREE
-- ways in this program — this CHECK, an `assert points % 2 == 0` in
-- src/points.py:credit_points (right before the INSERT, after cap
-- trimming), and a test sweeping every POINTS_* constant and formula
-- output (tests/test_points_ride_mode.py, tests/test_points_schedule.py).
-- This is the outermost of the three: even if a future bug slipped past
-- the Python assert, the database itself refuses an odd row.
--
-- WHY HISTORICAL ROWS ALREADY SATISFY THIS, so there is nothing to
-- backfill (sql/041's "bring rows inside the cap BEFORE the constraint
-- that requires it" ordering rule, satisfied trivially here): every
-- POINTS_* constant in src/points.py has always been even (10, 4, 10, 10,
-- 2, 20, 100, 10), 2-per-waypoint rows are 2 * count (even * anything is
-- even), and the one thing that can shrink an award — the
-- MAX_POINTS_PER_RIDE = 100 cap in _apply_ride_cap — trims to an even
-- remainder because it is subtracting an even "already awarded" sum from
-- an even cap. Not asserted blindly: this constraint is added VALIDATED
-- (not NOT VALID), so it is checked against every existing row when it is
-- added, exactly like sql/041 step 4. If it ever fails here, that is this
-- constraint doing its job — the fix is a one-time
-- `SELECT COUNT(*) FROM user_points WHERE points % 2 = 1` audit against
-- production BEFORE shipping (sql/041's "backfill before ADD" step; there
-- is no generic backfill to write here because an odd row would mean a
-- real historical bug, not an expected state a migration can massage).
--
-- Guarded on conname ALONE — sql/041 step-4 shape, deliberately unlike
-- part 1 above. This is a fixed rule with no vocabulary to widen later, so
-- there is nothing to re-check the definition's contents against; leaving
-- any existing constraint of this name untouched is what the guard is for.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'user_points_points_even'
       AND conrelid = 'user_points'::regclass
       AND contype = 'c';

    IF current_def IS NULL THEN
        ALTER TABLE user_points
            ADD CONSTRAINT user_points_points_even
            CHECK (points % 2 = 0);
    END IF;
END $$;
