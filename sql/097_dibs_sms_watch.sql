-- Dibs, watched: text the claimant when the scooter they called goes out.
--
-- WHAT WAS MISSING. The app has had an "if somebody takes your scooter, tell
-- me" switch, and nothing behind it. The claim is registered here (sql/076),
-- the fleet is observed every cycle, and the two never met: dibs rows carried
-- no account, so there was nobody to text, and no opt-in, so no permission to.
--
-- WHAT WE CAN AND CANNOT OBSERVE, because the message has to be honest about
-- it. src/ride_watch.py measured this against production: a rented Veo stays
-- in the feed with `is_reserved` true for the rental's duration, and some
-- operators drop a rented vehicle instead. Either way, what we see is "a
-- rental started on this vehicle" — NOT who started it.
--
-- In particular we cannot tell the claimant's own rental from a stranger's.
-- The app tells us when it can (`mine_at` below, stamped when the rider says
-- they are riding it), but a rider who walks up and unlocks through Veo's own
-- app never passes through our code at all. So the alert says what was
-- observed rather than what it would like to conclude, and the one thing it
-- must never say is "somebody took your scooter" as a statement of fact.

-- Who to text. Nullable, because a claim without a session is still a valid
-- claim and still gets a certificate — it just cannot be watched.
ALTER TABLE dibs ADD COLUMN IF NOT EXISTS account_id BIGINT
    REFERENCES accounts(id) ON DELETE SET NULL;

-- The rider's answer, AS GIVEN FOR THIS CLAIM, copied in at claim time.
--
-- Deliberately not a lookup against a stored account preference. The switch
-- lives in the app, the claim is the thing being watched, and a claim made
-- while the switch was on should be honoured even if the rider turns it off
-- an hour later — just as a claim made while it was off should not start
-- texting because they turned it on. Same reasoning as `expires_at` being
-- copied from the rules rather than computed on read.
ALTER TABLE dibs ADD COLUMN IF NOT EXISTS notify_sms BOOLEAN NOT NULL DEFAULT FALSE;

-- When the fleet first showed this vehicle out on rental during the claim.
-- Set whether or not a text went out, so "did dibs get disrespected" is
-- answerable separately from "did we manage to tell anybody".
ALTER TABLE dibs ADD COLUMN IF NOT EXISTS taken_at TIMESTAMPTZ;

-- When we texted. ONCE PER CLAIM, and this column is the proof: the cycle
-- runs every couple of minutes and a rental lasts many of them, so without
-- it a single rental would text the rider twenty times.
ALTER TABLE dibs ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;

-- Why we did not text, when we did not. One of a small set of reasons
-- (no_phone, unverified, opted_out, unusable, quota, error) — kept because
-- "the switch is on and no text arrived" is otherwise unanswerable, and the
-- commonest reason by far will be a number nobody verified.
ALTER TABLE dibs ADD COLUMN IF NOT EXISTS notify_skipped TEXT;

-- The claimant said they are riding it. Suppresses the alert: the rental we
-- are about to see is theirs.
ALTER TABLE dibs ADD COLUMN IF NOT EXISTS mine_at TIMESTAMPTZ;

-- The watcher's one query, per cycle: open claims that asked to be watched
-- and have not been answered yet. Partial, and the predicate is deliberately
-- free of NOW() — an index predicate has to be immutable, so the expiry test
-- stays in the query. In practice this index holds a handful of rows.
CREATE INDEX IF NOT EXISTS idx_dibs_watchable
    ON dibs (expires_at)
    WHERE notify_sms
      AND account_id IS NOT NULL
      AND notified_at IS NULL
      AND notify_skipped IS NULL
      AND mine_at IS NULL;

COMMENT ON COLUMN dibs.notify_sms IS
    'The rider asked to be told if this scooter went out while their claim '
    'was live. Copied in at claim time, not looked up later: a claim made '
    'under one answer should not change behaviour because the switch moved.';
COMMENT ON COLUMN dibs.mine_at IS
    'The claimant told us they are riding it, so the rental we are about to '
    'observe is theirs. Absent for a rider who unlocked through Veo''s own '
    'app without passing through ours — which is why the alert reports what '
    'was observed rather than concluding who did it.';
COMMENT ON COLUMN dibs.notified_at IS
    'Set when the alert went out. The once-only guard: the ingest cycle runs '
    'every couple of minutes and a rental spans many of them.';
