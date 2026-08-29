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

-- REPLAY SAFETY (added later, and the reason is worth recording).
--
-- These two rewrites were unconditional: DROP COLUMN then ADD COLUMN, every
-- time the file ran. Production applies each migration once
-- (src/pg.py:run_migrations records them in schema_migrations), so production
-- was never at risk — but the _pg test fixtures replay the WHOLE sql/
-- directory on every run, and Postgres never reclaims a dropped column's
-- attnum without a table rewrite. So each replay burned two of `accounts`'s
-- 1600 column slots, permanently, and a long enough test session eventually
-- died on `tables can have at most 1600 columns` in whichever test happened
-- to be last. Adding a new _pg test file was enough to tip it over.
--
-- Guarded on the EXPRESSION, not on the column's existence. The column is
-- not new here — sql/025 created it as `username_adjective || username_emoji`
-- — so this file's job is to REPLACE that definition, and an existence guard
-- would skip the replacement entirely and leave every fresh database on the
-- old, uncapitalised, unspaced formula. (That is exactly what a first attempt
-- at this guard did, and two _pg tests caught it.)
--
-- `upper(` appears in the new formula and in no earlier one, so its presence
-- is the marker for "already rewritten". Same shape as the constraint guards
-- in sql/040-042 and sql/050: read the live definition, act only when it is
-- not already the wanted one.
DO $$
DECLARE
    current_expr text;
BEGIN
    SELECT pg_get_expr(d.adbin, d.adrelid) INTO current_expr
      FROM pg_attribute a
      JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
     WHERE a.attrelid = 'accounts'::regclass
       AND a.attname = 'public_username'
       AND NOT a.attisdropped;

    IF current_expr IS NULL OR position('upper(' in current_expr) = 0 THEN
        ALTER TABLE accounts DROP COLUMN IF EXISTS public_username;
        ALTER TABLE accounts ADD COLUMN public_username TEXT
            GENERATED ALWAYS AS (
                upper(left(username_adjective, 1)) || substr(username_adjective, 2)
                || ' ' || username_emoji
            ) STORED;
    END IF;
END $$;

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
-- Same guard, same marker, same reason as public_username above. This column
-- is genuinely new in this file, so the NULL branch is the one that fires on
-- a fresh database — but it is guarded identically rather than left
-- unconditional, because "this one happens to be new" is a property that
-- stops holding the moment a later migration touches it.
DO $$
DECLARE
    current_expr text;
BEGIN
    SELECT pg_get_expr(d.adbin, d.adrelid) INTO current_expr
      FROM pg_attribute a
      JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
     WHERE a.attrelid = 'accounts'::regclass
       AND a.attname = 'display_name'
       AND NOT a.attisdropped;

    IF current_expr IS NULL OR position('upper(' in current_expr) = 0 THEN
        ALTER TABLE accounts DROP COLUMN IF EXISTS display_name;
        ALTER TABLE accounts ADD COLUMN display_name TEXT
            GENERATED ALWAYS AS (
                COALESCE(royalty_title || ' ', '')
                || upper(left(username_adjective, 1)) || substr(username_adjective, 2)
                || ' ' || username_emoji
            ) STORED;
    END IF;
END $$;
