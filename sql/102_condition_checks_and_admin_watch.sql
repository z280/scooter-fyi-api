-- Fleet reports Phases 1b + 2 (docs/FLEET_REPORTS_PLAN.md §4.4, §4.1(5)).
--
--   1. device_reports gains WHO resolved it in which capacity
--      (resolution_source: an admin, or a rider's condition check), the
--      GitHub login when an admin resolved it from the /admin pages (that
--      session has no rider account), the condition check that resolved it,
--      the reconfirmation stamp a "still a problem" answer writes, and the
--      reinstatement an admin may apply to a RIDER resolution (§4.4: the one
--      case where un-resolving is allowed, because the resolution was not an
--      admin's judgement).
--   2. device_condition_checks + device_condition_check_answers: a rider's
--      condition check (§4.4) and its per-report answers — the audit the
--      dossier and the reporter view read.
--   3. user_points gains 'condition_check' (10) and
--      'condition_check_confirmed' (+40).
--   4. admin_device_watches: an admin subscribed to one vehicle's movement
--      and state changes by SMS (§4.1(5) "Watch a device via SMS").
--
-- REPLAY SAFETY. Every file in sql/ is re-executed by the _pg fixtures
-- against a persistent database, so everything below is IF NOT EXISTS or
-- guarded on the new state already being present (the sql/029 shape).

-- ---------------------------------------------------------------------------
-- 1. Resolution provenance and reconfirmation on device_reports
-- ---------------------------------------------------------------------------
ALTER TABLE device_reports
    ADD COLUMN IF NOT EXISTS resolution_source    TEXT,
    ADD COLUMN IF NOT EXISTS resolved_by_login    TEXT,
    ADD COLUMN IF NOT EXISTS resolved_by_check_id BIGINT,
    ADD COLUMN IF NOT EXISTS last_reconfirmed_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reconfirm_count      INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reinstated_at        TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reinstated_by_login  TEXT,
    ADD COLUMN IF NOT EXISTS reinstate_reason     TEXT;

-- HOW A NEGATIVE REPORT CLEARS (owner, 2026-10-09; src/fleet_reports.py):
-- a >= 100 m straight-line move from where the vehicle was when reported
-- (plus a charge rise, for a rideability report), or reappearing >= 100 m
-- away after going off the map (with a full battery, for a rideability
-- report). That needs the vehicle's position at report time, which
-- device_reports never stored (lat/lng are the REPORTER's, optional):
--
--   vehicle_lat/lon_at_report  device_state.current_lat/lon when the report
--                              arrived; backfilled below from the stop the
--                              vehicle was at, else the reporter's point.
--   baseline_lat/lon, baseline_range_meters, baseline_at
--                              a reconfirmation ("still a problem" after a
--                              test ride) re-baselines the report at the
--                              vehicle's position and charge once the ride
--                              settles; NULL = use the report-time values.
--   baseline_pending           TRUE from the reconfirmation until it
--                              settles: the report cannot clear meanwhile.
--   baseline_check_id          which check owns the pending baseline.
ALTER TABLE device_reports
    ADD COLUMN IF NOT EXISTS vehicle_lat_at_report DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS vehicle_lon_at_report DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS baseline_lat          DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS baseline_lon          DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS baseline_range_meters INTEGER,
    ADD COLUMN IF NOT EXISTS baseline_at           TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS baseline_pending      BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS baseline_check_id     BIGINT;

-- Backfill: the stop the vehicle occupied when the report was filed.
-- Idempotent (only NULL rows), so a replay is a no-op.
UPDATE device_reports dr
   SET (vehicle_lat_at_report, vehicle_lon_at_report) = (
        SELECT h.lat, h.lon FROM device_history h
         WHERE h.vehicle_identifier = dr.vehicle_identifier
           AND h.snapshot_time <= dr.reported_at
         ORDER BY h.snapshot_time DESC LIMIT 1)
 WHERE dr.vehicle_lat_at_report IS NULL
   AND EXISTS (SELECT 1 FROM device_history h
                WHERE h.vehicle_identifier = dr.vehicle_identifier
                  AND h.snapshot_time <= dr.reported_at);
UPDATE device_reports
   SET vehicle_lat_at_report = lat, vehicle_lon_at_report = lng
 WHERE vehicle_lat_at_report IS NULL AND lat IS NOT NULL AND lng IS NOT NULL;

-- Straight-line metres between two points, equirectangular — exact to well
-- under a metre at city scale, and IMMUTABLE so the planner can inline it.
-- NULL in, NULL out (callers COALESCE to "not moved").
CREATE OR REPLACE FUNCTION geo_distance_m(
    lat1 DOUBLE PRECISION, lon1 DOUBLE PRECISION,
    lat2 DOUBLE PRECISION, lon2 DOUBLE PRECISION
) RETURNS DOUBLE PRECISION
LANGUAGE sql IMMUTABLE AS $$
    SELECT 6371008.8 * sqrt(
        power(radians(lat2 - lat1), 2)
        + power(radians(lon2 - lon1) * cos(radians((lat1 + lat2) / 2)), 2))
$$;

-- Every resolution written before this migration came from the Phase 1
-- admin endpoint, so it is an admin's.
UPDATE device_reports
   SET resolution_source = 'admin'
 WHERE resolved_at IS NOT NULL AND resolution_source IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'device_reports_resolution_source_allowed'
           AND conrelid = 'device_reports'::regclass
    ) THEN
        ALTER TABLE device_reports
            ADD CONSTRAINT device_reports_resolution_source_allowed
            CHECK (
                -- NULL source with a resolved_at is read as 'admin' (the only
                -- resolver before this migration); a source never outlives
                -- its resolution (a reinstatement clears both).
                resolution_source IS NULL
                OR (resolved_at IS NOT NULL
                    AND resolution_source IN ('admin', 'rider_check'))
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'device_reports_reinstate_reason_length'
           AND conrelid = 'device_reports'::regclass
    ) THEN
        ALTER TABLE device_reports
            ADD CONSTRAINT device_reports_reinstate_reason_length
            CHECK (reinstate_reason IS NULL OR char_length(reinstate_reason) <= 500);
    END IF;
END $$;

-- The census acknowledgement (sql/100) gets the same treatment: an admin
-- acting from the /admin pages is a GitHub login, not an account.
ALTER TABLE device_census_ack
    ADD COLUMN IF NOT EXISTS acknowledged_by_login TEXT,
    ADD COLUMN IF NOT EXISTS withdrawn_by_login    TEXT,
    ADD COLUMN IF NOT EXISTS note_by_login         TEXT;

-- ---------------------------------------------------------------------------
-- 2. Condition checks
-- ---------------------------------------------------------------------------
--
--   test_ride       the rider's answer to "Did you do a test ride?". FALSE
--                   rows are a minimal audit only: every condition answer
--                   in that submission was DISCARDED — no answer rows, no
--                   report changes, no points. Kept so the reporter view
--                   can see an account submitting checks it never rides.
--   proof           how presence at the scooter was shown: a plate-valid
--                   feature confirmation by the same account on the same
--                   vehicle in the last hour, a typed plate, or a scanned QR.
--   *_at_check      the vehicle as device_state had it when the check
--                   arrived — the baseline the feed confirmation compares
--                   against (a rental or a move since).
--   feed_status     not_applicable (test_ride false, or no base award) /
--                   pending (waiting on the feed) / confirmed / unconfirmed
--                   (the window closed with no rental or move).
--   points_*        what the ledger paid for this check (also in user_points
--                   with source_table = 'device_condition_checks').
--   points_withheld why the base 10 was not paid, when it was not.
CREATE TABLE IF NOT EXISTS device_condition_checks (
    id                         BIGSERIAL PRIMARY KEY,
    vehicle_identifier         TEXT NOT NULL,
    account_id                 BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
    submitted_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    test_ride                  BOOLEAN NOT NULL,
    proof                      TEXT NOT NULL
                               CONSTRAINT device_condition_checks_proof_allowed
                               CHECK (proof IN ('feature_report', 'plate', 'qr')),
    feature_report_id          BIGINT,
    reports_resolved           INTEGER NOT NULL DEFAULT 0,
    reports_reconfirmed        INTEGER NOT NULL DEFAULT 0,
    parked_since_at_check      TIMESTAMPTZ,
    rental_started_at_check    TIMESTAMPTZ,
    lat_at_check               DOUBLE PRECISION,
    lon_at_check               DOUBLE PRECISION,
    feed_status                TEXT NOT NULL DEFAULT 'not_applicable'
                               CONSTRAINT device_condition_checks_feed_status_allowed
                               CHECK (feed_status IN ('not_applicable', 'pending',
                                                      'confirmed', 'unconfirmed')),
    feed_signal                TEXT
                               CONSTRAINT device_condition_checks_feed_signal_allowed
                               CHECK (feed_signal IS NULL OR feed_signal IN (
                                   'reserved', 'moved', 'rental_before_check',
                                   'moved_before_check')),
    feed_checked_at            TIMESTAMPTZ,
    points_base                INTEGER NOT NULL DEFAULT 0,
    points_confirmed           INTEGER NOT NULL DEFAULT 0,
    points_withheld            TEXT,
    -- When the reconfirmed reports' baselines were settled (NULL until
    -- then, and for checks that reconfirmed nothing).
    baseline_settled_at     TIMESTAMPTZ
);
ALTER TABLE device_condition_checks
    ADD COLUMN IF NOT EXISTS baseline_settled_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_condition_checks_vehicle
    ON device_condition_checks (vehicle_identifier, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_condition_checks_account
    ON device_condition_checks (account_id, submitted_at DESC);
-- The per-cycle feed confirmation reads only these; a handful of rows.
CREATE INDEX IF NOT EXISTS idx_condition_checks_pending
    ON device_condition_checks (submitted_at)
    WHERE feed_status = 'pending';

--   outcome   resolved      "no longer a problem" — the report was resolved
--                           (resolution_source = 'rider_check');
--             reconfirmed   "still a problem" — stamped on the report;
--             found         a standing not_found report, resolved because the
--                           rider was standing at the scooter (never asked);
--             stale         the report had stopped standing between the GET
--                           and the POST (moved, charged, resolved): the
--                           answer is kept and changes nothing.
CREATE TABLE IF NOT EXISTS device_condition_check_answers (
    check_id         BIGINT NOT NULL REFERENCES device_condition_checks(id) ON DELETE CASCADE,
    report_id        BIGINT NOT NULL REFERENCES device_reports(id) ON DELETE CASCADE,
    still_a_problem  BOOLEAN,
    outcome          TEXT NOT NULL
                     CONSTRAINT device_condition_check_answers_outcome_allowed
                     CHECK (outcome IN ('resolved', 'reconfirmed', 'found', 'stale')),
    own_report       BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (check_id, report_id)
);

CREATE INDEX IF NOT EXISTS idx_condition_check_answers_report
    ON device_condition_check_answers (report_id);

-- ---------------------------------------------------------------------------
-- 3. Points actions. The full live list plus the two new ones (sql/078's
-- warning: a dropped-and-recreated CHECK must be complete). Guarded on the
-- new action already being permitted, so a replay leaves it alone.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'user_points_action_allowed'
       AND conrelid = 'user_points'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('condition_check_confirmed' in current_def) = 0 THEN
        ALTER TABLE user_points DROP CONSTRAINT IF EXISTS user_points_action_allowed;
        ALTER TABLE user_points ADD CONSTRAINT user_points_action_allowed CHECK (
            action IN (
                'battery_contribution', 'device_features_first', 'device_features_reconfirm',
                'device_features_review', 'device_photo', 'gbfs_trip_validated',
                'nav_distance_bonus', 'nav_qualitative_feedback', 'nav_route_feedback',
                'profile_completion', 'qr_scan', 'report_improper_parking',
                'report_not_found', 'report_not_rideable', 'report_vehicle_issue',
                'ride_survey', 'waypoint', 'referral',
                'stand_down', 'condition_check', 'condition_check_confirmed'
            )
        );
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4. Admin SMS device watches
-- ---------------------------------------------------------------------------
--
-- The /admin pages are GitHub-OAuth sessions with no rider account, but a
-- verified phone lives on a rider account. So a watch names BOTH: the
-- GitHub login that created it (who), and the admin-allowlisted account
-- whose VERIFIED phone receives the texts (where). consent_at is the
-- moment the admin ticked "text this account's phone"; comms enforces STOP
-- across the shared number, and accounts.sms_opted_out_at mirrors it.
--
--   last_*            the vehicle's state as the watcher last saw it; a
--                     change from these is what gets texted.
--   texts_sent        bounded by the watcher's per-watch cap.
--   ended_reason      expired / unsubscribed / opted_out / unusable /
--                     no_phone / unverified / cap_reached.
CREATE TABLE IF NOT EXISTS admin_device_watches (
    id                    BIGSERIAL PRIMARY KEY,
    vehicle_identifier    TEXT NOT NULL,
    created_by_login      TEXT NOT NULL,
    account_id            BIGINT REFERENCES accounts(id) ON DELETE CASCADE,
    consent_at            TIMESTAMPTZ NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at            TIMESTAMPTZ NOT NULL,
    ended_at              TIMESTAMPTZ,
    ended_reason          TEXT
                          CONSTRAINT admin_device_watches_ended_reason_allowed
                          CHECK (ended_reason IS NULL OR ended_reason IN (
                              'expired', 'unsubscribed', 'opted_out', 'unusable',
                              'no_phone', 'unverified', 'cap_reached')),
    ended_by_login        TEXT,
    last_in_feed          BOOLEAN,
    last_reserved         BOOLEAN,
    last_disabled         BOOLEAN,
    last_lat              DOUBLE PRECISION,
    last_lon              DOUBLE PRECISION,
    last_seen_at          TIMESTAMPTZ,
    texts_sent            INTEGER NOT NULL DEFAULT 0,
    last_texted_at        TIMESTAMPTZ,
    last_event            TEXT
);

CREATE INDEX IF NOT EXISTS idx_admin_device_watches_live
    ON admin_device_watches (expires_at)
    WHERE ended_at IS NULL;
