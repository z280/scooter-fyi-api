-- Username presentation: capitalized adjective, space before the emoji.
--
-- "brave🦉" becomes "Brave 🦉", and display_name follows it:
-- "Queen brave🦉" becomes "Queen Brave 🦉". Only the PRESENTATION
-- changes — username_adjective/username_emoji keep storing the curated
-- lowercase word and the emoji exactly as sql/025 seeded them, so the
-- lexicon endpoints, the FK references and the rider's picker all carry
-- on unchanged. This migration rewrites the two generated columns that
-- compose them (sql/025's public_username, sql/044's display_name); the
-- Python side of the same formula lives in
-- src/accounts.py:format_public_username and MUST match character for
-- character, since assign_public_username compares its candidate string
-- against this column to detect a collision.
--
-- WHY DROP AND RE-ADD
-- -------------------
-- ALTER TABLE ... ALTER COLUMN ... SET EXPRESSION is Postgres 17; we run
-- 15 (docker-compose.yml). Dropping and re-adding a STORED generated
-- column is the supported way to change the formula there, and it is
-- lossless because the values are derived, never written. Dropping
-- public_username takes accounts_public_username_key with it, so the
-- constraint is recreated below. The rewrite recomputes every row, which
-- is what backfills existing accounts into the new format — there is no
-- separate UPDATE to run.
--
-- WHY NOT initcap()
-- -----------------
-- initcap capitalizes EVERY word ('easy-going' -> 'Easy-Going'), which
-- Python's str.capitalize does not. Every seeded adjective is a single
-- lowercase word today, so the two agree — but a later migration
-- extending sfw_adjectives with a hyphenated entry would silently split
-- this column from format_public_username, and a mismatch there reads as
-- "that username is free" when it isn't. Capitalizing the first
-- character only is the same operation in both languages, for any input.

ALTER TABLE accounts DROP COLUMN IF EXISTS public_username;
ALTER TABLE accounts ADD COLUMN public_username TEXT
    GENERATED ALWAYS AS (
        upper(left(username_adjective, 1)) || substr(username_adjective, 2)
        || ' ' || username_emoji
    ) STORED;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'accounts_public_username_key'
          AND conrelid = 'accounts'::regclass AND contype = 'u'
    ) THEN
        ALTER TABLE accounts
            ADD CONSTRAINT accounts_public_username_key UNIQUE (public_username);
    END IF;
END $$;

-- Same formula, prefixed by the title. Reads the parts rather than
-- public_username for sql/044's reason: Postgres forbids a stored
-- generated column referencing another stored generated column.
ALTER TABLE accounts DROP COLUMN IF EXISTS display_name;
ALTER TABLE accounts ADD COLUMN display_name TEXT
    GENERATED ALWAYS AS (
        COALESCE(royalty_title || ' ', '')
        || upper(left(username_adjective, 1)) || substr(username_adjective, 2)
        || ' ' || username_emoji
    ) STORED;
