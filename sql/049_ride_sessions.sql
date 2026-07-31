-- Ride sessions: the per-ride signing material, the rider's ride-mode
-- option blob, the feed-anchored start, and the validation state that
-- drives the post-ride contribution screen.
--
-- WHAT A RIDE SESSION IS. Ride mode records its GPS track LOCALLY, in the
-- browser, and transmits nothing mid-ride (RIDE_MODE_OVERHAUL_PLAN.md
-- Part 2). For that local record to be worth anything later, the batches
-- have to be signed with something the server issued and can re-derive at
-- donation time. That is track_key + track_nonce: minted once, at ride
-- start, over the already-authenticated POST /api/v1/tracked-rides
-- channel, and handed back only to the ride's OWNER.
--
--   track_key            base64url of 32 random bytes — the HMAC-SHA256 key
--                        for this ride's JWS batches, `kid` = the ride id.
--                        A SECRET, in the sense that anyone holding it can
--                        mint batches this ride will accept: it is returned
--                        by the start call and by the two owner-only single
--                        -ride reads (so a reloaded client can resume
--                        signing), and NEVER in a list response.
--   track_nonce          16 random bytes, hex. Seeds the rolling chain hash
--                        (H_-1 = sha256(nonce)) and is echoed in every
--                        batch payload, so a chain built for another ride
--                        cannot be replayed into this one.
--   track_key_issued_at  when the pair was minted. Since it is stamped
--                        server-side at start, a donated chain cannot
--                        claim to predate the ride it belongs to.
--
-- Per-ride keys, not per-account: compromise is bounded to one ride, and
-- there is no key rotation problem because a key is never reused.
--
-- WHY TWO START BATTERIES. reported_start_battery_percent is what the
-- rider typed off the vehicle's own display; feed_start_battery_percent is
-- what the GBFS feed implied at the same moment
-- (quality.compute_battery_percent over the newest raw_telemetry_points
-- row for the vehicle — the exact derivation src/ride_watch.py uses for
-- gbfs_end_battery_percent). Both are kept because they are independent
-- observations of the same quantity and the battery model prefers the
-- feed-derived one while still needing a fallback. NUMERIC(4,1) on the
-- reported side mirrors nothing in the feed: it is a rider-typed number
-- that may carry a decimal, which is also why it needs the range CHECK
-- below (NUMERIC(4,1) by itself would happily store 999.9).
-- feed_start_battery_percent needs no such CHECK: it is INTEGER and its
-- only writer, compute_battery_percent, clamps to 0..100 by construction.
--
-- WHY A FEED-ANCHORED START POSITION. start_lat/start_lon are supplied by
-- the client, so correlating a donated track's first waypoint against them
-- is a client-versus-client comparison and proves nothing. feed_start_lat/
-- feed_start_lon are the vehicle's last position AS THE FEED SAW IT,
-- which the rider cannot supply or influence — that is what makes them the
-- anti-fabrication anchor for the donation-time start correlation
-- (PLAN_RIDE_MODE_API.md §A2 check 5, which prefers them and falls back to
-- start_lat/lon only when the feed had no fresh observation, so rides that
-- predate this migration stay verifiable). Read from the same newest
-- telemetry row as feed_start_battery_percent, in the same query.
--
-- ride_options is CLIENT-OWNED: stored and echoed back verbatim, and the
-- server reads only the booleans it gates on (save_tracks gates donation,
-- battery_modeling / nav_improvement / end_survey gate their awards). Its
-- 4 KB size cap lives in src/api_tracked_rides.py, not here, for the
-- reason sql/043's header gives: a product limit that will move should not
-- turn into a migration against every stored row.
--
-- validation_status is the ride's contribution eligibility, and it is
-- deliberately a small state machine rather than a boolean:
--   pending       nothing decided yet (the DEFAULT, and what a ride carries
--                 for its whole active life)
--   pending_feed  waiting on the GBFS side to resolve — the live feed has
--                 not yet told us where the vehicle reappeared, so the
--                 start/end correlation is undecidable
--   eligible      verified; points may be awarded
--   ineligible    decided against, with validation_reasons saying why from
--                 the fixed vocabulary (start_mismatch, end_mismatch,
--                 tracking_not_opted, too_few_waypoints, trip_too_short,
--                 chain_invalid, internal_error)
--   error         verification itself failed to run
-- PATCH .../end computes a PROVISIONAL status; A2's donation handler and
-- validation finisher own the authoritative one. validated_at is stamped
-- only when the status is settled, so "decided" is distinguishable from
-- "defaulted".
--
-- ---------------------------------------------------------------------------
-- MIGRATION SHAPE
-- ---------------------------------------------------------------------------
-- src/pg.py replays every file in sql/ on a fresh boot and the _pg test
-- fixtures execute the whole directory on every run, so everything here is
-- idempotent or guarded (sql/041's header). Columns are added BARE — no
-- inline CHECK inside ADD COLUMN IF NOT EXISTS, which would be silently
-- skipped on any database where the column already exists — and the two
-- CHECKs follow as explicitly named constraints behind guards.
--
-- The NOT NULL DEFAULT columns are safe on a populated table: Postgres 11+
-- stores the default in the catalog instead of rewriting the table, and
-- every existing row therefore reads '{}' / 'pending' / '[]' — all three of
-- which satisfy the constraints below, so there is nothing to backfill
-- before adding them (sql/041's ordering rule, satisfied trivially).

ALTER TABLE tracked_rides
    ADD COLUMN IF NOT EXISTS track_nonce                    TEXT,        -- 16-byte hex, server random
    ADD COLUMN IF NOT EXISTS track_key                      TEXT,        -- base64url 32-byte HMAC key
    ADD COLUMN IF NOT EXISTS track_key_issued_at            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reported_start_battery_percent NUMERIC(4,1),
    ADD COLUMN IF NOT EXISTS feed_start_battery_percent     INTEGER,     -- derived from the feed at start
    ADD COLUMN IF NOT EXISTS feed_start_lat                 DOUBLE PRECISION,  -- vehicle's last feed position at start —
    ADD COLUMN IF NOT EXISTS feed_start_lon                 DOUBLE PRECISION,  --   the feed-anchored start for A2's check 5
    ADD COLUMN IF NOT EXISTS ride_options                   JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS validation_status              TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS validation_reasons             JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS validated_at                   TIMESTAMPTZ;

-- ---------------------------------------------------------------------------
-- reported_start_battery_percent: numeric bound, conname-only guard.
-- ---------------------------------------------------------------------------
-- sql/041 step-4 shape. Guarded on conname ALONE, deliberately unlike the
-- block below: if a later migration moves this bound, a value check here
-- would see "100 is absent", fire, and revert the move. Leaving any
-- existing constraint of this name untouched is what makes a later change
-- stick. NULL passes — a rider need not report a start battery.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'tracked_rides_reported_start_battery_range'
       AND conrelid = 'tracked_rides'::regclass
       AND contype = 'c';

    IF current_def IS NULL THEN
        ALTER TABLE tracked_rides
            ADD CONSTRAINT tracked_rides_reported_start_battery_range
            CHECK (reported_start_battery_percent IS NULL
                   OR (reported_start_battery_percent BETWEEN 0 AND 100));
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- validation_status: enumerated list, value-checked guard.
-- ---------------------------------------------------------------------------
-- sql/040 / sql/042 shape. The guard reads the live definition so a replay
-- can tell "a value I need is missing" from "already fine" — which is what
-- makes replaying this file AFTER a later migration widens the list a
-- no-op instead of a regression that rejects rows that migration stored.
--
-- SUBSTRING TRAP, why the probed values are the ones they are: these five
-- names overlap. 'pending' is a substring of 'pending_feed' and 'eligible'
-- is a substring of 'ineligible', so a position() probe for either short
-- name is satisfied by the long one alone and proves nothing on its own.
-- The probes below are therefore the two UNAMBIGUOUS long names plus
-- 'error'; any definition containing 'pending_feed' and 'ineligible' is
-- one this file wrote or a later widening of it, and either way already
-- admits 'pending' and 'eligible'. ('eligible' is probed too — harmless,
-- and it documents the intended list — but 'pending_feed'/'ineligible' are
-- what actually carry the guard.)
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'tracked_rides_validation_status_allowed'
       AND conrelid = 'tracked_rides'::regclass
       AND contype = 'c';

    IF current_def IS NULL
       OR position('pending_feed' in current_def) = 0
       OR position('eligible'     in current_def) = 0
       OR position('ineligible'   in current_def) = 0
       OR position('error'        in current_def) = 0
    THEN
        -- The named constraint this file installs, so a partial earlier run
        -- leaves nothing behind. (There is no auto-named twin to drop: the
        -- ADD COLUMN above carries no inline CHECK, precisely because such
        -- a CHECK would be skipped whenever the column already exists.)
        ALTER TABLE tracked_rides DROP CONSTRAINT IF EXISTS tracked_rides_validation_status_allowed;
        ALTER TABLE tracked_rides
            ADD CONSTRAINT tracked_rides_validation_status_allowed
            CHECK (validation_status IN
                   ('pending', 'pending_feed', 'eligible', 'ineligible', 'error'));
    END IF;
END $$;
