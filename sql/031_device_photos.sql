-- Device photos (requirements #12/14) + reports filed against a photo
-- (requirement #13 — distinct from device_reports, which reports on the
-- DEVICE, not the photo). Bundled in one migration since they ship as one
-- feature, same convention as sql/013_frontend_reports.sql bundling
-- device_reports + discount_reports.
--
-- PUBLIC bucket (R2_BUCKET_NAME, prefix `device-photos/`) — device
-- photos must be publicly viewable (item 14), unlike the private
-- receipts bucket. Cap of 3 photos per device is enforced in the app
-- (src/api_device_photos.py), guarded by the same
-- pg_advisory_xact_lock(hashtextextended(...)) pattern src/ratelimit.py
-- uses to close the "two concurrent inserts both observe count < cap"
-- race. Attribution to the uploader's public_username happens at READ
-- time via a join to accounts (sql/025) — never denormalized here, since
-- a username can change after posting.
CREATE TABLE IF NOT EXISTS device_photos (
    id                  BIGSERIAL PRIMARY KEY,
    vehicle_identifier  TEXT NOT NULL,
    account_id          BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    r2_key              TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status              TEXT NOT NULL DEFAULT 'visible'
                        CONSTRAINT device_photos_status_allowed
                        CHECK (status IN ('visible', 'hidden'))
);

CREATE INDEX IF NOT EXISTS idx_device_photos_vehicle
    ON device_photos (vehicle_identifier, created_at);
CREATE INDEX IF NOT EXISTS idx_device_photos_account
    ON device_photos (account_id, created_at DESC);

-- `reason` taxonomy: wrong_device maps to the points list's "photo of
-- device is wrong" (future functionality — points not wired up for it
-- yet); the other two are general buckets. `status` supports a future
-- moderator-adjudication workflow without a schema rework — rows are
-- inserted 'open' and nothing transitions them yet.
CREATE TABLE IF NOT EXISTS device_photo_reports (
    id          BIGSERIAL PRIMARY KEY,
    photo_id    BIGINT NOT NULL REFERENCES device_photos(id) ON DELETE CASCADE,
    account_id  BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    reason      TEXT NOT NULL
                CONSTRAINT device_photo_reports_reason_allowed
                CHECK (reason IN ('wrong_device', 'inappropriate', 'other')),
    comment     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status      TEXT NOT NULL DEFAULT 'open'
                CONSTRAINT device_photo_reports_status_allowed
                CHECK (status IN ('open', 'resolved', 'dismissed'))
);

CREATE INDEX IF NOT EXISTS idx_device_photo_reports_photo ON device_photo_reports (photo_id);
CREATE INDEX IF NOT EXISTS idx_device_photo_reports_status ON device_photo_reports (status, created_at DESC);
-- One report per (account, photo) — resubmitting the same complaint
-- shouldn't spam the future moderation queue with duplicates.
CREATE UNIQUE INDEX IF NOT EXISTS idx_device_photo_reports_dedupe
    ON device_photo_reports (photo_id, account_id);
