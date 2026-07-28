-- Allow 'improperly_parked' as a device_reports.report_type.
--
-- A rider who spots a badly-parked Veo (blocking a sidewalk, ADA ramp,
-- transit stop, driveway…) files it from the map; the frontend also opens
-- Veo's Zendesk form pre-filled. These reports ARE counted in the reports
-- summary aggregate + monthly CSV export (the DOTI-relevant compliance
-- signal). They are deliberately EXCLUDED from has_negative_report /
-- reliability_tier — a badly-parked scooter can still ride perfectly, so a
-- parking complaint must not flag it "avoid" on the worth-the-walk view.
-- That exclusion lives in the app query (api_public.py / api_h3.py), not in
-- this constraint, which only governs what values are storable.

-- The original inline column CHECK from sql/013 is auto-named
-- <table>_<column>_check; drop it and install a named, extended one.
--
-- REPLAY SAFETY: the ADD below is guarded on the named constraint not
-- existing, so replaying this file after a LATER migration has widened the
-- value list (sql/029, sql/037) leaves that wider list alone instead of
-- reinstating this one — which would reject rows those migrations
-- legitimately stored. Keep that guard; see the longer note in sql/029,
-- which had to be repaired for exactly this reason.
ALTER TABLE device_reports
    DROP CONSTRAINT IF EXISTS device_reports_report_type_check;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'device_reports_report_type_allowed'
          AND conrelid = 'device_reports'::regclass
          AND contype = 'c'
    ) THEN
        ALTER TABLE device_reports
            ADD CONSTRAINT device_reports_report_type_allowed
            CHECK (report_type IN (
                'failed_unlock', 'dead_battery', 'damaged', 'improperly_parked'
            ));
    END IF;
END $$;
