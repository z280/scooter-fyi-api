-- Rider-owned preference blobs: named "saved map settings" (many per
-- account) and a single "find ride" preference.
--
-- ONE TABLE, TWO KINDS, rather than two tables or two JSONB columns on
-- accounts. The two things are the same shape — an account, an opaque
-- client-owned blob, timestamps — and differ only in their CARDINALITY.
-- Cardinality is exactly what a partial unique index expresses, so the
-- difference lives in the schema instead of in application code that has
-- to remember it:
--
--   saved_map_settings   UNIQUE (account_id, name)  -> many, one per name
--   find_ride_pref       UNIQUE (account_id)        -> at most one
--
-- "EXACTLY ONE find_ride_pref" IS IMPLEMENTED AS "AT MOST ONE" + UPSERT.
-- The requirement asked for exactly one, and this deliberately does not
-- deliver that, because the only way to guarantee at-least-one is to
-- invent a default blob for every account at creation and backfill one
-- into every existing account. That blob would be a preference the rider
-- never expressed, indistinguishable on read from one they did — and the
-- frontend cannot tell "they want the defaults" from "they never chose",
-- which is the one distinction a preferences API exists to preserve. GET
-- returns null when unset; PUT creates or replaces. If a seeded default
-- is genuinely wanted, it belongs in a later migration that can say what
-- the default IS.
--
-- `settings` is never interpreted by the API — it is client-owned state,
-- stored and handed back verbatim. Size and per-account count are capped
-- in src/api_preferences.py rather than here: both are product limits
-- that will move, and a CHECK on jsonb length would turn a limit change
-- into a migration against every stored row.

CREATE TABLE IF NOT EXISTS user_preferences (
    id          BIGSERIAL PRIMARY KEY,
    account_id  BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL
                CONSTRAINT user_preferences_kind_allowed
                CHECK (kind IN ('saved_map_settings', 'find_ride_pref')),
    -- NULL for find_ride_pref (it has no name — there is only one), and
    -- required for a saved map setting, which is addressed BY its name.
    name        TEXT
                CONSTRAINT user_preferences_name_length
                CHECK (name IS NULL OR (length(name) BETWEEN 1 AND 64)),
    settings    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT user_preferences_name_matches_kind CHECK (
        (kind = 'saved_map_settings' AND name IS NOT NULL) OR
        (kind = 'find_ride_pref'     AND name IS NULL)
    )
);

-- One saved setting per (account, name). Partial, so it does not also
-- constrain find_ride_pref rows — whose name is NULL, and NULLs do not
-- conflict in a btree unique index anyway, but relying on that would make
-- the cardinality rule an accident of NULL semantics rather than a
-- statement.
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_prefs_map_name
    ON user_preferences (account_id, name)
    WHERE kind = 'saved_map_settings';

-- THE at-most-one rule, and the only thing enforcing it. A second
-- find_ride_pref insert for the same account raises UniqueViolation
-- rather than quietly giving the account two preferences, one of which
-- would be invisible depending on the read's ORDER BY.
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_prefs_find_ride
    ON user_preferences (account_id)
    WHERE kind = 'find_ride_pref';

-- Listing an account's saved settings, newest first.
CREATE INDEX IF NOT EXISTS idx_user_prefs_account
    ON user_preferences (account_id, kind, updated_at DESC);
