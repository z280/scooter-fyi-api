-- Widen auth_sessions.method to the set of sign-in doors that actually
-- exist. THIS IS A BUG FIX, not a feature.
--
-- sql/012 created the column as
--     CHECK (method IN ('google', 'magic_link'))
-- and nothing ever widened it. But src/api_auth.py:auth_code_verify has
-- been minting sessions with method='email_code' since the typed-code
-- door shipped (sql/022 added its login_codes table). The consequence, in
-- production:
--
--   POST /api/v1/auth/code/verify
--     -> the code is validated, the attempt counted, the code BURNED
--        (used_at set, single-use enforced)
--     -> upsert_account succeeds
--     -> mint_session raises CheckViolation on the very last INSERT
--     -> the whole transaction rolls back and the caller gets a 500
--
-- So the emailed-code door has never once produced a session. Confirmed
-- against the live database before writing this file: the constraint was
-- still the original pair, and SELECT DISTINCT method FROM auth_sessions
-- returned only 'google' and 'magic_link' — no 'email_code' row has ever
-- been written, which is exactly what a constraint that rejects every
-- attempt looks like from the outside.
--
-- (The rollback is the one mercy here: because the burn and the mint are
-- in the same transaction, a failed verify un-burns the code too, so a
-- rider retrying got another 500 rather than "code already used". That is
-- why this presented as a dead door and not as vanishing codes.)
--
-- 'sms_code' is added in the same statement rather than in the SMS
-- migration that needs it. The list is a single CHECK: widening it twice
-- means writing this same guarded DO block twice, and the second one would
-- have to re-derive the first's value set to avoid dropping it. One
-- authoritative list, one place.
--
-- REPLAY SAFETY: src/pg.py records applied files, but the _pg test
-- fixtures execute the whole sql/ directory on every run, and sql/040 and
-- sql/041 both had to be written to survive that. Same shape here — read
-- the live constraint definition first and rewrite only when a value we
-- need is missing, so replaying this AFTER a later migration widens the
-- list again is a no-op rather than a regression that reverts it.
--
-- No data migration is needed: the existing rows are all 'google' or
-- 'magic_link', which the widened list still permits. A CHECK that only
-- ever gets more permissive cannot invalidate stored rows.

DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'auth_sessions_method_allowed'
       AND conrelid = 'auth_sessions'::regclass
       AND contype = 'c';

    IF current_def IS NULL
       OR position('email_code' in current_def) = 0
       OR position('sms_code' in current_def) = 0
    THEN
        -- The auto-named inline CHECK from sql/012's CREATE TABLE, and the
        -- named one this file installs, so a partial earlier run leaves
        -- nothing behind.
        ALTER TABLE auth_sessions DROP CONSTRAINT IF EXISTS auth_sessions_method_check;
        ALTER TABLE auth_sessions DROP CONSTRAINT IF EXISTS auth_sessions_method_allowed;
        ALTER TABLE auth_sessions
            ADD CONSTRAINT auth_sessions_method_allowed
            CHECK (method IN ('google', 'magic_link', 'email_code', 'sms_code'));
    END IF;
END $$;
