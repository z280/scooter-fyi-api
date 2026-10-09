-- Giving a claim back is not the same as running out of time.
--
-- WHAT THE CERTIFICATE COULD NOT SAY. `release` expires a claim by setting
-- `expires_at = NOW()`, which is exactly the shape a natural twenty-five
-- minute expiry has. Nothing distinguished them, so the verification page
-- told every rider who handed a scooter back early that their dibs
-- "expired".
--
-- That is wrong twice. It understates the rider — they gave the scooter back
-- rather than ran out the clock — and it cuts the wrong way in the argument
-- the certificate exists to settle. Release at 14:05, somebody rides off at
-- 14:06, and "had dibs, expired at 14:05" invites "my dibs were fresh and you
-- took it" when in fact they had been handed back. The page's prominent "null
-- and void" limits the damage, but the record itself could not tell the two
-- apart.
--
-- sql/076's own words are that "a claim that was real and then given back is
-- a different thing from one that never happened". This is the column that
-- finally makes that true of the data rather than only of the intention.
--
-- NULL means expiry — including every claim released before this migration,
-- which is honest: we genuinely do not know which of those were given back.
ALTER TABLE dibs ADD COLUMN IF NOT EXISTS released_at TIMESTAMPTZ;

COMMENT ON COLUMN dibs.released_at IS
    'When the holder gave the claim back, as opposed to letting it run out. '
    'Set only by POST /api/v1/dibs/{id}/release, and only when that call '
    'actually released a live claim. NULL means it expired on its own clock, '
    'or was released before this column existed.';
