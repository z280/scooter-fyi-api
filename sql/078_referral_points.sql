-- Paying out referrals and stand-downs.
--
-- sql/076 created `referrals` with `activated_at` and `awarded_at` columns
-- and a note that a lead is not a referral until the newcomer turns up and
-- rides. sql/077 added the stand-down's second debt. Neither column has ever
-- been written by anything: the certificate page has been promising 100, 300
-- and 50 points that nothing pays.
--
-- This is the missing half. Two changes, both small, because the machinery
-- already exists — `user_points` (sql/028) is the ledger and `credit_points`
-- (src/points.py) is the one writer.
--
-- 1. THE ACTION VOCABULARY. `user_points.action` is a CHECK constraint, not
--    free text, so a ledger row cannot be written for an action the schema
--    has not heard of. Two new ones:
--
--      'referral'   — paid to the REFERRER when someone they introduced
--                     completes their first ride.
--      'stand_down' — paid to the NEWCOMER for handing a scooter back.
--
--    Both are even, which the even-points invariant (sql/053) requires and
--    src/points.py asserts: 100, 300 and 50 all are.
--
-- 2. The payout has to be idempotent per referral, and `user_points` already
--    has the mechanism: a unique index on (source_table, source_id, action)
--    with `credit_points` doing ON CONFLICT DO NOTHING. Rows are written with
--    source_table = 'referrals' and source_id = the referral id, so a retry
--    after a dropped connection — or two rides finishing in the same second —
--    cannot pay twice. Nothing to add here; recorded so the guarantee is
--    written down where the columns are.

-- The list below is the LIVE constraint's vocabulary plus the two new
-- actions, read out of pg_constraint rather than reassembled by hand. A
-- first draft of this file did reassemble it from memory and got three
-- entries wrong: it invented 'report_wont_start' and 'track_donation', and
-- DROPPED 'report_not_rideable' — which would have made every
-- not-rideable report fail its insert. Dropping and recreating a CHECK
-- means the replacement must be complete, not merely correct about the
-- part being added.
ALTER TABLE user_points DROP CONSTRAINT IF EXISTS user_points_action_allowed;
ALTER TABLE user_points ADD CONSTRAINT user_points_action_allowed CHECK (
    action IN (
        'battery_contribution', 'device_features_first', 'device_features_reconfirm',
        'device_features_review', 'device_photo', 'gbfs_trip_validated',
        'nav_distance_bonus', 'nav_qualitative_feedback', 'nav_route_feedback',
        'profile_completion', 'qr_scan', 'report_improper_parking',
        'report_not_found', 'report_not_rideable', 'report_vehicle_issue',
        'ride_survey', 'waypoint', 'referral',
        'stand_down'
    )
);

-- "Which referrals are owed and not yet paid?" — the sweep the activation
-- path makes. Partial: paid rows are the overwhelming majority over time.
CREATE INDEX IF NOT EXISTS idx_referrals_payable
    ON referrals (activated_at)
    WHERE awarded_at IS NULL;
