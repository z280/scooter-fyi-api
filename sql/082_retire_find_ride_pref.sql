-- Retire `find_ride_pref`: the ride spec is what it was reserved for.
--
-- WHAT IT WAS. sql/043 created a kind for "the rider's saved answer to what
-- they want when finding a ride" — at most one per account, its own partial
-- unique index, its own three endpoints. That is, word for word, what a ride
-- spec (sql/080) is.
--
-- WHAT IT ACTUALLY WAS. Nothing ever wrote one. No client code in
-- denver-scooter-fyi references the endpoints, and no server code outside
-- api_preferences.py's own CRUD touches the kind. It has been three routes, a
-- cardinality rule and an index serving nothing since the day it shipped.
--
-- So sql/080 built the feature this was reserved for, next to this, and the
-- two sat side by side meaning the same thing — which is how a rider (or the
-- next person reading the table) ends up unable to say which is which. One
-- of them has to go, and the one with no implementation is the one.
--
-- ORDERING, AND WHY IT IS THE REVERSE OF sql/050 AND sql/080 -----------------
-- Those files ADD a kind, so they widen `name_matches_kind` FIRST: it is a
-- TOTAL rule enumerating every kind, so a kind it has not heard of fails it
-- however permissive `kind_allowed` is.
--
-- Removing a kind inverts that. Both constraints are checked against every
-- stored row, so narrowing EITHER one while a `find_ride_pref` row exists
-- fails. The rows therefore go first, and after that the order of the two
-- narrowings cannot matter — which is why this file does not agonise over it
-- the way sql/050's header does.
--
-- ON DELETING ROWS. Production is expected to have none, and the DO block
-- below says how many it found in the boot log rather than deleting in
-- silence. If a row does exist it is unreachable data: no code path can read
-- it back, because the only readers were removed in this same change.

-- ---------------------------------------------------------------------------
-- 1. The rows. MUST PRECEDE both narrowings.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    doomed bigint;
BEGIN
    SELECT COUNT(*) INTO doomed
      FROM user_preferences WHERE kind = 'find_ride_pref';

    IF doomed > 0 THEN
        -- Said out loud on purpose: this is the only destructive step in the
        -- file, and a silent DELETE of rider-owned rows is not something a
        -- boot log should have to be reconstructed to find.
        RAISE NOTICE 'sql/082: deleting % unreachable find_ride_pref row(s)', doomed;
        DELETE FROM user_preferences WHERE kind = 'find_ride_pref';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. The index that enforced its at-most-one rule.
-- ---------------------------------------------------------------------------
-- sql/043 called this "THE at-most-one rule, and the only thing enforcing
-- it". With the kind gone it enforces a rule about rows that can no longer
-- exist.
DROP INDEX IF EXISTS idx_user_prefs_find_ride;

-- ---------------------------------------------------------------------------
-- 3. The name/kind agreement rule.
-- ---------------------------------------------------------------------------
-- Guarded on ABSENCE rather than presence, the mirror of sql/080's guard: a
-- replay after this file has run must not put the clause back, and a replay
-- of sql/080 (which runs first, in sorted order, and re-adds it) must be
-- undone again by this one. That pair is what makes the whole sql/ directory
-- replayable in order, which tests/test_migration_replay_pg.py checks.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'user_preferences_name_matches_kind'
       AND conrelid = 'user_preferences'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('find_ride_pref' in current_def) > 0 THEN
        ALTER TABLE user_preferences
            DROP CONSTRAINT IF EXISTS user_preferences_name_matches_kind;
        ALTER TABLE user_preferences
            ADD CONSTRAINT user_preferences_name_matches_kind CHECK (
                (kind = 'saved_map_settings' AND name IS NOT NULL) OR
                (kind = 'ride_mode_usual'    AND name IS NOT NULL) OR
                (kind = 'ride_spec'          AND name IS NOT NULL)
            );
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4. The kind list.
-- ---------------------------------------------------------------------------
-- EVERY REMAINING KIND IS NAMED, which is new: the table no longer has a
-- nameless cardinality to support. If a future kind is nameless, both this
-- constraint and the one above need the clause back — and sql/043's header,
-- which explains the one-table-two-cardinalities design, is the thing to read
-- first.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'user_preferences_kind_allowed'
       AND conrelid = 'user_preferences'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('find_ride_pref' in current_def) > 0 THEN
        ALTER TABLE user_preferences
            DROP CONSTRAINT IF EXISTS user_preferences_kind_allowed;
        ALTER TABLE user_preferences
            ADD CONSTRAINT user_preferences_kind_allowed
            CHECK (kind IN ('saved_map_settings', 'ride_mode_usual', 'ride_spec'));
    END IF;
END $$;

COMMENT ON TABLE user_preferences IS
    'Rider-owned preference blobs, one row per (account, kind, name). Three '
    'kinds, and they answer three different questions: saved_map_settings is '
    'what you want to LOOK AT, ride_spec is what you will RIDE, '
    'ride_mode_usual is how the ride SCREEN should behave. A fourth, '
    'find_ride_pref, was retired in sql/082 — it meant the same thing as '
    'ride_spec and was never implemented.';
