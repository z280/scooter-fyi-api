-- Fleet reports, Phase 1 (docs/FLEET_REPORTS_PLAN.md §4.1 items 1, 3a, 3b, 7).
--
-- Five changes, one migration, because they are one feature and none of them
-- is useful alone:
--
--   1. 'inaccessible' joins device_reports.report_type (§2.1): "the vehicle
--      may be perfectly fine; you cannot lawfully or reasonably reach it".
--   2. device_reports.range_at_report_meters (§2.4 / §4.1(3a)): the charge as
--      it stood when the report was filed, so a report clears on a RISE in
--      charge rather than on a LEVEL. The old rule cleared a signed-in report
--      the moment the vehicle read 100%, which made a fully charged scooter
--      unreportable — including the one behind the fence that prompted the
--      plan.
--   3. device_reports.resolved_at / resolved_by / resolution (§4.1(3b)): an
--      admin can void or resolve a report. Without them "unresolved" has no
--      meaning and suppression cannot be undone by anyone but Veo.
--   4. device_census_ack (§2.8 / §4.1(7)): an admin's judgement that a missing
--      vehicle is permanently gone. A TABLE, not a device_state column,
--      because device_state is rewritten by every ingest cycle and an admin's
--      judgement must not live where a cycle can overwrite it.
--   5. The missing index on device_state.first_ever_observed_at, which the
--      newest-arrivals list sorts by.
--   6. device_reports.reason / submitted_reason (owner, 2026-10-09): WHY a
--      vehicle is not rideable — acceleration, flat_tire, wheel, lighting,
--      seat, handlebar — and, when the rider picked one of the picker's two
--      decoys ("cannot find", "dead battery"), which one, because the server
--      re-files those as `inaccessible` / `dead_battery` and the remap should
--      be visible rather than silent.
--
-- REPLAY SAFETY. Every file in sql/ is re-executed by the _pg test fixtures
-- against a persistent database. The constraint rewrite below is therefore
-- guarded on the new value already being permitted (the sql/029 shape —
-- read that file's header), and sql/037's device_reports block was guarded
-- in the same change, because it used to drop and re-add the five-value list
-- unconditionally and would have rejected the first stored 'inaccessible'
-- row on the next replay. Both historical constraint names are dropped
-- (device_reports_report_type_allowed, and sql/013's inline
-- device_reports_report_type_check for instances predating sql/023).
-- Adding a permitted value rewrites no rows, so no UPDATE is needed.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'device_reports_report_type_allowed'
       AND conrelid = 'device_reports'::regclass
       AND contype = 'c';

    IF current_def IS NULL OR position('inaccessible' in current_def) = 0 THEN
        ALTER TABLE device_reports
            DROP CONSTRAINT IF EXISTS device_reports_report_type_allowed;
        ALTER TABLE device_reports
            DROP CONSTRAINT IF EXISTS device_reports_report_type_check;
        ALTER TABLE device_reports
            ADD CONSTRAINT device_reports_report_type_allowed
            CHECK (report_type IN (
                'not_rideable', 'dead_battery', 'damaged', 'improperly_parked',
                'not_found', 'inaccessible'
            ));
    END IF;
END $$;

-- 2. Charge at report time. NULL when the vehicle was not in a recent feed
-- snapshot when the report arrived, and for every report filed before this
-- migration. A NULL clears nothing (the report holds until the vehicle
-- moves), exactly as a NULL current range always has.
ALTER TABLE device_reports
    ADD COLUMN IF NOT EXISTS range_at_report_meters INTEGER;

-- 3. Resolution. resolved_by is the ADMIN's account (ON DELETE SET NULL, the
-- same "the account link is removed, the record stays" rule as account_id).
-- `resolution` is a short audited note: why it was voided.
ALTER TABLE device_reports
    ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS resolved_by BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS resolution TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'device_reports_resolution_length'
           AND conrelid = 'device_reports'::regclass
    ) THEN
        ALTER TABLE device_reports
            ADD CONSTRAINT device_reports_resolution_length
            CHECK (resolution IS NULL OR char_length(resolution) <= 500);
    END IF;
END $$;

-- Suppression (§2.5) and the accountable has_negative_report branch both scan
-- signed-in, unresolved reports per vehicle with no time bound. Partial, so
-- the index stays the size of the live set rather than of history.
CREATE INDEX IF NOT EXISTS idx_device_reports_open_accountable
    ON device_reports (vehicle_identifier, reported_at DESC)
    WHERE account_id IS NOT NULL AND resolved_at IS NULL;

-- 4. The census acknowledgement.
--
--   status            'gone' = an admin confirmed it is not coming back;
--                     'not_gone' = acknowledgement withdrawn, or a note on a
--                     vehicle nobody has called gone. Withdrawing keeps the
--                     row (and its note) rather than deleting it, so the
--                     history of a judgement is not erased by reversing it.
--   last_observed_at_ack
--                     device_state.last_observed_at when the admin
--                     acknowledged. A later last_observed_at is a vehicle
--                     marked gone that came back — surfaced, never silently
--                     relisted, and the ack is never deleted for it (§2.8).
--   acknowledged_by / withdrawn_by
--                     the admin's account, ON DELETE SET NULL.
CREATE TABLE IF NOT EXISTS device_census_ack (
    vehicle_identifier   TEXT PRIMARY KEY,
    status               TEXT NOT NULL DEFAULT 'gone'
                         CONSTRAINT device_census_ack_status_allowed
                         CHECK (status IN ('gone', 'not_gone')),
    acknowledged_by      BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
    acknowledged_at      TIMESTAMPTZ,
    last_observed_at_ack TIMESTAMPTZ,
    withdrawn_by         BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
    withdrawn_at         TIMESTAMPTZ,
    note                 TEXT
                         CONSTRAINT device_census_ack_note_length
                         CHECK (note IS NULL OR char_length(note) <= 2000),
    note_by              BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
    note_at              TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_device_census_ack_gone
    ON device_census_ack (acknowledged_at DESC)
    WHERE status = 'gone';

-- 5. Newest arrivals sort on this. first_ever_observed_at is "never reset";
-- do NOT confuse it with first_observed_at_location two columns away, which
-- resets on every move (§2.8, the likeliest single bug in the section).
CREATE INDEX IF NOT EXISTS idx_device_state_first_ever_observed
    ON device_state (first_ever_observed_at DESC);

-- 6. Why not rideable. NULL = unspecified: every report before this
-- migration, and every client that does not send one. Only a not_rideable
-- report may carry a reason; the API refuses one on any other type, and this
-- constraint is the backstop.
--
-- submitted_reason records a DECOY the server remapped: the not_rideable
-- picker also offers "cannot find" and "dead battery", which are not reasons
-- a vehicle won't ride but different reports, so the API stores them as
-- report_type 'inaccessible' / 'dead_battery' with reason NULL and keeps the
-- choice here. It is NULL for every report that was not remapped.
ALTER TABLE device_reports
    ADD COLUMN IF NOT EXISTS reason TEXT,
    ADD COLUMN IF NOT EXISTS submitted_reason TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'device_reports_reason_allowed'
           AND conrelid = 'device_reports'::regclass
    ) THEN
        ALTER TABLE device_reports
            ADD CONSTRAINT device_reports_reason_allowed
            CHECK (reason IS NULL OR (
                report_type = 'not_rideable'
                AND reason IN ('acceleration', 'flat_tire', 'wheel',
                               'lighting', 'seat', 'handlebar')
            ));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'device_reports_submitted_reason_allowed'
           AND conrelid = 'device_reports'::regclass
    ) THEN
        ALTER TABLE device_reports
            ADD CONSTRAINT device_reports_submitted_reason_allowed
            CHECK (submitted_reason IS NULL OR (
                (submitted_reason = 'cannot_find' AND report_type = 'inaccessible')
                OR (submitted_reason = 'dead_battery' AND report_type = 'dead_battery')
            ));
    END IF;
END $$;
