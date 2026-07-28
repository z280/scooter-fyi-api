-- Give an active off-feed ride a 24-hour lifetime.
--
-- THE BUG THIS CLOSES. sql/035 made `rides` a lifecycle table and enforced
-- "one active ride per account" with idx_rides_one_active_per_account, a
-- partial unique index on WHERE status = 'active'. It did not give an
-- active ride any way to stop being active except the rider reporting an
-- end. A rider who starts a ride and never ends it — phone dies, app is
-- uninstalled, they simply forget — is 409'd out of POST /api/v1/rides/start
-- FOREVER. The only escape was DELETE, which destroys the ride and its whole
-- waypoint track. That is a permanent lockout fixed by data loss.
--
-- tracked_rides (sql/027) already solved this: it carries watch_expires_at
-- and an 'expired' status, and `python -m src.cli expire_stale_watches`
-- sweeps the rows whose window elapsed. This migration gives `rides` the
-- same terminal state, swept by `python -m src.cli expire_stale_off_feed_rides`.
--
-- WHY NO watch_expires_at COLUMN HERE. tracked_rides needs one because its
-- window is a GBFS *watch* the ingest cycle reads on every pass, and
-- src/ride_watch.py compares against it per device. Nothing polls an
-- off-feed ride — there is no vehicle to observe — so the deadline is a
-- constant, not per-row state, and a stored column would only be a second
-- copy of it that could disagree with the sweep. The sweep measures from
-- created_at (see the CLI docstring for why created_at and not started_at:
-- started_at is client-supplied and therefore spoofable in both directions,
-- created_at is DEFAULT NOW() and is not).
--
-- WHAT AN EXPIRED RIDE MEANS — identical to an expired tracked ride:
--   * ended_at, duration_s, end_lat, end_lon stay NULL. We never observed
--     an end and will not invent one. An expired ride is an INCOMPLETE
--     record, not a completed ride with a guessed end.
--   * distance_m / distance_source keep whatever the uploaded waypoints
--     already measured (start -> last fix). That number is real; it is
--     simply missing its last leg, exactly as it was while the ride ran.
--   * It earns NO badge mileage and feeds NO streak. src/badges.py unions
--     `WHERE ended_at IS NOT NULL AND status = 'completed'` against
--     tracked_rides' `WHERE user_reported_ended_at IS NOT NULL`, so both
--     tables drop their expired rows for the same reason: a ride nobody
--     ever ended is not evidence of a distance ridden.
--   * The row and its waypoints are KEPT and still export. Expiry frees the
--     active slot; it never deletes rider data. Only the rider deletes.

-- ---------------------------------------------------------------------------
-- Widen the status CHECK.
-- ---------------------------------------------------------------------------
-- REPLAY SAFETY (src/pg.py: every file is re-run on every boot, and the _pg
-- test fixtures execute the whole sql/ directory on every run). Follow the
-- shape sql/029 had to be repaired into: read the constraint definition
-- first and only rewrite it when it does not already permit the new value,
-- so replaying this file after a LATER migration widens the list again is a
-- no-op rather than a regression that rejects rows that migration stored.
--
-- sql/035 installed its value list as an inline CHECK on ADD COLUMN, which
-- Postgres auto-names rides_status_check. Replaying sql/035 cannot undo this
-- file: `ADD COLUMN IF NOT EXISTS` skips the entire subcommand — constraint
-- included — once the column exists. Any future migration touching this
-- constraint must keep the guarded shape below.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'rides_status_allowed'
       AND conrelid = 'rides'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('expired' in current_def) = 0 THEN
        -- The auto-named inline CHECK from sql/035, and the named one this
        -- file installs, so a partial earlier run leaves nothing behind.
        ALTER TABLE rides DROP CONSTRAINT IF EXISTS rides_status_check;
        ALTER TABLE rides DROP CONSTRAINT IF EXISTS rides_status_allowed;
        ALTER TABLE rides
            ADD CONSTRAINT rides_status_allowed
            CHECK (status IN ('active', 'completed', 'expired'));
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- The sweep's index.
-- ---------------------------------------------------------------------------
-- `WHERE status = 'active' AND created_at < <cutoff>`. Partial on the same
-- predicate as idx_rides_one_active_per_account, so it stays at roughly one
-- row per rider currently mid-ride no matter how large the history grows —
-- the same reason idx_watch_list_open_expiry (sql/027) is partial.
CREATE INDEX IF NOT EXISTS idx_rides_active_created
    ON rides (created_at ASC) WHERE status = 'active';

-- NOTE on rides_completed_is_complete (sql/035): it is written as
-- `status <> 'completed' OR (... IS NOT NULL ...)`, so 'expired' satisfies it
-- with every one of those columns still NULL. That is deliberate and is
-- exactly why the constraint was scoped to 'completed' rather than to
-- "not active" — nothing here needs to change.
