-- Ride specs: a rider's saved "ideal scooter".
--
-- ALONG_THE_WAY_PLAN.md §5. A spec is what a rider will ride — kind of
-- device, required features, minimum quality, minimum battery — with each
-- requirement marked MUST or PREFER. It is read by the trip search
-- (POST /api/v1/trip/candidates), which ranks against it and relaxes the
-- preferences in a published order before telling anybody there is nothing
-- for them.
--
-- WHY IT IS NOT A SAVED MAP SETTING, given that the two overlap almost
-- entirely in vocabulary. A map setting decides what is DRAWN; a spec
-- decides what the app will WALK YOU TO. A filter that hides something is a
-- view; a spec that excludes something sends you somewhere else. They are
-- bridged in the UI by one tap each way (plan §5.5) and deliberately not
-- fused: a rider narrowing the map to look at something must not thereby
-- change what the app walks them to two minutes later. Sharing one row would
-- make that impossible to keep apart.
--
-- A FOURTH KIND, NOT A FOURTH TABLE — sql/050's reasoning, unchanged. The
-- cardinality is the map-settings/Usuals one:
--
--   saved_map_settings   UNIQUE (account_id, name)  -> many, one per name
--   find_ride_pref       UNIQUE (account_id)        -> at most one
--   ride_mode_usual      UNIQUE (account_id, name)  -> many, one per name
--   ride_spec            UNIQUE (account_id, name)  -> many, one per name
--
-- The NAMESPACE IS SEPARATE, for sql/050's reason: the index below is
-- partial on kind, so a rider may hold a map setting, a Usual and a spec all
-- called 'commute' without them colliding. They are reached through
-- different endpoints and mean different things.
--
-- THE BLOB STAYS OPAQUE HERE, and that is a narrower claim than it looks.
-- src/api_preferences.py stores and returns it verbatim and never reads
-- inside it — same contract as every other kind in this table. The trip
-- search DOES interpret it, because interpreting it is that endpoint's whole
-- job. Those are different jobs and it is correct that only one of them
-- understands the shape; what must not happen is the preferences module
-- growing validation, which would put an API deploy in front of every new
-- client-side requirement.
--
-- Size and the per-account count are capped in src/api_preferences.py
-- (MAX_BLOB_BYTES, MAX_RIDE_SPECS = 5) and deliberately not here — sql/043's
-- header: both are product limits that will move. Five rather than the
-- Usuals' ten because a spec is picked at the top of a trip from a short
-- list, and a rider with ten of them has built a search problem.
--
-- REPLAY SAFETY. Both CHECK rewrites use the sql/040/041/042 guarded shape
-- sql/050 established: read the live constraint definition first and rewrite
-- only when 'ride_spec' is missing from it, so replaying this file after a
-- later migration adds a FIFTH kind is a no-op rather than a regression that
-- reverts that kind and rejects the rows it stored.
--
-- No data migration: both CHECKs only get more permissive, and no existing
-- row is a ride_spec, so nothing stored can be invalidated.

-- ---------------------------------------------------------------------------
-- 1. Teach the name/kind agreement rule about the new kind. MUST PRECEDE 2.
-- ---------------------------------------------------------------------------
-- user_preferences_name_matches_kind is a TOTAL rule: it enumerates every
-- kind and its name requirement, so a kind it has never heard of fails it no
-- matter what the kind list permits. Widening kind_allowed first would open a
-- window in which 'ride_spec' is a legal kind and every insert of one still
-- dies on the other constraint. Ordering it this way means neither half of
-- this file is ever the one that is live alone.
--
-- ride_spec REQUIRES a name: it is addressed BY its name
-- (GET/PUT/DELETE .../ride-specs/{name}), and the partial unique index in
-- step 3 is what makes that address unique. An unnamed spec would be
-- unreachable through the only API that reads it.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'user_preferences_name_matches_kind'
       AND conrelid = 'user_preferences'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('ride_spec' in current_def) = 0 THEN
        ALTER TABLE user_preferences
            DROP CONSTRAINT IF EXISTS user_preferences_name_matches_kind;
        ALTER TABLE user_preferences
            ADD CONSTRAINT user_preferences_name_matches_kind CHECK (
                (kind = 'saved_map_settings' AND name IS NOT NULL) OR
                (kind = 'ride_mode_usual'    AND name IS NOT NULL) OR
                (kind = 'ride_spec'          AND name IS NOT NULL) OR
                (kind = 'find_ride_pref'     AND name IS NULL)
            );
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Widen the kind list.
-- ---------------------------------------------------------------------------
-- As in sql/050: sql/043 named both constraints explicitly in the CREATE
-- TABLE, so there is no auto-named twin carrying an older value list that
-- could survive this DROP and go on rejecting 'ride_spec'.
--
-- Guarded on 'ride_spec' specifically rather than on the whole list, so this
-- file replayed after a fifth kind is added leaves that kind in place.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'user_preferences_kind_allowed'
       AND conrelid = 'user_preferences'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('ride_spec' in current_def) = 0 THEN
        ALTER TABLE user_preferences
            DROP CONSTRAINT IF EXISTS user_preferences_kind_allowed;
        ALTER TABLE user_preferences
            ADD CONSTRAINT user_preferences_kind_allowed
            CHECK (kind IN ('saved_map_settings', 'find_ride_pref',
                            'ride_mode_usual', 'ride_spec'));
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. One spec per (account, name).
-- ---------------------------------------------------------------------------
-- Partial on kind, exactly like idx_user_prefs_map_name and
-- idx_user_prefs_usual_name — so it constrains specs only, and is also the
-- arbiter src/api_preferences.py's upsert names in
-- `ON CONFLICT (account_id, name) WHERE kind = 'ride_spec'`. Without this
-- index that upsert has no inferrable arbiter and every PUT fails outright,
-- so the index is load-bearing for writes and not merely a uniqueness
-- opinion.
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_prefs_spec_name
    ON user_preferences (account_id, name)
    WHERE kind = 'ride_spec';

-- No new listing index: idx_user_prefs_account (sql/043) is already
-- (account_id, kind, updated_at DESC), which serves "this rider's specs,
-- newest first" as well as it serves the map settings and the Usuals.
