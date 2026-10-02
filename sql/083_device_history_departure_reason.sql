-- Why a stop ended: 'moved' or 'absent' (src/device_state.py).
--
-- THE BUG THIS SUPPORTS ------------------------------------------------------
-- device_history.departed_at used to be stamped only when a vehicle was seen
-- somewhere else (MOVED) or was unlocked (a rental's first reserved cycle).
-- A vehicle that simply left the GBFS feed (pulled for repair, retired,
-- re-keyed) kept an open stop forever: a "ghost stop". Veo keeps RENTED
-- vehicles in the feed (reserved), so absence from the feed genuinely means
-- the vehicle is not there. Measured 2026-09-29: permanent ghosts had grown
-- from 1.8% of the reconstructed fleet in July to 6.2%, and every snapshot
-- since 2026-09-12 failed the ±10% fidelity gate in src/equity_backfill.py.
--
-- device_state now closes the open stop of a vehicle that has been out of the
-- feed longer than ABSENT_STOP_AFTER, stamping departed_at with the vehicle's
-- LAST OBSERVED time (the last moment it was known to be there), and records
-- that here as 'absent'. Every other close is now written as 'moved': a MOVED
-- transition, or a rental's start (the rider took it from this spot).
--
-- NULL means the row was closed before this migration (all such closes were
-- moves or rental starts) or is still open. Historical rows are deliberately
-- NOT backfilled to 'moved': that is an UPDATE of ~13M rows (3 GB heap) on a
-- table the ingest cycle writes every two minutes, for a value NULL already
-- implies. `python -m src.cli close_ghost_stops` closes the existing ghosts
-- (as 'absent') by the same rule, once, by hand.
--
-- LIVE-TABLE SAFETY ----------------------------------------------------------
-- * ADD COLUMN with no DEFAULT is a catalog-only change in Postgres 11+: no
--   table rewrite, no scan.
-- * The CHECK is added NOT VALID, so Postgres does not scan 13M rows to prove
--   what is trivially true (every existing value is NULL). It is enforced on
--   every INSERT and UPDATE from now on, which is all it is for.
-- * Both still need a brief ACCESS EXCLUSIVE lock. lock_timeout bounds how
--   long this may queue behind a long reader (the weekly area-universe scan,
--   an equity backfill) while itself blocking the ingest cycle's writes. If
--   it times out, boot fails loudly and the container's restart retries,
--   which is better than stalling ingest. RESET restores the session default
--   for the migrations that follow in the same batch (src/pg.py applies every
--   pending file in ONE transaction).
-- * Idempotent: the pg test fixtures replay every sql/*.sql on every run, and
--   ADD CONSTRAINT has no IF NOT EXISTS, hence the catalog check.

SET lock_timeout = '10s';

ALTER TABLE device_history
    ADD COLUMN IF NOT EXISTS departure_reason TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'device_history_departure_reason_allowed'
          AND conrelid = 'device_history'::regclass
    ) THEN
        ALTER TABLE device_history
            ADD CONSTRAINT device_history_departure_reason_allowed
            CHECK (departure_reason IN ('moved', 'absent')) NOT VALID;
    END IF;
END
$$;

COMMENT ON COLUMN device_history.departure_reason IS
    'Why this stop ended. moved = the vehicle was seen elsewhere, or a rental '
    'started here; absent = it left the GBFS feed for longer than '
    'device_state.ABSENT_STOP_AFTER and departed_at is its last observed time. '
    'NULL = still open, or closed before sql/083 (always a move or rental start).';

RESET lock_timeout;
