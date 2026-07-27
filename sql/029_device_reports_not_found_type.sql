-- Allow 'not_found' as a device_reports.report_type — maps the points
-- spec's "Report vehicle not found" action. Distinct from failed_unlock:
-- "not found" means the rider went to the GBFS-reported location and the
-- scooter isn't physically there — a data/theft/relocation signal, not
-- "it's here but won't unlock." Exact copy of the
-- sql/023_improperly_parked_report_type.sql pattern.
--
-- UNLIKE improperly_parked, 'not_found' DOES count toward
-- has_negative_report/reliability_tier: a device that isn't where the
-- feed says it is is a reliability problem with the device/feed itself,
-- not a parking-etiquette complaint. It is therefore deliberately NOT
-- added to NON_RELIABILITY_REPORT_TYPES in src/api_frontend_reports.py.

ALTER TABLE device_reports
    DROP CONSTRAINT IF EXISTS device_reports_report_type_allowed;

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
                'failed_unlock', 'dead_battery', 'damaged', 'improperly_parked',
                'not_found'
            ));
    END IF;
END $$;
