-- Two fixed transaction-screenshot slots per ride (requirement #16):
-- 'overview' and 'receipt'. PRIVATE bucket — reuses R2_RECEIPTS_BUCKET
-- (no new bucket/env var), same EXIF-strip pipeline as src/receipts.py
-- via src/image_processing.py.
--
-- Re-uploading the same (ride, screenshot_type) OVERWRITES the prior
-- image rather than being rejected: there is no delete endpoint in this
-- feature, so rejecting a re-upload would strand a rider with a bad
-- screenshot forever. The app deletes the superseded R2 object on
-- overwrite (src/api_ride_screenshots.py).
--
-- ride_id references tracked_rides (sql/027), NOT the legacy `rides`
-- table — a UUID, matching that table's primary key type.
CREATE TABLE IF NOT EXISTS ride_transaction_screenshots (
    id                BIGSERIAL PRIMARY KEY,
    ride_id           UUID NOT NULL REFERENCES tracked_rides(id) ON DELETE CASCADE,
    account_id        BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    screenshot_type   TEXT NOT NULL
                      CONSTRAINT ride_transaction_screenshots_type_allowed
                      CHECK (screenshot_type IN ('overview', 'receipt')),
    r2_key            TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ride_screenshots_ride_type
    ON ride_transaction_screenshots (ride_id, screenshot_type);
CREATE INDEX IF NOT EXISTS idx_ride_screenshots_account
    ON ride_transaction_screenshots (account_id, created_at DESC);
