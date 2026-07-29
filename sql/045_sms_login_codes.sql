-- SMS sign-in codes, and the phone-ownership proof that makes them safe.
--
-- Reuses login_codes (sql/022) rather than adding a parallel sms_codes
-- table: the attempt cap, the burn-on-success, the burn-on-reissue and the
-- opportunistic prune are subtle enough that a second implementation of
-- them is a second place to get them wrong. A code row now has exactly one
-- destination — an email or a phone number, never both and never neither.
--
-- WHY phone_verified_at exists, and why it is not merely nice-to-have:
-- PUT /api/v1/profile lets any signed-in rider write any E.164 string to
-- accounts.phone_number with no proof they own it. That is harmless while
-- the column is only a contact detail. The moment SMS sign-in exists it
-- becomes an AUTHENTICATION KEY, and an unverified one is an account
-- takeover: claim a stranger's number in your profile first, and their SMS
-- sign-in resolves to YOUR account. So the column that sign-in matches on
-- is a different one — set only by proving receipt of a code at that
-- number, never by the profile PUT.

ALTER TABLE login_codes ADD COLUMN IF NOT EXISTS phone_number TEXT;

-- login_codes.email was NOT NULL from sql/022 (email was the only door).
ALTER TABLE login_codes ALTER COLUMN email DROP NOT NULL;

-- Exactly one destination. `<>` on two booleans is XOR, so this rejects
-- both-null and both-set in one expression.
--
-- Guarded rather than bare: the _pg test fixtures replay the whole sql/
-- directory on every run, and ADD CONSTRAINT has no IF NOT EXISTS in the
-- Postgres versions this targets, so a second run would error out. Same
-- shape as the guards in sql/040-042.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'login_codes_one_destination'
           AND conrelid = 'login_codes'::regclass
    ) THEN
        ALTER TABLE login_codes
            ADD CONSTRAINT login_codes_one_destination
            CHECK ((email IS NULL) <> (phone_number IS NULL));
    END IF;
END $$;

-- Mirrors idx_login_codes_email_live: the verify path reads the newest
-- live code for one destination, and nothing else.
CREATE INDEX IF NOT EXISTS idx_login_codes_phone_live
    ON login_codes (phone_number, created_at DESC)
    WHERE used_at IS NULL;

-- Proof of ownership, set ONLY by a successful SMS code verification.
-- NULL means "a number is on file but nobody has ever proved they answer
-- it" — which is exactly the state a profile-written number starts in, and
-- a state SMS sign-in refuses to authenticate against.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS phone_verified_at TIMESTAMPTZ;
