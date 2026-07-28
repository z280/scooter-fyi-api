-- Allow 'not_found' as a device_reports.report_type — maps the points
-- spec's "Report vehicle not found" action. Distinct from not_rideable
-- (still called failed_unlock when this migration was written; renamed in
-- sql/037): "not found" means the rider went to the GBFS-reported location
-- and the scooter isn't physically there — a data/theft/relocation signal,
-- not "it's here but you can't ride it." Extends the constraint installed
-- by sql/023_improperly_parked_report_type.sql.
--
-- UNLIKE improperly_parked, 'not_found' DOES count toward
-- has_negative_report/reliability_tier: a device that isn't where the
-- feed says it is is a reliability problem with the device/feed itself,
-- not a parking-etiquette complaint. It is therefore deliberately NOT
-- added to NON_RELIABILITY_REPORT_TYPES in src/api_frontend_reports.py.
--
-- REPLAY SAFETY (src/pg.py: "IF NOT EXISTS makes every file safe to
-- re-run" — and the _pg test fixtures execute every sql/ file on every
-- run). This file used to DROP the constraint unconditionally and re-add
-- it with the value list as it stood when it was written, which does not
-- contain 'not_rideable'. Replaying it against a database that had already
-- run sql/037 and held a single not_rideable report therefore died with a
-- CheckViolation — and a migration set that can't be replayed can't build
-- a database from scratch. The guard below makes the rewrite conditional
-- on the constraint not already permitting 'not_found', so replaying after
-- a LATER migration has widened the list is a no-op instead of a
-- regression. Any future migration touching this constraint must keep the
-- same shape.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'device_reports_report_type_allowed'
       AND conrelid = 'device_reports'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('not_found' in current_def) = 0 THEN
        ALTER TABLE device_reports
            DROP CONSTRAINT IF EXISTS device_reports_report_type_allowed;
        -- The original inline column CHECK from sql/013 is auto-named
        -- <table>_<column>_check, in case an instance predates sql/023.
        ALTER TABLE device_reports
            DROP CONSTRAINT IF EXISTS device_reports_report_type_check;
        ALTER TABLE device_reports
            ADD CONSTRAINT device_reports_report_type_allowed
            CHECK (report_type IN (
                'failed_unlock', 'dead_battery', 'damaged', 'improperly_parked',
                'not_found'
            ));
    END IF;
END $$;
