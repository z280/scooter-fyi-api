-- Ride Mode routes + end-of-ride surveys (PLAN_RIDE_MODE_API.md phase A3 /
-- master RIDE_MODE_OVERHAUL_PLAN.md Part 2, contract table §1.5).
--
-- TWO NEW TABLES:
--
--   ride_routes   Screen 4's chosen route, stored so Screen 9's survey can
--                 rate the leg it names and A2's nav_distance_bonus can
--                 confirm a route exists for the ride. Has GEOMETRY
--                 (route_polyline, origin/dest points), so it is subject to
--                 the master's "everything with fine geometry loses account
--                 linkage within <=28h" rule (Risk 3) — see the de-id arm
--                 note below.
--   ride_surveys  Screen 9's end-of-ride feedback. Free text and small
--                 structured answers, no geometry — kept under normal
--                 hard-delete rules (cascades with the account/ride), never
--                 de-identified.
--
-- A3 IS INDEPENDENT OF A2 (both depend only on A1) AND MAY LAND FIRST. Two
-- consequences, both handled entirely in this file plus (for the de-id arm)
-- already-shipped code in src/cli.py that guards on this table's existence:
--
--   1. ride_routes needs its OWN 28h de-id sweep, because A2's sweep lives
--      entirely inside track_donations/donated_track_points rows that only
--      exist for a donated ride — a nav-improvement ride whose track is
--      never donated would otherwise keep route geometry account-linked
--      forever. src/cli.py:deidentify_donations already carries this arm,
--      guarded on `to_regclass('ride_routes') IS NOT NULL` (a safe
--      existence probe against a database that hasn't applied this file
--      yet) — nothing in src/cli.py changes for this migration to land;
--      the guard simply starts resolving true.
--   2. user_points.action needs 'ride_survey', 'nav_route_feedback' and
--      'nav_qualitative_feedback' whether or not sql/053 (A2's points
--      migration, which widens the SAME constraint for its own two new
--      actions) has run yet — see part 2 below.
--
-- REPLAY SAFETY: src/pg.py replays every file in sql/ on every boot, and
-- the _pg test fixtures execute the whole directory on every run, so every
-- statement here is idempotent (CREATE TABLE/INDEX IF NOT EXISTS, a
-- value-checked guarded DO block for the action-vocabulary widening) per
-- sql/041's header rule.

-- ---------------------------------------------------------------------------
-- 1. ride_routes
-- ---------------------------------------------------------------------------
-- No uniqueness on tracked_ride_id: intentionally multi-row-per-ride. The
-- S8 New-Destination loop re-runs Screen 4 mid-ride, and each deliberate
-- selection is its own row (automatic off-route re-routes never POST — a
-- frontend rule, not enforced here). tracked_ride_id/account_id are both
-- nulled by the de-id sweep, same as track_donations (sql/051).
CREATE TABLE IF NOT EXISTS ride_routes (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tracked_ride_id           UUID   REFERENCES tracked_rides(id) ON DELETE CASCADE, -- nulled by de-id
    account_id                BIGINT REFERENCES accounts(id)      ON DELETE CASCADE, -- nulled by de-id
    profile                   TEXT NOT NULL,       -- a config.json valhalla.profiles key (safe|range|shade|express today)
    origin_lat  DOUBLE PRECISION NOT NULL, origin_lon DOUBLE PRECISION NOT NULL,
    dest_lat    DOUBLE PRECISION NOT NULL, dest_lon  DOUBLE PRECISION NOT NULL,
    route_polyline            TEXT NOT NULL,       -- precision-5, src/polyline.py convention
    distance_meters           DOUBLE PRECISION,
    duration_seconds          DOUBLE PRECISION,
    battery_percent_estimate  DOUBLE PRECISION,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deidentified_at           TIMESTAMPTZ
);
-- sql/051's index pair, mirrored: the ride lookup (A2's nav_distance_bonus,
-- the survey's already-linked check, tracked_rides' delete cascade) and the
-- sweep's predicate (idx_track_donations_deid's twin — the hourly 28h arm).
CREATE INDEX IF NOT EXISTS idx_ride_routes_ride
    ON ride_routes (tracked_ride_id) WHERE tracked_ride_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ride_routes_deid
    ON ride_routes (created_at) WHERE deidentified_at IS NULL;

-- ---------------------------------------------------------------------------
-- 2. ride_surveys
-- ---------------------------------------------------------------------------
-- One survey per ride (UNIQUE tracked_ride_id) — single-shot, per the
-- POST .../survey endpoint's 409-on-second-submit contract. No geometry, so
-- this table is NOT touched by the de-id sweep; account linkage follows the
-- ordinary hard-delete/cascade rule every other rider-owned table follows.
CREATE TABLE IF NOT EXISTS ride_surveys (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tracked_ride_id  UUID NOT NULL UNIQUE REFERENCES tracked_rides(id) ON DELETE CASCADE,
    account_id       BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    vehicle_model    TEXT,               -- stamped server-side from device_state.current_vehicle_model_name
    would_ride_again BOOLEAN,
    was_perfect      BOOLEAN,
    issues           JSONB NOT NULL DEFAULT '[]',   -- validated against the 16-item vocabulary
    model_bonus      JSONB NOT NULL DEFAULT '{}',
    nav_route_rating INTEGER CHECK (nav_route_rating BETWEEN 1 AND 10),
    nav_deviated     BOOLEAN,
    nav_deviated_needs_improvement BOOLEAN,
    nav_nps          INTEGER CHECK (nav_nps BETWEEN 0 AND 10),
    nav_qualitative  TEXT,                          -- free text, <=2000 chars (api-enforced)
    ride_route_id    UUID REFERENCES ride_routes(id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 3. user_points.action: widen for THIS phase's three new award actions —
--    'ride_survey', 'nav_route_feedback', 'nav_qualitative_feedback' —
--    independent of whether sql/053 (A2's own widening, for
--    'battery_contribution' and 'nav_distance_bonus') has already run.
-- ---------------------------------------------------------------------------
-- sql/028 named this constraint at CREATE TABLE time and sql/037 already
-- rewrote it once by DROP/re-ADD of that same name — no Postgres-auto-named
-- twin to clean up here, unlike sql/040/041/042.
--
-- LANDING-ORDER INDEPENDENCE (the reason this guard exists at all): A2 ships
-- sql/053, which widens this SAME constraint for its own two new actions
-- using a guard keyed on 'battery_contribution'. A2 and A3 are independently
-- mergeable and may deploy in EITHER order, so this file cannot assume
-- sql/053 has (or hasn't) already run.
--
-- The guard below is keyed on 'ride_survey' SPECIFICALLY — never
-- 'battery_contribution', sql/053's key — so the two migrations check for
-- DIFFERENT sentinel values and each only ever finds its OWN prior work,
-- never mistaking the other's for its own:
--   * sql/052 (this file) first:  installs the full five-action target list
--     below (keyed on 'ride_survey' being present). sql/053 runs next, sees
--     'battery_contribution' still missing, and rewrites to the SAME target
--     list — a genuine no-op against what this file already installed.
--   * sql/053 first: installs its own five-action target list (its
--     docstring spells out the identical union). This file then runs, sees
--     'ride_survey' still missing, and rewrites to the same union — again a
--     genuine no-op.
-- Both files converge on an IDENTICAL target list precisely so that
-- whichever lands second is a no-op rather than a narrowing regression.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'user_points_action_allowed'
       AND conrelid = 'user_points'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('ride_survey' in current_def) = 0 THEN
        ALTER TABLE user_points DROP CONSTRAINT IF EXISTS user_points_action_allowed;
        ALTER TABLE user_points
            ADD CONSTRAINT user_points_action_allowed
            CHECK (action IN (
                -- Pre-existing values (sql/028, sql/037).
                'profile_completion', 'waypoint', 'gbfs_trip_validated',
                'report_not_rideable', 'report_not_found',
                'report_vehicle_issue', 'report_improper_parking',
                'qr_scan',
                -- Ride Mode (RIDE_MODE_OVERHAUL_PLAN.md Decision 6 / Part
                -- 1.1 goal 4) — the full five-action union both sql/052 and
                -- sql/053 install, so either landing order converges here.
                'battery_contribution', 'nav_route_feedback',
                'nav_qualitative_feedback', 'nav_distance_bonus', 'ride_survey'
            ));
    END IF;
END $$;
