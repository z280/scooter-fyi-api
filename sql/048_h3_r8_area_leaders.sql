-- H3 r8 area leader report (FEATURE_PLAN_2026-07.md §11 /
-- PLAN_RIDE_MODE_API.md Phase A4): "all r8 hexagons in the local network,
-- with the user who earned the most points there in the last four weeks,
-- recalculated."
--
-- Three tables, one nightly job (src/area_leaders.py:recompute, cron
-- `15 9 * * *`):
--
--   h3_r8_area_report       one row per UNIVERSE cell — every r8 cell with
--                           observed devices (device_history / device_state)
--                           OR points history (user_points), ALL-TIME, not
--                           windowed (~720 cells today). total_points /
--                           distinct_earners ARE windowed (trailing 28
--                           days, status='confirmed' only) — they are what
--                           "recalculated" nightly actually recalculates.
--   h3_r8_area_leaders      TOP 3 per cell, not just the winner. Privacy
--                           (show_in_leaderboards / show_public_username)
--                           is applied at READ time by the endpoint, not
--                           here, because those flags can flip at any
--                           moment and must take effect immediately rather
--                           than waiting for tomorrow's run — an opted-out
--                           rider's cell falls through to the runner-up
--                           instead of going blank until the next recompute.
--                           Tie-break (deterministic, enforced in
--                           src/area_leaders.py, not in SQL, so it is
--                           covered by ordinary fake-cursor unit tests and
--                           not only by a live Postgres window-function
--                           test): points DESC, then first_point_at ASC
--                           ("whoever got there first holds the
--                           territory"), then account_id ASC as the final
--                           total order. Only status = 'confirmed' ledger
--                           rows ever count.
--   h3_r8_area_leader_runs  APPEND-ONLY audit log, one row per recompute,
--                           stamping the window it measured. Unlike the two
--                           tables above (fully replaced every run), this
--                           one accumulates — it is the record of what each
--                           run measured, not the report itself.
--
-- REPLACEMENT, NOT ACCUMULATION. One transaction does
-- `DELETE FROM h3_r8_area_report` (cascading h3_r8_area_leaders via its FK)
-- -> INSERT the fresh universe -> INSERT the fresh leaders -> INSERT the
-- run row. Same idiom as src/daily_trips.py:compute_for_date, so a re-run
-- (backfill, or just tomorrow's cron) always reflects current data rather
-- than an accumulation on top of yesterday's.
--
-- Every table here is a fresh CREATE TABLE IF NOT EXISTS, so — unlike an
-- ALTER TABLE ... ADD COLUMN IF NOT EXISTS, which Postgres skips in its
-- entirety (constraint included) once the column exists — inline CHECKs in
-- CREATE TABLE carry no replay hazard: there is no "already exists without
-- its constraint" state for a table this migration is the sole creator of.
-- No guarded DO $$ blocks are needed for anything below, matching sql/028's
-- own precedent for a brand-new ledger-adjacent table.

CREATE TABLE IF NOT EXISTS h3_r8_area_report (
    h3_8_index        BIGINT PRIMARY KEY,
    has_devices       BOOLEAN NOT NULL,
    has_points        BOOLEAN NOT NULL,
    total_points      INTEGER NOT NULL DEFAULT 0,
    distinct_earners  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS h3_r8_area_leaders (
    h3_8_index      BIGINT NOT NULL REFERENCES h3_r8_area_report(h3_8_index) ON DELETE CASCADE,
    rank            SMALLINT NOT NULL CHECK (rank BETWEEN 1 AND 3),
    account_id      BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    points          INTEGER NOT NULL CHECK (points > 0),
    first_point_at  TIMESTAMPTZ NOT NULL,   -- tie-break provenance
    PRIMARY KEY (h3_8_index, rank)
);

CREATE TABLE IF NOT EXISTS h3_r8_area_leader_runs (
    id            BIGSERIAL PRIMARY KEY,
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    window_start  TIMESTAMPTZ NOT NULL,
    window_end    TIMESTAMPTZ NOT NULL,
    cell_count    INTEGER NOT NULL,
    led_cells     INTEGER NOT NULL
);

-- Serves the nightly job's per-cell windowed read (WHERE h3_8_index = ...
-- ORDER BY created_at) — the composite leading on h3_8_index with
-- created_at DESC.
CREATE INDEX IF NOT EXISTS idx_user_points_h3_8_created
    ON user_points (h3_8_index, created_at DESC);

-- ---------------------------------------------------------------------------
-- Reconciliation beyond §11.2 (PLAN_RIDE_MODE_API.md Phase A4 calls this out
-- explicitly as one of two narrow deviations from the FEATURE_PLAN text):
-- sql/028 already ships a plain `idx_user_points_h3_8 ON user_points
-- (h3_8_index)`. The composite index just above leads on the exact same
-- column (h3_8_index) with created_at DESC appended, so it satisfies every
-- query the plain single-column index could ever serve (any query plannable
-- against (h3_8_index) alone is plannable against (h3_8_index, created_at
-- DESC) — the leading column is identical) — the plain index is therefore
-- strictly subsumed and would otherwise sit there forever as dead weight
-- every INSERT into user_points has to maintain for nothing.
-- `DROP INDEX IF EXISTS` is idempotent/replay-safe: a first run drops the
-- sql/028 index once, and every later replay (this file re-run, or the _pg
-- test fixtures replaying the whole sql/ directory) matches zero indexes
-- and is a no-op.
DROP INDEX IF EXISTS idx_user_points_h3_8;
