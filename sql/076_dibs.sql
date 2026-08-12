-- Dibs: a rider's timestamped claim on a scooter.
--
-- WHY THIS IS SERVER-SIDE AT ALL. Dibs started on the phone, and for the claim
-- itself that was right — it is a social object, not a lock, and a server
-- registry that let one rider's dibs block another's ride would be a promise
-- the app has no standing to make. Veo does not do reservations and neither do
-- we.
--
-- But the CERTIFICATE is different. Its whole purpose is to be shown to
-- somebody who has no reason to trust the person holding the phone, and a
-- claim stored only in that person's localStorage is one they can edit. A
-- timestamp anybody can rewrite settles no argument.
--
-- So the claim is recorded here and the certificate carries a scannable link
-- back to it. What that buys, precisely:
--
--   * the TIMESTAMP is the server's, not the phone's. A traveller with a
--     wrong clock, or somebody who set theirs back deliberately, cannot win
--     an argument they should lose.
--   * the other person can verify it themselves, on their own phone, without
--     installing anything.
--   * the page they land on is the app's front door — which is the other half
--     of why the certificate exists.
--
-- Still not enforcement. Nothing here prevents anybody from riding anything.

CREATE TABLE IF NOT EXISTS dibs (
    -- Short, URL-safe, and unguessable: it goes in a QR and then into a
    -- stranger's address bar. Not sequential — a browsable list of who called
    -- dibs on what, keyed by an integer anyone can increment, is a privacy
    -- leak dressed as a primary key.
    id               TEXT PRIMARY KEY,
    vehicle_identifier TEXT NOT NULL,
    -- Denormalised on purpose. The certificate has to render months later,
    -- after the vehicle has left the fleet, and still say what it was for.
    vehicle_name     TEXT NOT NULL,
    plate            TEXT,
    -- Display name at claim time, or the anonymous form. Never fabricated —
    -- the artifact is an assertion about who did what.
    claimed_by       TEXT NOT NULL,
    -- THE FIELD THE WHOLE THING TURNS ON, and the reason this table exists:
    -- set by the database, never by the client.
    claimed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Copied from the rules in src/dibs.ts at claim time rather than computed
    -- on read, so a later change to the rules cannot retroactively extend or
    -- void certificates already handed out.
    expires_at       TIMESTAMPTZ NOT NULL,
    -- Whether the rider had already set off when they last checked in. Drives
    -- nothing here; recorded so the "did dibs change behaviour" question is
    -- answerable later.
    started_walking  BOOLEAN NOT NULL DEFAULT FALSE
);

-- "Does this rider already hold a live claim on this vehicle?" — the only
-- query the write path makes.
CREATE INDEX IF NOT EXISTS idx_dibs_vehicle_claimed
    ON dibs (vehicle_identifier, claimed_at DESC);

-- Housekeeping: certificates are meant to be verifiable after they expire
-- (that is half their point — "I had dibs, you took it anyway"), so rows are
-- kept well past expiry and swept on age, not on validity.
CREATE INDEX IF NOT EXISTS idx_dibs_claimed_at ON dibs (claimed_at);

COMMENT ON TABLE dibs IS
    'Rider claims on vehicles. The server timestamp is the point: a '
    'certificate stored only on the claimant''s phone is one they can edit, '
    'and a timestamp anybody can rewrite settles no argument. Not '
    'enforcement — nothing here prevents anyone riding anything.';


-- The two campaign codes the certificate uses, registered so telemetry
-- attributes them instead of collapsing them to 'other'.
--
-- src/campaigns.py resolves a client-sent utm_campaign against this registry:
-- a well-formed but UNREGISTERED code is recorded as 'other', which is exactly
-- what would have happened to every dibs scan if these rows did not exist. The
-- QR would have worked, the scans would have landed, and the campaign report
-- would have read zero.
--
--   dibs             a stranger scanned the QR on somebody's certificate
--   dibs-validation  ...and then clicked through to the app from the
--                     verification page. A much stronger signal, and worth
--                     separating: one is curiosity, the other is intent.
INSERT INTO campaigns (code, name, channel, notes, created_by)
VALUES
  ('dibs', 'Dibs certificate QR', 'qr',
   'Scanned off a rider''s certificate of dibs, in person.', 'system'),
  ('dibs-validation', 'Dibs validation page', 'referral',
   'Clicked through to the app from a dibs verification page.', 'system')
ON CONFLICT (code) DO NOTHING;

-- What the certificate says about the vehicle. Denormalised for the same
-- reason vehicle_name is: the page has to render months later, after the
-- vehicle has left the fleet.
ALTER TABLE dibs ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'Veo';
ALTER TABLE dibs ADD COLUMN IF NOT EXISTS device_type TEXT NOT NULL DEFAULT '';
-- Where the rider was standing when they called it. Carried onto any referral
-- made from this certificate, because a referral's value is partly WHERE it
-- happened — a signup won at a light-rail stop is a different fact from one
-- won in a suburb, and the points are awarded against that spot.
ALTER TABLE dibs ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION;
ALTER TABLE dibs ADD COLUMN IF NOT EXISTS lon DOUBLE PRECISION;

-- Referrals.
--
-- A rider shows somebody their certificate; that person signs up from the
-- verification page; the rider gets 100 points once the newcomer is ACTIVE,
-- not merely registered. The distinction is the whole integrity of the
-- scheme: paying on signup rewards address harvesting, paying on activity
-- rewards introducing somebody who actually rides.
CREATE TABLE IF NOT EXISTS referrals (
    id            BIGSERIAL PRIMARY KEY,
    -- The certificate it came from, when it came from one. Null for a
    -- referral entered by hand from the profile page.
    dibs_id       TEXT REFERENCES dibs(id) ON DELETE SET NULL,
    -- Who gets the points. Stored as the public username rather than an
    -- account id so a referral survives the referrer changing their handle,
    -- and so the page can print it without a join.
    referrer_username TEXT NOT NULL,
    -- Contact the newcomer gave. One of these is enough; both is better.
    -- NOT unique together on purpose: two people may legitimately refer the
    -- same person, and deciding who "wins" is a policy question, not a
    -- constraint. The award path below is what enforces one payout.
    email         TEXT,
    phone         TEXT,
    -- Where the referral was made — inherited from the dibs claim, or the
    -- referrer's fix when entered by hand.
    lat           DOUBLE PRECISION,
    lon           DOUBLE PRECISION,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Set when the newcomer first signs in AND does something. Until then
    -- this is a lead, not a referral.
    activated_at  TIMESTAMPTZ,
    -- Set when the points actually land, so a retry cannot pay twice.
    awarded_at    TIMESTAMPTZ,
    points        INTEGER NOT NULL DEFAULT 100,
    CONSTRAINT referrals_has_contact CHECK (
        (email IS NOT NULL AND email <> '') OR (phone IS NOT NULL AND phone <> '')
    )
);

-- "Has this person already been referred, and by whom?" — the lookup the
-- activation path makes, on both contact fields.
CREATE INDEX IF NOT EXISTS idx_referrals_email ON referrals (lower(email))
    WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_referrals_phone ON referrals (phone)
    WHERE phone IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals (referrer_username, created_at DESC);
-- Pending payouts: activated but not yet awarded.
CREATE INDEX IF NOT EXISTS idx_referrals_unpaid ON referrals (activated_at)
    WHERE activated_at IS NOT NULL AND awarded_at IS NULL;

COMMENT ON TABLE referrals IS
    'Rider-to-rider introductions, from a dibs certificate or entered by hand. '
    'Points land on ACTIVATION, not signup: paying on signup rewards address '
    'harvesting, paying on activity rewards introducing somebody who rides.';
