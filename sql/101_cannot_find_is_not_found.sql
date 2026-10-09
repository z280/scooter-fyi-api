-- The "cannot find" decoy is re-filed as `not_found`, not `inaccessible`
-- (owner, 2026-10-09, after sql/100 shipped in #144).
--
-- sql/100 tied submitted_reason = 'cannot_find' to report_type =
-- 'inaccessible'. The owner's correction: "cannot find" means the scooter is
-- not where the map says — `not_found`. `inaccessible` stays its own type,
-- for a scooter you can SEE but cannot reach (fenced in, locked inside,
-- private property).
--
-- A NEW FILE, not an edit to sql/100: sql/100 may already be recorded in
-- schema_migrations on production, so editing it would change nothing there.
--
-- Any row sql/100's rule already stored that way is re-filed under the type
-- the rider meant. Both suppress, so no vehicle's suppression changes; only
-- the reason it reports does.
--
-- REPLAY SAFETY. Guarded on the constraint already pairing 'cannot_find' with
-- 'not_found', so a whole-directory replay leaves it alone. sql/100's own
-- block is guarded on the constraint name existing, so on a replay it never
-- reinstates the old pairing over this one.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'device_reports_submitted_reason_allowed'
       AND conrelid = 'device_reports'::regclass
       AND contype = 'c';

    IF current_def IS NULL
       OR current_def NOT LIKE '%cannot_find%not_found%' THEN
        ALTER TABLE device_reports
            DROP CONSTRAINT IF EXISTS device_reports_submitted_reason_allowed;

        UPDATE device_reports
           SET report_type = 'not_found'
         WHERE submitted_reason = 'cannot_find'
           AND report_type = 'inaccessible';

        ALTER TABLE device_reports
            ADD CONSTRAINT device_reports_submitted_reason_allowed
            CHECK (submitted_reason IS NULL OR (
                (submitted_reason = 'cannot_find' AND report_type = 'not_found')
                OR (submitted_reason = 'dead_battery' AND report_type = 'dead_battery')
            ));
    END IF;
END $$;
