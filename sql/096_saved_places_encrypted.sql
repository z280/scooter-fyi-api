-- Saved places, encrypted at rest.
--
-- WHAT THIS REPLACES, and why it is a new column rather than a type change on
-- the old one.
--
-- Riders' saved places live in three places today, all plaintext:
--
--   * accounts.favorites  — a JSONB column, client-writable through
--     PUT /api/v1/profile since sql/012, which the frontend never learned to
--     use. It may hold rows from an older build; it is treated as legacy data
--     to migrate, not as an empty column to drop.
--   * accounts.home_lat / home_lng — the rider's doorstep.
--   * accounts.work_lat / work_lng — where they work.
--
-- The last two are the sensitive ones. "Home", a latitude and a longitude next
-- to an email address and a phone number in one row is a dossier, and the rows
-- it sits in are the ones that reach a database dump, a backup bucket, a read
-- replica and whatever a SQL-injection bug can read. Volume encryption, which
-- the host already provides, protects a stolen disk and none of those.
--
-- So: a new TEXT column holding a Fernet token produced by
-- src/place_crypto.py. TEXT and not BYTEA because a Fernet token is already
-- urlsafe-base64 ASCII, and a BYTEA round trip would add an encoding to get
-- wrong for no gain.
--
-- A NEW COLUMN, AND THE OLD ONES STAY, which is the whole migration strategy
-- and is deliberate:
--
--   1. This migration adds the column and nothing else. It cannot encrypt
--      anything — Postgres does not hold the key, and it should not.
--   2. The API migrates each rider's row lazily, on their next profile read:
--      it decrypts what is there, folds in whatever the legacy columns hold,
--      and writes the encrypted blob back. A rider who never returns is never
--      touched, which is correct — we are not going to decrypt-and-rewrite a
--      table on their behalf and we do not need to.
--   3. A LATER migration drops the plaintext columns, once the traffic has
--      done the work and the figure can be checked.
--
-- Doing it in one step would mean a backfill script holding the encryption key
-- against production, with no way to verify the result afterwards (the point
-- of the column being that nothing can read it). Two steps with live traffic
-- in between is slower and leaves something to inspect.
--
-- NULL means "nothing saved, or not migrated yet", and those two are
-- deliberately not distinguished at the schema level. The API tells them apart
-- by looking at the legacy columns, which is the only place the difference is
-- knowable — and once the legacy columns are gone the difference stops
-- existing.

ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS saved_places_encrypted TEXT;

COMMENT ON COLUMN accounts.saved_places_encrypted IS
    'Fernet token (src/place_crypto.py) over the rider''s saved places. '
    'Superseding accounts.favorites and the home_/work_ lat/lng pairs, which '
    'stay until lazy migration has drained them. Never readable in SQL.';
