-- Rename the device_reports type 'failed_unlock' -> 'not_rideable', and the
-- matching points action 'report_wont_start' -> 'report_not_rideable'.
--
-- The old name described one specific failure (the unlock didn't work). The
-- rider-facing question is broader and simpler: could you ride it or not?
-- A scooter that unlocks and then won't move, or that is unsafe to ride, is
-- the same answer to the rider. The button now reads "🚫 Not Rideable",
-- matching the "Likely rideable" tier language the map already uses.
--
-- Spelling: "rideable", consistent with the existing "Likely rideable".
--
-- Order matters. Each CHECK is dropped BEFORE its rows are rewritten and
-- re-added after, because the constraint would otherwise reject the very
-- UPDATE that migrates the data.

-- --------------------------------------------------------------------------
-- device_reports.report_type
-- --------------------------------------------------------------------------
ALTER TABLE device_reports
    DROP CONSTRAINT IF EXISTS device_reports_report_type_allowed;
-- The original inline constraint from sql/013, in case an instance predates
-- sql/023 having renamed it.
ALTER TABLE device_reports
    DROP CONSTRAINT IF EXISTS device_reports_report_type_check;

UPDATE device_reports
   SET report_type = 'not_rideable'
 WHERE report_type = 'failed_unlock';

ALTER TABLE device_reports
    ADD CONSTRAINT device_reports_report_type_allowed
    CHECK (report_type IN (
        'not_rideable', 'dead_battery', 'damaged', 'improperly_parked',
        'not_found'
    ));

-- --------------------------------------------------------------------------
-- user_points.action
-- --------------------------------------------------------------------------
ALTER TABLE user_points
    DROP CONSTRAINT IF EXISTS user_points_action_allowed;

UPDATE user_points
   SET action = 'report_not_rideable'
 WHERE action = 'report_wont_start';

ALTER TABLE user_points
    ADD CONSTRAINT user_points_action_allowed
    CHECK (action IN (
        'profile_completion', 'waypoint', 'gbfs_trip_validated',
        'report_not_rideable', 'report_not_found',
        'report_vehicle_issue', 'report_improper_parking',
        'qr_scan'
    ));
