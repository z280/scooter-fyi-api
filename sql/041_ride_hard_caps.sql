-- Record the operator's hard ride caps in the schema.
--
-- Three invariants, set by the operator, transcribed in
-- src/ride_limits.py and enforced in the API. This file makes the one of
-- them that is a property of a STORED ROW — "no ride is more than 80 km" —
-- true in the database as well, so a future writer that bypasses the API
-- cannot quietly reintroduce what the API now refuses.
--
--   A. <=100 points per ride    application-only (src/points.py). Not
--      expressible here: the cap is a SUM across user_points rows for one
--      ride, which a CHECK cannot see. A trigger could, but it would be a
--      second enforcement point that can disagree with the first, and the
--      first is a single function every award already funnels through.
--   B. <=3 km between consecutive path points    application-only. The
--      points are separate rows in *_waypoints; a row-local CHECK cannot
--      see its neighbour, and the ordering that defines "consecutive" is
--      itself a query.
--   C. <=80 km per ride    THIS FILE.
--
-- ---------------------------------------------------------------------------
-- !! THIS MIGRATION RUNS AT BOOT AGAINST THE POPULATED PRODUCTION DATABASE !!
-- ---------------------------------------------------------------------------
-- src/pg.py replays every file in sql/ on every boot, and the _pg test
-- fixtures execute the whole directory on every run. A migration that can
-- fail on existing data does not fail a migration — it fails the API's
-- startup. Everything below is therefore either idempotent or guarded, and
-- the ordering is load-bearing: rows are brought inside the cap BEFORE the
-- constraint that requires it is added, so ADD CONSTRAINT can never be the
-- statement that takes the service down.
--
-- WHAT HAPPENS TO HISTORY THAT ALREADY BREAKS THE CAP: it is CLAMPED, not
-- left alone and not deleted. Rows above 80 km are set to exactly 80 km and
-- what they previously read is preserved in the new
-- distance_clamped_from_m column. Three reasons this beats the alternatives:
--
--   * Leaving history alone would mean the invariant is simply false for
--     the rows that matter most — the over-long ones are exactly the rows
--     src/badges.py is summing. An invariant with an exemption for existing
--     violations is not an invariant, it is a preference about new writes.
--   * Adding the CHECK as NOT VALID (the usual way to skip a scan) would
--     leave those rows above the cap AND make any later UPDATE of one fail
--     at runtime: a NOT VALID CHECK is still enforced on UPDATE. The 24-hour
--     expiry sweep (sql/040) and a rider's own PATCH .../end both update
--     ride rows, so the cost would be an unrelated code path throwing
--     CheckViolation in production, long after anybody connects it to this.
--   * Nothing is lost. Clamping here is the same bargain the API makes at
--     PATCH .../end: record the number we are willing to stand behind, and
--     keep the number we actually measured next to it. A row that reads
--     "80 000, clamped from 412 883" is strictly more informative than one
--     that reads "412 883" and is believed.
--
-- No ride is DELETED and no waypoint is touched. distance_source is left
-- exactly as it was: it describes how the distance was measured, and
-- clamping doesn't change that — distance_clamped_from_m is what says the
-- recorded figure is a ceiling.
--
-- The points cap (A) is FORWARD-ONLY and this file does not touch
-- user_points. The ledger is append-only and is the record of what riders
-- were actually granted; clawing back points people earned under the old
-- rules would be a worse breach of that than the overpayment was.

-- ---------------------------------------------------------------------------
-- 1. The column that lets a clamped row stay honest.
-- ---------------------------------------------------------------------------
-- NULL means "not clamped, distance is what we measured", which is the
-- overwhelming majority of rows and the default for every new one. A
-- non-NULL value is the pre-clamp measurement, and implies distance is
-- sitting at the cap.
ALTER TABLE rides
    ADD COLUMN IF NOT EXISTS distance_clamped_from_m DOUBLE PRECISION
        CHECK (distance_clamped_from_m IS NULL OR distance_clamped_from_m > 0);

ALTER TABLE tracked_rides
    ADD COLUMN IF NOT EXISTS distance_clamped_from_m DOUBLE PRECISION
        CHECK (distance_clamped_from_m IS NULL OR distance_clamped_from_m > 0);

-- ---------------------------------------------------------------------------
-- 2. Bring existing rows inside the cap. MUST precede step 4.
-- ---------------------------------------------------------------------------
-- Idempotent by construction: after this runs there is nothing left above
-- the cap, so a replay matches zero rows. COALESCE protects the original
-- measurement if a row somehow gets here twice — the first clamp's value
-- wins, so replaying can never record "clamped from 80 000".
UPDATE rides
   SET distance_clamped_from_m = COALESCE(distance_clamped_from_m, distance_m),
       distance_m = 80000
 WHERE distance_m IS NOT NULL
   AND distance_m > 80000;

UPDATE tracked_rides
   SET distance_clamped_from_m = COALESCE(distance_clamped_from_m, distance_meters),
       distance_meters = 80000
 WHERE distance_meters IS NOT NULL
   AND distance_meters > 80000;

-- ---------------------------------------------------------------------------
-- 3. Widen distance_source for the partial measurement.
-- ---------------------------------------------------------------------------
-- A path with a leg too long to believe is measured with that leg left out,
-- which is a different claim from a path measured whole — 'waypoints' says
-- "we measured your track", 'waypoints_partial' says "we measured your
-- track except for a piece we didn't believe". Flattening the two would let
-- a consumer treat a ride with a hole in it as equal evidence.
--
-- REPLAY SAFETY: same guarded shape sql/040 uses (and that sql/029 had to be
-- repaired into) — read the live constraint definition first and rewrite
-- only when the new value isn't already permitted, so replaying after a
-- LATER migration widens the list again is a no-op rather than a regression
-- that rejects rows that migration stored.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'rides_distance_source_allowed'
       AND conrelid = 'rides'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('waypoints_partial' in current_def) = 0 THEN
        -- The auto-named inline CHECK from sql/035's ADD COLUMN, and the
        -- named one this file installs, so a partial earlier run leaves
        -- nothing behind.
        ALTER TABLE rides DROP CONSTRAINT IF EXISTS rides_distance_source_check;
        ALTER TABLE rides DROP CONSTRAINT IF EXISTS rides_distance_source_allowed;
        ALTER TABLE rides
            ADD CONSTRAINT rides_distance_source_allowed
            CHECK (distance_source IN ('client', 'waypoints', 'straight_line',
                                       'waypoints_partial'));
    END IF;
END $$;

DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'tracked_rides_distance_source_allowed'
       AND conrelid = 'tracked_rides'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('waypoints_partial' in current_def) = 0 THEN
        -- sql/034 installed its list as an inline CHECK on ADD COLUMN.
        ALTER TABLE tracked_rides DROP CONSTRAINT IF EXISTS tracked_rides_distance_source_check;
        ALTER TABLE tracked_rides DROP CONSTRAINT IF EXISTS tracked_rides_distance_source_allowed;
        ALTER TABLE tracked_rides
            ADD CONSTRAINT tracked_rides_distance_source_allowed
            CHECK (distance_source IN ('waypoints', 'straight_line',
                                       'waypoints_partial'));
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4. The cap itself. Runs only after step 2 guaranteed it can be satisfied.
-- ---------------------------------------------------------------------------
-- Added VALIDATED, not NOT VALID — see the header. The full-table scan this
-- costs is bounded by the size of two rider-owned tables and happens once.
--
-- NULL passes: an active ride has no distance yet (sql/035 dropped the NOT
-- NULL for exactly that), and a tracked ride that never ended never gets
-- one. The cap is a statement about recorded distances, not a requirement
-- that one exist.
--
-- Guarded on conname ALONE — deliberately unlike step 3, which also
-- inspects the value. The two guards protect against opposite things and
-- must not be made to match:
--
--   Step 3 widens an enumerated list. A replay has to be able to tell "the
--   value I need is missing" from "already fine", so it reads the
--   definition for 'waypoints_partial'.
--   Step 4 installs a numeric BOUND. If a later migration raises the cap to
--   100000, a value check here would see "80000 is absent", fire, and
--   revert the raise — the exact regression step 3's value check exists to
--   prevent. Leaving any existing constraint of this name untouched is what
--   makes a later change stick.
--
-- Net effect either way: replay is a no-op, and a later migration owns the
-- cap once it changes it. (An earlier revision of this comment claimed the
-- value was checked here. It never was, and it must not be.)
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'rides_distance_within_cap'
       AND conrelid = 'rides'::regclass
       AND contype = 'c';

    IF current_def IS NULL THEN
        ALTER TABLE rides
            ADD CONSTRAINT rides_distance_within_cap
            CHECK (distance_m IS NULL OR distance_m <= 80000);
    END IF;
END $$;

DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'tracked_rides_distance_within_cap'
       AND conrelid = 'tracked_rides'::regclass
       AND contype = 'c';

    IF current_def IS NULL THEN
        ALTER TABLE tracked_rides
            ADD CONSTRAINT tracked_rides_distance_within_cap
            CHECK (distance_meters IS NULL OR distance_meters <= 80000);
    END IF;
END $$;

-- NOTE on the literal 80000 appearing here and as MAX_RIDE_DISTANCE_METERS
-- in src/ride_limits.py. A CHECK cannot read a Python constant, so this is
-- the one unavoidable second copy. It is pinned by
-- tests/test_ride_hard_caps_pg.py, which asserts the live constraint
-- definition matches the Python value — so the two cannot drift without a
-- test failing.
