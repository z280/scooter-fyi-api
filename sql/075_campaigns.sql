-- Marketing-campaign attribution for the first-party user analytics
-- (sql/061_telemetry.sql). Admins create campaigns in /admin/campaigns,
-- share links tagged ?utm_campaign=<code>, and telemetry events carry the
-- code so acquisition can be measured per campaign.
--
-- PRIVACY SHAPE — same contract as 061, kept structurally:
--
--   * The campaign value on telemetry_events is a BOUNDED VOCABULARY, not
--     free text. Ingest (src/api_telemetry.py + src/campaigns.py) accepts
--     a client-supplied code only if it matches a live row in `campaigns`;
--     anything else collapses to the literal 'other', and untagged traffic
--     is 'none'. The column's distinct values are therefore bounded by the
--     number of campaigns an admin has created, plus two sentinels.
--   * No new identity is introduced: a campaign code identifies a LINK we
--     published, not a person. It joins to nothing account-shaped.
--
-- campaigns_daily is the per-campaign rollup (rollup_analytics in
-- src/analytics.py), aggregate and identity-free like the other *_daily
-- tables, so per-campaign history survives the 90-day raw-event pruning
-- and is kept indefinitely. Rows exist only for campaign <> 'none'.
--
-- city_id carried per MULTI_TENANCY_PLAN.md §9, same as 061: a tenant
-- dimension, NULL = Denver today.

CREATE TABLE IF NOT EXISTS campaigns (
    id          BIGSERIAL PRIMARY KEY,
    city_id     BIGINT,
    -- The utm_campaign value in shared links. Slug format enforced in
    -- code (src/campaigns.py CODE_RE), per house convention (sql/043).
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL DEFAULT '',
    -- Where the links live: 'qr-sticker', 'social', 'email', … — admin
    -- free text about our own marketing, never rider data.
    channel     TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Archived campaigns stop attributing: ingest treats their code as
    -- unknown ('other') from the moment this is set. History remains.
    archived_at TIMESTAMPTZ
);

ALTER TABLE telemetry_events
    ADD COLUMN IF NOT EXISTS campaign TEXT NOT NULL DEFAULT 'none';

-- Partial: campaign reporting only ever filters tagged traffic, and the
-- overwhelming bulk of rows is 'none'.
CREATE INDEX IF NOT EXISTS telemetry_events_campaign_received_at_idx
    ON telemetry_events (campaign, received_at)
    WHERE campaign <> 'none';

CREATE TABLE IF NOT EXISTS campaigns_daily (
    day            DATE NOT NULL,
    city_id        BIGINT,
    campaign       TEXT NOT NULL,
    events         INTEGER NOT NULL DEFAULT 0,
    visitors       INTEGER NOT NULL DEFAULT 0,
    sessions       INTEGER NOT NULL DEFAULT 0,
    page_loads     INTEGER NOT NULL DEFAULT 0,
    ride_completes INTEGER NOT NULL DEFAULT 0,
    auth_successes INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS campaigns_daily_key_idx
    ON campaigns_daily (day, campaign, COALESCE(city_id, -1));
