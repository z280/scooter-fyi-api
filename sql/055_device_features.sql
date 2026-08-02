-- Crowdsourced device features (baskets/bells/cup holders — the owner's
-- "device_features" concept). Veo's feed tells us nothing about what is
-- bolted to a given scooter, so riders standing next to one tell us, and the
-- fleet becomes filterable on equipment for the first time.
--
-- THREE PIECES, in dependency order:
--   1. device_feature_reports — the append-only submission log. EVERY
--      submission lands here, including ones whose typed plate did not
--      match: the owner's rule is "we will accept but give no points for
--      wrong entered plate numbers", so a wrong plate is a stored row with
--      plate_valid = false, not a rejected request. Nothing outside the
--      audit trail ever reads those rows (src/device_features.py's
--      processor filters on plate_valid).
--   2. device_state feature columns — the CONSENSUS view: what we believe is
--      actually on the vehicle, plus the three-value feature_status the map
--      payload publishes.
--   3. user_points.action widening for the three new awards.
--
-- The state machine (src/device_features.py owns the implementation):
--
--   needs_features_confirmed --(first valid report)--> up_to_date
--   up_to_date --(a later report disagrees)---------> needs_review
--   needs_review --(3 valid reports, 2/3 consensus)-> up_to_date
--
-- Reports are graded by a cron job every ten minutes (crontab, on the 8s),
-- never inline on the request — a rider's POST is a write to the log and
-- nothing more, so the endpoint's latency never depends on how many other
-- people reported the same scooter.

-- ---------------------------------------------------------------------------
-- 1. The submission log.
-- ---------------------------------------------------------------------------
-- Keyed on vehicle_identifier (the sql/004 identity model) — device_id is
-- the rotating GBFS bike_id and is kept only because the owner asked for
-- reports to be logged "with device-id and submitted plate number", i.e. as
-- the audit record of what the rider's client actually saw and typed.
--
-- submitted_plate is stored VERBATIM, as typed. It is not a secret we are
-- leaking: the rider read it off the sticker on a scooter they are standing
-- next to, and device_state.vehicle_plate already stores the real one in the
-- clear. Storing what was typed (rather than only a match/no-match bit) is
-- what makes "why did this device flip to needs_review?" answerable later —
-- a rash of near-miss plates is a rider confusing two adjacent scooters, a
-- rash of empty ones is a broken client.
CREATE TABLE IF NOT EXISTS device_feature_reports (
    id                    BIGSERIAL PRIMARY KEY,
    vehicle_identifier    TEXT NOT NULL,
    -- The rotating GBFS bike_id the client had on screen. Nullable: a client
    -- that only ever held the identifier is still allowed to report.
    device_id             TEXT,
    reported_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Points are never anonymous (sql/028), but a REPORT may be: an
    -- anonymous report still carries data worth having, it just earns
    -- nothing. ON DELETE SET NULL so closing an account doesn't erase the
    -- fleet knowledge that account contributed.
    account_id            BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
    reporter_ip           TEXT,
    reporter_user_agent   TEXT,
    submitted_plate       TEXT NOT NULL,
    plate_valid           BOOLEAN NOT NULL,
    -- The four answers. All four are NOT NULL because the client requires
    -- every toggle to be pressed before it will submit — "neither pressed by
    -- default" is a UI rule about the INITIAL state, not permission to send
    -- a half-answered survey.
    has_bell              BOOLEAN NOT NULL,
    has_cup_holder        BOOLEAN NOT NULL,
    has_phone_holder      BOOLEAN NOT NULL,
    all_good_condition    BOOLEAN NOT NULL,
    -- Which of the present features are NOT in good condition — the
    -- follow-up question that only appears when all_good_condition is false.
    -- Always a subset of the features this same row reported present; the
    -- endpoint enforces that, so a row claiming a broken cup holder on a
    -- scooter with no cup holder cannot exist.
    poor_condition        TEXT[] NOT NULL DEFAULT '{}',
    -- The feature_status the vehicle carried when this report was accepted.
    -- Recorded because it is what decided the award, and the vehicle's live
    -- status will have moved on by the time anyone audits the ledger.
    status_at_report      TEXT NOT NULL,
    points_awarded        INTEGER NOT NULL DEFAULT 0 CHECK (points_awarded >= 0),
    -- NULL until the ten-minute processor has folded this row into the
    -- consensus. The partial index below is the processor's work queue.
    processed_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_device_feature_reports_vehicle
    ON device_feature_reports (vehicle_identifier, reported_at);
-- The processor's queue: only ever a handful of rows, so a partial index
-- keeps the scan proportional to the backlog rather than to history.
CREATE INDEX IF NOT EXISTS idx_device_feature_reports_unprocessed
    ON device_feature_reports (reported_at)
    WHERE processed_at IS NULL;
-- Powers the per-account award-eligibility probe on submit.
CREATE INDEX IF NOT EXISTS idx_device_feature_reports_account
    ON device_feature_reports (account_id, vehicle_identifier, reported_at DESC)
    WHERE account_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. The consensus view, on device_state.
-- ---------------------------------------------------------------------------
-- feature_status defaults to 'needs_features_confirmed' so EVERY device —
-- the ~8k already in the table and every one ingest inserts tomorrow — is
-- labelled that way from the moment this migration lands, with no backfill
-- pass: the owner's "all devices will at first be labeled 'Needs features
-- confirmed'" is the column default doing the work.
--
-- The three feature booleans stay NULL until a report makes them real.
-- NULL here means "nobody has told us", which is exactly what
-- feature_status = 'needs_features_confirmed' says in the payload; they are
-- deliberately not defaulted to false, which would claim we know a scooter
-- has no bell when in truth we have never looked.
ALTER TABLE device_state
    ADD COLUMN IF NOT EXISTS feature_status TEXT NOT NULL
        DEFAULT 'needs_features_confirmed',
    ADD COLUMN IF NOT EXISTS has_bell BOOLEAN,
    ADD COLUMN IF NOT EXISTS has_cup_holder BOOLEAN,
    ADD COLUMN IF NOT EXISTS has_phone_holder BOOLEAN,
    ADD COLUMN IF NOT EXISTS features_poor_condition TEXT[],
    -- When the CURRENT consensus was written (first authoritative report, or
    -- the 2/3 vote that resolved a review). NULL = never confirmed.
    ADD COLUMN IF NOT EXISTS features_confirmed_at TIMESTAMPTZ,
    -- When this vehicle entered 'needs_review'. The processor counts only
    -- reports at or after this instant toward the three it needs, so a
    -- device that has been through two disagreements doesn't resolve the
    -- second one using votes cast about the first.
    ADD COLUMN IF NOT EXISTS features_review_since TIMESTAMPTZ,
    -- Valid reports folded into the current consensus. Display/telemetry
    -- only; no logic reads it.
    ADD COLUMN IF NOT EXISTS features_report_count INTEGER NOT NULL DEFAULT 0;

-- Guarded on conname alone (sql/053 part-2 shape): a fixed three-value
-- vocabulary with nothing to widen later, so an existing constraint of this
-- name is left exactly as it is.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'device_state_feature_status_allowed'
       AND conrelid = 'device_state'::regclass
       AND contype = 'c';

    IF current_def IS NULL THEN
        ALTER TABLE device_state
            ADD CONSTRAINT device_state_feature_status_allowed
            CHECK (feature_status IN (
                'needs_features_confirmed', 'needs_review', 'up_to_date'
            ));
    END IF;
END $$;

-- Lets the map payload's "which devices still need features confirmed?"
-- filter (and the leaderboard-facing "what's worth 14 points right now?"
-- question) hit an index instead of the whole fleet.
CREATE INDEX IF NOT EXISTS idx_device_state_feature_status
    ON device_state (feature_status);

-- ---------------------------------------------------------------------------
-- 3. user_points.action: three more awards.
-- ---------------------------------------------------------------------------
-- Same guarded-widening shape as sql/053 part 1, keyed on the one value only
-- THIS file ever adds ('device_features_first'), so a replay after some
-- later migration widens the list further is a no-op rather than a
-- regression that reverts it. The three values map 1:1 to the owner's three
-- award tiers; the amounts live in src/points.py, never here (sql/028's
-- rule: "a value tweak is a code change, not a migration").
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'user_points_action_allowed'
       AND conrelid = 'user_points'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('device_features_first' in current_def) = 0 THEN
        ALTER TABLE user_points DROP CONSTRAINT IF EXISTS user_points_action_allowed;
        ALTER TABLE user_points
            ADD CONSTRAINT user_points_action_allowed
            CHECK (action IN (
                -- Everything sql/028 + sql/037 + sql/052 + sql/053 allow.
                -- Repeated in full because a CHECK is replaced wholesale,
                -- not appended to.
                'profile_completion', 'waypoint', 'gbfs_trip_validated',
                'report_not_rideable', 'report_not_found',
                'report_vehicle_issue', 'report_improper_parking',
                'qr_scan',
                'battery_contribution', 'nav_route_feedback',
                'nav_qualitative_feedback', 'nav_distance_bonus', 'ride_survey',
                -- New in this migration.
                'device_features_first', 'device_features_review',
                'device_features_reconfirm'
            ));
    END IF;
END $$;
