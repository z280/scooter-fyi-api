-- The stand-down offer on a certificate page.
--
-- WHO IS READING THAT PAGE. A dibs certificate is scanned by exactly one kind
-- of person: somebody standing at the same scooter, who wanted it. The page
-- already tells them the claim is real and offers them an account. This adds
-- the thing they might actually want in that moment — payment for walking
-- away — because the alternative outcome is two people arguing on a pavement
-- and one of them deleting the app.
--
-- It rides on `referrals` (sql/076) rather than a table of its own: the row
-- shape is identical (a contact, a position, an origin claim, an activation
-- to wait for) and the only differences are who is owed and how much. A
-- second near-identical table would have to be joined into every payout query
-- forever.
--
-- TWO PAYOUTS, TWO PAYEES, ONE ROW:
--
--   referrer_username / points        the dibs holder, for the introduction
--                                     (100, unchanged)
--   newcomer_points                   the person who stood down, for standing
--                                     down (300)
--
-- `points` was already "what the referrer is owed", so its meaning is
-- untouched; the new column is a separate debt to a separate person. Both are
-- still gated on activation — a lead is not a referral, and somebody who fills
-- in a phone number and never rides has not stood down from anything, they
-- have typed into a box.

ALTER TABLE referrals
    -- 'referral' — the ordinary "sign up and my friend gets points".
    -- 'stand_down' — "I wanted this scooter, I am letting them have it."
    -- Free text rather than an enum, per house convention (sql/043); the
    -- vocabulary is enforced in src/api_dibs.py.
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'referral',
    -- What the NEWCOMER is owed, as distinct from `points` (the referrer's).
    -- Zero for an ordinary referral, which is why it defaults to 0 rather
    -- than to 300: the common row owes the newcomer nothing.
    ADD COLUMN IF NOT EXISTS newcomer_points INTEGER NOT NULL DEFAULT 0,
    -- The offer is same-day ("start a ride on scooter.fyi today"), so the
    -- deadline is stored rather than recomputed. A promise whose expiry is
    -- derived at payout time is a promise that quietly changes when the
    -- derivation does, and this one is printed on a page somebody screenshots.
    ADD COLUMN IF NOT EXISTS newcomer_deadline TIMESTAMPTZ;

-- Pending stand-down payouts, which is a different question from pending
-- referral payouts: a different payee, a different amount, and a deadline that
-- can pass. Partial, because stand-downs are a small minority of rows.
CREATE INDEX IF NOT EXISTS idx_referrals_stand_down
    ON referrals (activated_at, newcomer_deadline)
    WHERE kind = 'stand_down' AND awarded_at IS NULL;
