-- A day whose Equity Area figure the latest reconstruction could not
-- measure defensibly, recorded as such instead of reading as "not
-- reprocessed yet" indefinitely.
--
-- THE GAP THIS CLOSES. Days that predate the official map (sql/079) carry a
-- NULL `avg_percent_all_devices_equity` until src/equity_backfill.py
-- rebuilds them from device_history, and /api/v1/compliance/calendar shows a
-- NULL as `pending`. But a rebuild can also run and FAIL to measure a day:
-- every 6-9 AM snapshot failed the reconstruction fidelity gate
-- (reconstructed fleet vs the fleet the cycle recorded, ±10%), or none had
-- history or a recorded fleet to check against. 2026-08-09 and 2026-08-10
-- are exactly that — 91/91 and 93/93 snapshots rejected at fidelity
-- 1.11–1.15. The job then writes nothing, the average stays NULL, and the
-- day reads `pending` — "not attempted yet" — when it has been attempted;
-- once outside the nightly job's 14-day lookback it is not attempted again
-- unless someone runs the backfill by hand.
--
-- WHY A STORED VERDICT, NOT A REQUEST-TIME ONE. Reconstructing a day takes a
-- device_history scan and a DuckDB spatial join; the calendar must not do
-- that per request. The job records the outcome of its latest attempt here,
-- and the calendar reads it. NULL = no verdict (measured, or not yet
-- attempted).
--
-- A CURRENT VERDICT, NOT A PERMANENT ONE. It says what the latest
-- reconstruction could defend, nothing more: the nightly sweep retries these
-- days while they are inside its lookback, a manual run can retry any day,
-- and the first attempt that produces a figure clears it (below).
--
-- WHO SETS AND CLEARS IT.
--   * set:   src/equity_backfill.py, when a window-only reprocess of a day
--            with snapshots passes none of them through the gate — and only
--            onto a row whose equity average is still NULL, so a live
--            figure is never contradicted.
--   * clear: src/daily_sla.py's upsert, whenever it produces a non-NULL
--            equity average for the day. A later run that CAN measure the
--            day (fewer ghost stops, a different gate) therefore supersedes
--            the verdict automatically.
--
-- ONLY THE OFFICIAL GROUP. v1/v2 (and er1..er6) were recorded live by the
-- pipeline and are never reconstructed, so they have no verdict column;
-- see src/equity_groups.py REPROCESSED_GROUPS.
--
-- Reason codes (a CHECK, so a typo cannot publish an undocumented value):
--   low_fidelity — at least one snapshot was reconstructed and every one
--                  that was fell outside the fidelity gate
--   no_history   — no snapshot could be reconstructed or checked at all
--                  (no device_history coverage, or no recorded fleet)
--
-- No data step here on purpose: marking 2026-08-09/10 is done by re-running
-- the backfill for those dates after deploy
-- (`python -m src.cli equity_backfill 2026-08-09 2026-08-10`), so the
-- verdict is reached by the same code that will reach it for any future
-- day rather than asserted by hand in a migration.
--
-- Replay-safe: every statement is guarded, as tests/test_migration_replay_pg.py
-- requires of the whole sql/ set.

ALTER TABLE daily_sla_compliance
    ADD COLUMN IF NOT EXISTS equity_unmeasurable_reason TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'daily_sla_equity_unmeasurable_reason_allowed'
           AND conrelid = 'daily_sla_compliance'::regclass
           AND contype = 'c'
    ) THEN
        ALTER TABLE daily_sla_compliance
            ADD CONSTRAINT daily_sla_equity_unmeasurable_reason_allowed
            CHECK (equity_unmeasurable_reason IN ('low_fidelity', 'no_history'));
    END IF;
END $$;
