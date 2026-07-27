-- User points ledger (requirement #10). Append-only; an account's running
-- total is SUM(points) — never a cached counter — so it's always
-- reconstructible and immune to drift.
--
-- Every row records WHERE the action happened: lat/lng plus an H3
-- resolution-8 cell computed server-side the same way src/ingest.py
-- computes h3_8_index (see src/points.py:h3_8_index_for) — signed BIGINT,
-- same convention as sql/006_h3_indexes.sql. Rows are always tied to an
-- account (points are never anonymous); vehicle_identifier is set only
-- for device-linked actions (NULL for profile_completion).
--
-- `status` exists so a future moderator-approval workflow (improper-
-- parking-with-proof-screenshot, disputed device photos — both still
-- "future functionality" as of this migration) can insert a
-- 'pending_review' row and flip it to 'confirmed' later without a schema
-- change. Nothing wired up in this migration's era ever inserts
-- 'pending_review' — every credit path goes straight to 'confirmed'.
--
-- Point AMOUNTS live in src/points.py (POINTS_* constants), not here, so
-- a value tweak is a code change, not a migration.
--
-- source_table/source_id (TEXT, not BIGINT: sources include both
-- device_reports.id [bigint] and tracked_rides.id [uuid], stored as
-- text) + the partial unique index below make ride-completion credits
-- idempotent against retries.
CREATE TABLE IF NOT EXISTS user_points (
    id                  BIGSERIAL PRIMARY KEY,
    account_id          BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action              TEXT NOT NULL
                        CONSTRAINT user_points_action_allowed
                        CHECK (action IN (
                            'profile_completion', 'waypoint', 'gbfs_trip_validated',
                            'report_wont_start', 'report_not_found',
                            'report_vehicle_issue', 'report_improper_parking',
                            'qr_scan'
                        )),
    points              INTEGER NOT NULL CHECK (points > 0),
    lat                 DOUBLE PRECISION NOT NULL,
    lng                 DOUBLE PRECISION NOT NULL,
    h3_8_index          BIGINT NOT NULL,
    vehicle_identifier  TEXT,
    status              TEXT NOT NULL DEFAULT 'confirmed'
                        CONSTRAINT user_points_status_allowed
                        CHECK (status IN ('confirmed', 'pending_review')),
    source_table        TEXT,
    source_id           TEXT
);

CREATE INDEX IF NOT EXISTS idx_user_points_account
    ON user_points (account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_points_account_action_vehicle
    ON user_points (account_id, action, vehicle_identifier);
CREATE INDEX IF NOT EXISTS idx_user_points_h3_8
    ON user_points (h3_8_index);
-- Idempotency guard for ride-completion credits (waypoint /
-- gbfs_trip_validated) — at most one row per (source, action). Does NOT
-- apply to qr_scan/profile_completion, which have no source_id and are
-- guarded by their own app-level checks instead (see src/points.py).
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_points_source_dedupe
    ON user_points (source_table, source_id, action)
    WHERE source_table IS NOT NULL AND source_id IS NOT NULL;
