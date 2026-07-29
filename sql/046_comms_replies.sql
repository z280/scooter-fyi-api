-- Inbound SMS replies collected from z280-comms, and the consent record we
-- keep because of them.
--
-- Replies are POLLED, not pushed (z280/comms:docs/INTEGRATION.md), and the
-- poll CLAIMS what it returns: a reply handed to us is never handed out
-- again, even if our process dies a microsecond later. That single fact
-- dictates this table's shape — if we don't write the reply down at the
-- moment we collect it, nothing else will ever tell us it existed. So the
-- worker inserts first and interprets second, and `handled_at` (not the
-- row's existence) is what says we did something about it.
--
-- The id is comms' own reply id, as the primary key. That makes collection
-- idempotent for free: a redelivery we somehow see twice is an
-- ON CONFLICT DO NOTHING, not a duplicate row.

CREATE TABLE IF NOT EXISTS comms_replies (
    id            TEXT PRIMARY KEY,
    channel       TEXT,
    from_number   TEXT,
    body          TEXT,
    -- Comms' id for OUR message this answers, when it has one. Not a
    -- foreign key: we don't store outbound message ids, and SMS has no
    -- thread identifier to make this reliable anyway (see below).
    in_reply_to   TEXT,
    received_at   TIMESTAMPTZ,
    metadata      JSONB NOT NULL DEFAULT '{}',
    collected_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- What we decided this reply MEANT: 'stop', 'unstop', or 'other'.
    classified_as TEXT,
    -- Set when the worker finished acting on it AND comms accepted our ack.
    -- NULL on a row that is not brand new means a human should look: we
    -- collected it (so it is gone from comms' queue) and did not finish.
    handled_at    TIMESTAMPTZ
);

-- The "needs a human" query: collected, never handled.
CREATE INDEX IF NOT EXISTS idx_comms_replies_unhandled
    ON comms_replies (collected_at DESC)
    WHERE handled_at IS NULL;

-- Reading a rider's inbound history by number.
CREATE INDEX IF NOT EXISTS idx_comms_replies_from
    ON comms_replies (from_number, received_at DESC);

-- Our LOCAL record of a STOP/UNSTOP we were told about.
--
-- Comms remains authoritative on consent and enforces it for us — we
-- cannot send to someone who opted out, and we will get a 409 for people
-- we have never messaged, because a STOP to ANY application on the shared
-- number blocks all of them. This column exists so we can be honest in the
-- UI *before* trying (and so a rider's own record reflects a choice they
-- made), never as the thing that decides whether a send is allowed.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS sms_opted_out_at TIMESTAMPTZ;
