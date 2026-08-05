-- One row per scheduled operation, so /admin/scheduler can say what
-- actually ran instead of only what is supposed to run.
--
-- The Scheduler page showed the crontab plus a cadence table for the ingest
-- cycle -- which was both a duplicate of /admin/cycles (its own dedicated
-- page, backed by observation_cycles) and the only job on the page. Every
-- OTHER command in that crontab -- the SLA and trip rollups, the archive,
-- four retention sweeps, two expiries, the routing/geocoding refreshes, the
-- battery extract and refit, the comms poll, the de-id sweep, the area
-- universe, the feature grader -- ran with no operator-visible record at
-- all. If one silently stopped firing, nothing said so.
--
-- src/cli.py has a single dispatch point (`COMMANDS[cmd]()`), so recording
-- every run is one wrapper rather than an edit per command. That is
-- deliberate: a job added later is recorded because it exists, not because
-- somebody remembered to instrument it.
--
-- ingest_cycle is EXCLUDED, by design and not by omission -- see
-- src/job_runs.py. It runs every 2 minutes (~720 rows/day, versus ~700/day
-- for everything else combined) and already has richer per-cycle records in
-- observation_cycles behind /admin/cycles. Recording it here would swamp
-- the table to duplicate a better page.
--
-- `summary` holds whatever the command returned -- each one already returns
-- a dict of its own counts (src/daily_trips.py's convention, followed by
-- the rest), and until now those went only to the container log. `error`
-- holds the exception string on failure; the traceback still goes to the
-- log and to Sentry, which are the right places for it.
--
-- A 'running' row is written BEFORE the command executes and updated after,
-- so a job that dies without unwinding -- OOM, container kill -- is visible
-- as a run that started and never finished, which is exactly the failure a
-- log-only record hides.

CREATE TABLE IF NOT EXISTS job_runs (
    id           BIGSERIAL PRIMARY KEY,
    command      TEXT NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    status       TEXT NOT NULL DEFAULT 'running'
                 CONSTRAINT job_runs_status_allowed
                 CHECK (status IN ('running', 'ok', 'error')),
    duration_ms  INTEGER,
    summary      JSONB,
    error        TEXT
);

-- The page's main query is "latest run per command", and the retention
-- sweep's is a time range; this serves both.
CREATE INDEX IF NOT EXISTS idx_job_runs_command_started
    ON job_runs (command, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_runs_started
    ON job_runs (started_at DESC);
