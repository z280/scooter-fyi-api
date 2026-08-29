-- My Scooters: the specific vehicles a rider has kept.
--
-- ALONG_THE_WAY_PLAN.md §8. A rider who has physically stood at a scooter and
-- scanned the QR sticker under its handlebar can keep it — name it, find it
-- again next week, be told when it comes free. Ten per account.
--
-- (The plan numbered this 082 and the dibs swap chain 081; the swap chain has
-- not shipped, so this takes the next free number and that one moves to 082.
-- Sorted replay does not care, but "next free number" is the convention and a
-- deliberate hole would be the confusing thing, not the renumber.)
--
-- WHY A TABLE AND NOT ANOTHER user_preferences KIND. Three named kinds
-- already share that table and this looked at first like a fourth. It is not:
-- user_preferences holds an OPAQUE blob the server never reads, and every
-- column here is one the server reads — the gate's timestamp, the
-- notification opt-in, the vehicle it points at. It is also read by a
-- per-cycle job, which is not a shape that belongs behind "client-owned
-- state stored verbatim".
--
-- WHAT THE QR SCAN PROVES, AND WHAT IT DOES NOT ---------------------------
-- src/qr.py:validate_scan checks hash_plate(extract_plate(payload)) ==
-- vehicle_identifier. That proves the scanner HAS THE PLATE. It does not
-- prove they are standing anywhere near the scooter, and nothing in
-- src/api_qr.py or credit_qr_scan_points compares the submitted lat/lng to
-- anything at all.
--
-- Tolerable for a points bonus. Not tolerable for a feature whose whole
-- premise is "you were there", so src/api_favorites.py requires the scan AND
-- a fix within FAVORITE_PROXIMITY_METERS of the vehicle's last known
-- position.
--
-- The gate is ANTI-ABUSE, NOT PRIVACY. It stops idle favouriting and bot
-- enumeration; it does nothing at all about somebody scanning the scooter
-- parked outside a person's house. The privacy control is a different
-- mechanism entirely — see below — and conflating the two is the mistake
-- this comment exists to prevent.
--
-- THE POSITION THIS TABLE DOES NOT STORE -----------------------------------
-- Not where the rider was standing when they favourited. The 75 m check
-- happens at write time and the fix is then discarded. Nothing any feature
-- does reads it, and every stored position is a retention obligation across
-- three files (src/cli.py, src/api_meta.py, the privacy policy). The cheapest
-- privacy decision available is not to have the data.
--
-- THE POSITION THE API WILL NOT RETURN -------------------------------------
-- A favourite's position is withheld while the vehicle is in a rental.
-- src/ride_watch.py measured why: a rented Veo stays in the feed for the
-- whole rental, at 2-minute granularity, broadcasting its live moving
-- position with is_reserved true. /api/v1/devices/current publishes that,
-- publicly, today. So the capability already exists for anyone with a script
-- — but a favourite would turn it into a one-tap, persistent, TARGETED
-- subscription to one vehicle somebody physically located, which is the
-- difference between a public dataset and a tool for following a person.
--
-- The line is drawn where it costs the feature nothing: you may know where
-- your scooter is PARKED (already public, and the whole point of the
-- feature), and you may not watch it move. Enforced in the endpoint, not in
-- the client, and reported as an explicit `position_withheld` flag so a
-- later "fix" for the missing field has to argue with the name.

CREATE TABLE IF NOT EXISTS favorite_devices (
    id                  BIGSERIAL PRIMARY KEY,
    account_id          BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    -- Not a foreign key to device_state on purpose: a vehicle can leave the
    -- fleet, and a favourite that vanished from a rider's list the day Veo
    -- retired the scooter would be a worse answer than one that says
    -- "not seen since March".
    vehicle_identifier  TEXT NOT NULL,
    -- The rider's own name for it. NULL is fine — the vehicle already has a
    -- name (src/vehicle_identity.py), and "My Rover" is a nicety.
    nickname            TEXT
                        CONSTRAINT favorite_devices_nickname_length
                        CHECK (nickname IS NULL OR (length(nickname) BETWEEN 1 AND 40)),
    -- THE GATE, stored as a TIME and never as a PLACE. Refreshed when a
    -- rider re-scans a scooter they already keep.
    verified_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- "Tell me when it's free again." OFF by default: a favourite is a
    -- memory, and turning one into a notification is a second decision.
    notify_on_available BOOLEAN NOT NULL DEFAULT FALSE,
    -- Housekeeping rather than a feature: when the fleet last showed us this
    -- vehicle, so a retired scooter can age out of somebody's list instead of
    -- sitting there as a permanent "gone".
    last_seen_at        TIMESTAMPTZ,
    -- One row per (rider, vehicle). Re-favouriting refreshes verified_at
    -- rather than making a second row — a rider standing at their own
    -- scooter pressing the button again has not made a mistake.
    CONSTRAINT favorite_devices_unique UNIQUE (account_id, vehicle_identifier)
);

-- "This rider's list, newest first" — the only read the panel makes.
CREATE INDEX IF NOT EXISTS idx_favorite_devices_account
    ON favorite_devices (account_id, created_at DESC);

-- The availability watch's query: everybody who kept a vehicle that just came
-- free. Partial, because notify_on_available is opt-in and expected to stay a
-- minority — the same shape sql/069's idx_device_state_in_rental uses for the
-- same reason.
CREATE INDEX IF NOT EXISTS idx_favorite_devices_notify
    ON favorite_devices (vehicle_identifier)
    WHERE notify_on_available;

COMMENT ON TABLE favorite_devices IS
    'Vehicles a rider kept after proving at the kerb they were standing at '
    'one. Not a claim and not a reservation. The API withholds a favourite''s '
    'position while it is in a rental: you may know where yours is parked, '
    'you may not watch it move.';

COMMENT ON COLUMN favorite_devices.verified_at IS
    'When the rider last passed the QR-scan + proximity gate for this '
    'vehicle. A TIME, never a PLACE — the fix is checked at write time and '
    'discarded, because nothing reads it and every stored position is a '
    'retention rule.';
