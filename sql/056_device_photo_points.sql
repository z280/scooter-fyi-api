-- Device photos earn points (owner: "Grant 6pts per uploaded photo").
--
-- Schema-wise this is one line of real work — widening user_points.action
-- to accept 'device_photo'. Everything else about the award lives in code:
-- the VALUE is src/points.py:POINTS_DEVICE_PHOTO (sql/028's rule: "a value
-- tweak is a code change, not a migration"), and the credit itself goes
-- through src/points.py:credit_device_photo_points.
--
-- WHY NO NEW COLUMN ON device_photos: the ledger row already points back at
-- the photo through (source_table, source_id) = ('device_photos', id), which
-- is also what makes the credit idempotent — sql/028's
-- idx_user_points_source_dedupe is a partial UNIQUE on
-- (source_table, source_id, action), so a retried upload that somehow
-- reached the credit twice writes one row, not two. A denormalized
-- points_awarded column on device_photos would be a second copy of a fact
-- the ledger already owns, and SUM(user_points.points) is the only
-- definition of a rider's total.
--
-- ABUSE BOUND, for the record: this award needs no per-account cooldown of
-- the kind sql/055's feature awards carry, because it is bounded per
-- VEHICLE instead — at most MAX_PHOTOS_PER_DEVICE (3) awards each, counted
-- from THIS LEDGER by credit_device_photo_points, not from how many photos
-- the device currently holds.
--
-- The distinction matters and is the whole reason the check reads the
-- ledger. The endpoint's own cap query counts `status = 'visible'`, and
-- sql/031 reserves that column for a future moderator workflow. The day a
-- photo can be hidden is the day "3 photos per device" stops bounding
-- "3 awards per device" — hide, re-upload, get paid again, on a loop.
-- Counting paid awards instead keeps the bound true whatever that workflow
-- does later. (Points are never clawed back when a photo goes away; nothing
-- in this program reverses a credit.)
--
-- POINTS ARE NEVER ANONYMOUS (sql/028). That holds here by construction:
-- the upload endpoint is require_session, so every credited photo has a real
-- account_id behind it. A rider who hides their public_username is still a
-- known account to us — `uploaded_by` goes null in the PUBLIC listing only.

DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'user_points_action_allowed'
       AND conrelid = 'user_points'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('device_photo' in current_def) = 0 THEN
        ALTER TABLE user_points DROP CONSTRAINT IF EXISTS user_points_action_allowed;
        ALTER TABLE user_points
            ADD CONSTRAINT user_points_action_allowed
            CHECK (action IN (
                -- Everything sql/028 + sql/037 + sql/052 + sql/053 + sql/055
                -- allow. Repeated in full because a CHECK is replaced
                -- wholesale, not appended to.
                'profile_completion', 'waypoint', 'gbfs_trip_validated',
                'report_not_rideable', 'report_not_found',
                'report_vehicle_issue', 'report_improper_parking',
                'qr_scan',
                'battery_contribution', 'nav_route_feedback',
                'nav_qualitative_feedback', 'nav_distance_bonus', 'ride_survey',
                'device_features_first', 'device_features_review',
                'device_features_reconfirm',
                -- New in this migration.
                'device_photo'
            ));
    END IF;
END $$;
