-- Model reports: "we're showing this vehicle as an unrecognized model —
-- tell us what it actually is."
--
-- The frontend surfaces a "Veo Unknown → Tell us!" form in the device popup
-- whenever vehicle_model_name isn't one we recognize, and has been POSTing
-- to /api/v1/reports/model since before the endpoint existed. This is the
-- backing store for that queue.
--
-- Anonymous is allowed (account_id nullable, IP-rate-limited): naming a
-- scooter model is not evidence about a rider, and requiring sign-in would
-- lose most of the corrections. Contrast discount_reports (sql/013), which
-- IS evidence and therefore requires a session.
--
-- Not a device_reports row: those are typed failure signals that feed
-- has_negative_report / reliability_tier. A model report is a catalog
-- correction and must never affect whether a scooter reads as rideable.
CREATE TABLE IF NOT EXISTS model_reports (
    id                   BIGSERIAL PRIMARY KEY,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Nullable: anonymous reports are accepted.
    account_id           BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
    -- The per-cycle GBFS device id the rider was looking at. Not an FK —
    -- device rows are archived out every 48h and the report outlives them.
    device_id            TEXT NOT NULL,
    -- Stable HMAC identifier when the frontend had one. 16 lowercase hex.
    vehicle_identifier   TEXT CHECK (
                             vehicle_identifier IS NULL
                             OR vehicle_identifier ~ '^[0-9a-f]{16}$'),
    description          TEXT NOT NULL CHECK (
                             char_length(description) BETWEEN 1 AND 2000),
    lat                  DOUBLE PRECISION CHECK (lat BETWEEN -90 AND 90),
    lng                  DOUBLE PRECISION CHECK (lng BETWEEN -180 AND 180),
    -- PRIVATE bucket, EXIF stripped on ingest (src/receipts.py's pipeline).
    -- A photo of a scooter is a photo of wherever the rider was standing.
    photo_r2_key         TEXT,
    photo_deleted_at     TIMESTAMPTZ,
    -- Review queue state. 'open' until an operator names the model.
    status               TEXT NOT NULL DEFAULT 'open'
                         CHECK (status IN ('open', 'resolved', 'rejected')),
    resolved_model_name  TEXT,
    reporter_ip          INET,
    reporter_user_agent  TEXT
);

-- The queue view: oldest open reports first.
CREATE INDEX IF NOT EXISTS idx_model_reports_open
    ON model_reports (created_at ASC) WHERE status = 'open';

-- "What has been reported about this vehicle?"
CREATE INDEX IF NOT EXISTS idx_model_reports_vehicle
    ON model_reports (vehicle_identifier) WHERE vehicle_identifier IS NOT NULL;
