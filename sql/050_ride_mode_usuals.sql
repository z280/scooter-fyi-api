-- Ride Mode "Usuals": a rider's saved ride-options presets.
--
-- Screen 2 of the ride wizard (RIDE_MODE_OVERHAUL_PLAN §1.2) is eight
-- toggles the same rider sets the same way every morning. A "Usual" is one
-- saved answer to that screen, picked from Screen 2.5 and applied wholesale.
--
-- A THIRD KIND, NOT A THIRD TABLE. sql/043's header explains the shape:
-- user_preferences is (account, opaque client-owned blob, timestamps), and
-- the kinds differ only in their CARDINALITY, which a partial unique index
-- expresses in the schema instead of in application code that has to
-- remember it:
--
--   saved_map_settings   UNIQUE (account_id, name)  -> many, one per name
--   find_ride_pref       UNIQUE (account_id)        -> at most one
--   ride_mode_usual      UNIQUE (account_id, name)  -> many, one per name
--
-- A Usual has exactly the map-settings cardinality, so it gets exactly the
-- map-settings treatment: named, addressed by its name, replaced wholesale
-- by PUT. A separate table would duplicate five columns, two constraints and
-- four handlers to store the same thing under a different noun.
--
-- The NAMESPACES ARE SEPARATE even though both indexes cover
-- (account_id, name): the new index is partial on kind, so a rider may hold
-- a map setting called 'commute' AND a Usual called 'commute'. They are
-- reached through different endpoints and mean different things; making one
-- collide with the other would be an accident of sharing a table.
--
-- WHAT IS IN THE BLOB, AND WHY THE SCHEMA DOESN'T SAY. A Usual's `settings`
-- is the frontend's ride_options object plus a display `label`. That is a
-- contract between the wizard and itself — the API stores it and hands it
-- back verbatim, exactly as it does for a saved map setting. Note the blob
-- is NOT the same object as tracked_rides.ride_options (sql/049): that
-- column is what a rider actually rode under and the server gates awards on
-- it, which is why THAT one is shape-checked in the handler. A Usual is a
-- draft of an intention, validated when it is used to start a ride, and the
-- one place that validation lives is api_tracked_rides._serialize_ride_options.
--
-- Size and the per-account count are capped in src/api_preferences.py
-- (MAX_BLOB_BYTES, MAX_RIDE_USUALS = 10) and deliberately not here — sql/043's
-- header: both are product limits that will move, and a CHECK on jsonb length
-- would turn a limit change into a migration against every stored row.
--
-- REPLAY SAFETY. src/pg.py records applied files, but the _pg test fixtures
-- execute the whole sql/ directory on every run, so both CHECK rewrites below
-- use the sql/040/041/042 guarded shape: read the live constraint definition
-- first and rewrite only when 'ride_mode_usual' is missing from it. Replaying
-- this file AFTER a later migration adds a FOURTH kind is then a no-op rather
-- than a regression that reverts that kind and rejects the rows it stored.
--
-- No data migration: both CHECKs only get more permissive, and no existing
-- row is a ride_mode_usual, so nothing stored can be invalidated.

-- ---------------------------------------------------------------------------
-- 1. Teach the name/kind agreement rule about the new kind. MUST PRECEDE 2.
-- ---------------------------------------------------------------------------
-- user_preferences_name_matches_kind is a TOTAL rule: it enumerates every
-- kind and its name requirement, so a kind it has never heard of fails it no
-- matter what the kind list permits. Widening kind_allowed first would
-- therefore open a window in which 'ride_mode_usual' is a legal kind and
-- every insert of one still dies on the other constraint. Ordering it this
-- way means neither half of this file is ever the one that is live alone.
--
-- ride_mode_usual REQUIRES a name for the same reason a saved map setting
-- does: it is addressed BY its name (GET/PUT/DELETE .../ride-usuals/{name}),
-- and the partial unique index in step 3 is what makes that address unique.
-- An unnamed Usual would be unreachable through the only API that reads it.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'user_preferences_name_matches_kind'
       AND conrelid = 'user_preferences'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('ride_mode_usual' in current_def) = 0 THEN
        ALTER TABLE user_preferences
            DROP CONSTRAINT IF EXISTS user_preferences_name_matches_kind;
        ALTER TABLE user_preferences
            ADD CONSTRAINT user_preferences_name_matches_kind CHECK (
                (kind = 'saved_map_settings' AND name IS NOT NULL) OR
                (kind = 'ride_mode_usual'    AND name IS NOT NULL) OR
                (kind = 'find_ride_pref'     AND name IS NULL)
            );
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Widen the kind list.
-- ---------------------------------------------------------------------------
-- NO AUTO-NAMED TWIN TO DROP, unlike sql/040/041/042. Those files had to drop
-- a Postgres-generated *_check alongside the named constraint because their
-- predecessors wrote the value list as an unnamed inline CHECK. sql/043 named
-- both of the constraints this file touches explicitly in the CREATE TABLE, so
-- the named DROP below is the whole cleanup — there is no second copy of the
-- old list hiding under a generated name that could survive and reject
-- 'ride_mode_usual' after this succeeds.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'user_preferences_kind_allowed'
       AND conrelid = 'user_preferences'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('ride_mode_usual' in current_def) = 0 THEN
        ALTER TABLE user_preferences
            DROP CONSTRAINT IF EXISTS user_preferences_kind_allowed;
        ALTER TABLE user_preferences
            ADD CONSTRAINT user_preferences_kind_allowed
            CHECK (kind IN ('saved_map_settings', 'find_ride_pref', 'ride_mode_usual'));
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. One Usual per (account, name).
-- ---------------------------------------------------------------------------
-- Partial on kind, exactly like idx_user_prefs_map_name — so it constrains
-- Usuals only, and is also the arbiter src/api_preferences.py's upsert names
-- in `ON CONFLICT (account_id, name) WHERE kind = 'ride_mode_usual'`. Without
-- this index that upsert has no inferrable arbiter and every PUT fails, so the
-- index is load-bearing for writes and not merely a uniqueness opinion.
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_prefs_usual_name
    ON user_preferences (account_id, name)
    WHERE kind = 'ride_mode_usual';

-- No new listing index: idx_user_prefs_account (sql/043) is already
-- (account_id, kind, updated_at DESC), which serves "this rider's Usuals,
-- newest first" as well as it serves the map settings.
