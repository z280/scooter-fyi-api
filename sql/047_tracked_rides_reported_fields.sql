-- Rider-reported ride facts (FEATURE_PLAN_2026-07.md §10): how many
-- minutes the operator's app said the ride was, and which rate-plan tier
-- the rider says they rode under.
--
-- Both are INERT STORED FACTS. Nothing in the close-out logic reads them:
-- not distance, not clamping, not points. They exist so the number the
-- rider was shown by the operator is recorded next to the numbers we
-- observed ourselves, and the two are allowed to disagree.
--
--   reported_minutes  is NOT reconciled against
--                     user_reported_ended_at - started_at. The whole point
--                     of a reported field is that it can differ from what
--                     we observed; comparing them is an analytics
--                     question, not a validation one. Capped at 24 h for
--                     the same reason the 80 km distance cap exists
--                     (sql/041) — a number we would not stand behind
--                     should not enter the table.
--   reported_plan     reuses the ('resident','visitor','equity')
--                     vocabulary from accounts.rate_plan and
--                     rides.rate_plan — confirmed by the operator
--                     2026-07-28 as the rate-plan tier, not a Veo pass
--                     product. Note the asymmetry this creates ON PURPOSE:
--                     accounts.rate_plan is the plan the rider says they
--                     are ON, tracked_rides.reported_plan is the plan they
--                     say they RODE UNDER, and the two can legitimately
--                     disagree on any given ride.
--
-- reported_cost is deliberately NOT added: total_cost_cents already IS
-- that field, and two rider-reported cost columns could disagree about
-- what one person paid.
--
-- ---------------------------------------------------------------------------
-- WHY THE COLUMNS ARE ADDED BARE AND THE CHECKS ARE ADDED SEPARATELY
-- ---------------------------------------------------------------------------
-- §10's published DDL inlines both CHECKs inside ADD COLUMN IF NOT EXISTS.
-- That shape is a trap and this file does not use it: `ADD COLUMN IF NOT
-- EXISTS` skips the ENTIRE subcommand — constraint included — once the
-- column exists, so the CHECK is silently never installed on any database
-- where the column arrived first (and it can never be repaired by
-- re-running the file either). Same rule the sql/040-042 headers state.
--
-- So: bare ADD COLUMN, then each CHECK as its own explicitly NAMED
-- constraint behind its own guard. The two guards are DIFFERENT SHAPES and
-- per sql/041's step-3/step-4 rule they must not be made to match:
--
--   reported_plan    is an ENUMERATED LIST -> value-checked guard
--                    (sql/040 / sql/042 shape). A replay has to be able to
--                    tell "a value I need is missing" from "already fine",
--                    so it reads pg_get_constraintdef. That makes a replay
--                    after a LATER migration widens the list a no-op
--                    instead of a regression that rejects rows the later
--                    migration stored.
--   reported_minutes is a NUMERIC BOUND -> conname-only guard (sql/041
--                    step-4 shape). If a later migration moves the cap, a
--                    value check here would see "1440 is absent", fire, and
--                    revert the move. Leaving any existing constraint of
--                    this name untouched is what makes a later change
--                    stick.
--
-- ORDERING / BACKFILL (sql/041's rule: bring rows inside a constraint
-- BEFORE adding it). Nothing to backfill here. Both columns are new in
-- this file, so on a first run every existing tracked_rides row holds NULL
-- and both CHECKs admit NULL. On a replay the constraints already exist
-- and their guards skip. The one way a row could ever be out of bounds is
-- a writer that bypasses the API, which is precisely what these
-- constraints exist to stop — they are added VALIDATED (not NOT VALID) so
-- that is true of stored rows and not only of future writes; the one-time
-- scan is bounded by a rider-owned table.

ALTER TABLE tracked_rides
    ADD COLUMN IF NOT EXISTS reported_minutes INTEGER,  -- as the operator's app reported it
    ADD COLUMN IF NOT EXISTS reported_plan    TEXT;     -- rate-plan tier ridden under

-- ---------------------------------------------------------------------------
-- reported_plan: enumerated list, value-checked guard.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'tracked_rides_reported_plan_allowed'
       AND conrelid = 'tracked_rides'::regclass
       AND contype = 'c';

    IF current_def IS NULL
       OR position('resident' in current_def) = 0
       OR position('visitor'  in current_def) = 0
       OR position('equity'   in current_def) = 0
    THEN
        -- The named constraint this file installs, so a partial earlier run
        -- leaves nothing behind — plus the auto-name Postgres would have
        -- given §10's published inline CHECK, in case that DDL was ever
        -- applied by hand to a database this file later runs against.
        ALTER TABLE tracked_rides DROP CONSTRAINT IF EXISTS tracked_rides_reported_plan_check;
        ALTER TABLE tracked_rides DROP CONSTRAINT IF EXISTS tracked_rides_reported_plan_allowed;
        ALTER TABLE tracked_rides
            ADD CONSTRAINT tracked_rides_reported_plan_allowed
            CHECK (reported_plan IS NULL
                   OR reported_plan IN ('resident', 'visitor', 'equity'));
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- reported_minutes: numeric bound, conname-only guard.
-- ---------------------------------------------------------------------------
-- Guarded on conname ALONE, deliberately unlike the block above. See the
-- header: a value check here would revert a later migration that moves the
-- 1440-minute bound. NULL passes — a rider need not report a duration, and
-- the constraint is a statement about reported durations, not a
-- requirement that one exist.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'tracked_rides_reported_minutes_range'
       AND conrelid = 'tracked_rides'::regclass
       AND contype = 'c';

    IF current_def IS NULL THEN
        ALTER TABLE tracked_rides
            ADD CONSTRAINT tracked_rides_reported_minutes_range
            CHECK (reported_minutes IS NULL
                   OR reported_minutes BETWEEN 0 AND 1440);
    END IF;
END $$;
