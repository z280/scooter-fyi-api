-- Frontend report ingestion (API_REQUIREMENTS.md §3).
--
-- device_reports:   rider-facing failure reports (failed_unlock /
--                    dead_battery / damaged). Anonymous allowed; when a
--                    session is presented the report is linked to the
--                    account. Feeds has_negative_report on /devices/current
--                    (same h3-anchored 24h staleness rule as
--                    negative_reports) and therefore reliability_tier.
--
-- discount_reports: missed equity-discount evidence. Requires a signed-in
--                    session (provenance); optional receipt image lives in
--                    a PRIVATE R2 bucket, EXIF-stripped, 18-month retention
--                    (src/cli.py cleanup_receipts).

CREATE TABLE IF NOT EXISTS device_reports (
    id                  BIGSERIAL PRIMARY KEY,
    reported_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    vehicle_identifier  TEXT NOT NULL,
    report_type         TEXT NOT NULL
                        CHECK (report_type IN ('failed_unlock', 'dead_battery', 'damaged')),
    observed_at         TIMESTAMPTZ,
    lat                 DOUBLE PRECISION,
    lng                 DOUBLE PRECISION,
    -- Anchored to the SCOOTER's current cell when coords are absent —
    -- same rationale as negative_reports (sql/008).
    h3_10_index         BIGINT,
    account_id          BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
    reporter_ip         INET,
    reporter_user_agent TEXT
);

CREATE INDEX IF NOT EXISTS idx_device_reports_live_lookup
    ON device_reports (vehicle_identifier, h3_10_index, reported_at DESC);
CREATE INDEX IF NOT EXISTS idx_device_reports_reported_at
    ON device_reports (reported_at DESC);
-- Dedupe probe: identical (vehicle, type, reporter) within 30 min. The
-- app queries this as two mutually-exclusive shapes (authenticated:
-- account_id = ?; anonymous: account_id IS NULL AND reporter_ip = ?), so
-- one index covers the authenticated case and a partial index covers the
-- anonymous case — a single index on (vehicle, type, reported_at) alone
-- would still have to scan+filter every recent row for that vehicle/type.
CREATE INDEX IF NOT EXISTS idx_device_reports_dedupe_auth
    ON device_reports (vehicle_identifier, report_type, account_id, reported_at DESC);
CREATE INDEX IF NOT EXISTS idx_device_reports_dedupe_anon
    ON device_reports (vehicle_identifier, report_type, reporter_ip, reported_at DESC)
    WHERE account_id IS NULL;

CREATE TABLE IF NOT EXISTS discount_reports (
    id                   BIGSERIAL PRIMARY KEY,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    account_id           BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    ride_ended_at        TIMESTAMPTZ NOT NULL,
    zone_version         TEXT NOT NULL CHECK (zone_version IN ('v1', 'v2')),
    end_lat              DOUBLE PRECISION,
    end_lng              DOUBLE PRECISION,
    amount_charged_cents INTEGER CHECK (amount_charged_cents >= 0),
    receipt_r2_key       TEXT,
    receipt_deleted_at   TIMESTAMPTZ,     -- set by the 18-month retention job
    reporter_ip          INET,
    reporter_user_agent  TEXT
);

CREATE INDEX IF NOT EXISTS idx_discount_reports_created_at
    ON discount_reports (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_discount_reports_account
    ON discount_reports (account_id);
