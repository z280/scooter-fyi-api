-- User feedback on our quality_designation calls.
--
-- POST /api/v1/quality-feedback — public, no auth/rate-limit yet.
-- Intent: collect labels we can later use to train / tune the
-- quality_designation model. Each row captures one feedback event:
--   - which scooter (vehicle_identifier)
--   - where (h3_10_index — the cell the scooter was in when the
--     feedback was given)
--   - polarity ('positive' = "you got it right", 'negative' = "you got
--     it wrong")
--   - optional free-text comment
--
-- Reporter IP/UA captured for provenance only (no PII intended).

CREATE TABLE IF NOT EXISTS quality_feedback (
    id                    BIGSERIAL PRIMARY KEY,
    feedback_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    vehicle_identifier    TEXT NOT NULL,
    h3_10_index           BIGINT NOT NULL,
    polarity              TEXT NOT NULL CHECK (polarity IN ('positive', 'negative')),
    designation_observed  TEXT,                -- the quality_designation the user was reacting to
    comment               TEXT,
    reporter_ip           INET,
    reporter_user_agent   TEXT
);

CREATE INDEX IF NOT EXISTS idx_quality_feedback_vehicle
    ON quality_feedback (vehicle_identifier);
CREATE INDEX IF NOT EXISTS idx_quality_feedback_h3_10
    ON quality_feedback (h3_10_index);
CREATE INDEX IF NOT EXISTS idx_quality_feedback_at
    ON quality_feedback (feedback_at DESC);
