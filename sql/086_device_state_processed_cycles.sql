-- The cycles src/device_state.py actually processed (the ABSENCE ledger).
--
-- WHY ------------------------------------------------------------------------
-- sql/083's absence rule closes a vehicle's open stop once it has been out of
-- the feed for over an hour AND missed ABSENT_MIN_MISSED_CYCLES consecutive
-- observed cycles. Until now "observed cycle" meant a snapshot_metadata_core
-- row. Those rows prove a snapshot was WRITTEN, not that device_state saw it:
-- src/cycle.py commits the core snapshot first and then calls
-- device_state.update_for_cycle inside a try/except that logs and swallows.
-- With ingest healthy and the updater alone failing for more than an hour,
-- vehicles that were in every one of those feeds looked absent, and their
-- stops were closed and backdated to the start of the failure (PR #98 review,
-- finding 1).
--
-- device_state now writes one row here per cycle it processes, INSIDE its own
-- transaction, so a row exists exactly when that cycle's observations (every
-- last_observed_at it refreshed) were committed. The absence rule counts these
-- rows, and only those with counts_as_observation, instead of
-- snapshot_metadata_core.
--
-- counts_as_observation is false for a cycle whose eligible payload (devices
-- with a vehicle_identifier) was empty, or below half the median of the
-- previous ~24 h of processed cycles: an upstream or ingest glitch, not
-- evidence that the fleet left (PR #98 review, finding 2). Such a cycle is
-- still processed (its devices are observed, its sweep still runs for
-- vehicles that already qualified from real cycles) but it can never be one
-- of the missed cycles that makes a stop close. The rule and the production
-- data behind it are in src/device_state.py (ABSENT_FLOOR_RATIO).
--
-- No backfill from snapshot_metadata_core: those rows are exactly the
-- unverified evidence this replaces. Until ABSENT_MIN_MISSED_CYCLES rows exist
-- (about 10 minutes after deploy) nothing is judged absent, and until
-- min + ABSENT_SWEEP_WINDOW_CYCLES rows exist the per-cycle sweep runs
-- unbounded, as it did when sql/083 was new.
--
-- Retention: device_state trims rows older than ABSENT_LEDGER_RETENTION each
-- cycle, but always keeps the newest ABSENT_LEDGER_KEEP_MIN rows, so an
-- updater outage of any length cannot empty it. Steady state is a few
-- thousand narrow rows.
--
-- LIVE-DB SAFETY -------------------------------------------------------------
-- Creates a new table and its indexes only. Nothing existing is altered,
-- rewritten or scanned, and no lock is taken on any table the ingest cycle
-- uses (deliberately no foreign key to observation_cycles, which would take a
-- SHARE ROW EXCLUSIVE lock on it). lock_timeout is set anyway, as in sql/083,
-- so that if this ever queues on a catalog lock it fails boot loudly rather
-- than stalling ingest; RESET restores the default for later files in the same
-- batch. Replay-safe: every statement is IF NOT EXISTS (the pg fixtures replay
-- every sql/*.sql on every run).

SET lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS device_state_processed_cycles (
    cycle_id              UUID PRIMARY KEY,
    snapshot_time         TIMESTAMPTZ NOT NULL,
    eligible_count        INTEGER     NOT NULL CHECK (eligible_count >= 0),
    baseline_count        NUMERIC,
    counts_as_observation BOOLEAN     NOT NULL,
    processed_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The baseline read (newest N processed cycles) and the retention trim.
CREATE INDEX IF NOT EXISTS idx_dspc_snapshot_time
    ON device_state_processed_cycles (snapshot_time DESC);

-- absence_window's read: newest k + m cycles that count as observations.
CREATE INDEX IF NOT EXISTS idx_dspc_observation_time
    ON device_state_processed_cycles (snapshot_time DESC)
    WHERE counts_as_observation;

COMMENT ON TABLE device_state_processed_cycles IS
    'One row per cycle src/device_state.py committed, written in its own '
    'transaction. The absence rule (sql/083) counts rows with '
    'counts_as_observation as the observed cycles a vehicle must miss. See '
    'sql/086.';
COMMENT ON COLUMN device_state_processed_cycles.eligible_count IS
    'Devices in the cycle with a vehicle_identifier (what device_state tracks).';
COMMENT ON COLUMN device_state_processed_cycles.baseline_count IS
    'Median eligible_count of the previous ABSENT_BASELINE_CYCLES processed '
    'cycles, at the time of this one; NULL while fewer than '
    'ABSENT_BASELINE_MIN_CYCLES existed.';
COMMENT ON COLUMN device_state_processed_cycles.counts_as_observation IS
    'False when eligible_count was 0 or below ABSENT_FLOOR_RATIO x baseline: '
    'such a cycle can never be one of the missed cycles that closes a stop.';

RESET lock_timeout;
