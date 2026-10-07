-- discount_reports: equity receipt claims, Phase 1 (capture).
-- docs/PLAN_EQUITY_RECEIPTS.md "Phase 1"; owner-approved 2026-10-07.
--
-- A Veo receipt carries the scooter code (plate), the trip minutes, the costs
-- and a CHARGE DATE, but no location and no time of day. So a claim row holds
-- what the receipt says plus the rider's optional tie-breakers; where the ride
-- happened is filled in later by matching against feed history (Phase 2,
-- matched_* and match_status).
--
--   vehicle_plate        the code as printed (digits only). ADMIN-ONLY: the
--                        public CSV carries vehicle_identifier (HMAC) instead.
--   trip_minutes, subtotal_cents, total_cents, charge_date
--                        the receipt; the gate (plate + minutes + a cost + date)
--                        is enforced in the endpoint before anything is stored.
--   approx_started_at    optional, rider-supplied, only to break ties.
--   pin_*                optional rider pins; evidence for tie-breaking, never
--                        for eligibility. Rounded to 3 decimals anywhere public.
--   declared_rate_plan   the plan the rider says they were on.
--   plan_evidence_r2_key screenshot of the rider's active plan: REQUIRED for an
--                        equity claim (the contract applies the Equity Area rate
--                        whatever the tier, so the plan is what makes it stand).
--                        Same private bucket and 18-month retention as receipts.
--   expected_cents, rate_error_cents, rate_signature, tax_cents, tax_finding,
--   analysis             arithmetic recorded at submission; nothing is judged.
--
-- ride_ended_at becomes nullable: a receipt has no end time. Legacy v1 rows
-- (0 ever filed) keep theirs.

SET lock_timeout = '10s';

ALTER TABLE discount_reports ALTER COLUMN ride_ended_at DROP NOT NULL;

ALTER TABLE discount_reports
    ADD COLUMN IF NOT EXISTS claim_version        SMALLINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS vehicle_plate        TEXT,
    ADD COLUMN IF NOT EXISTS vehicle_identifier   TEXT,
    ADD COLUMN IF NOT EXISTS trip_minutes         INTEGER,
    ADD COLUMN IF NOT EXISTS subtotal_cents       INTEGER,
    ADD COLUMN IF NOT EXISTS total_cents          INTEGER,
    ADD COLUMN IF NOT EXISTS charge_date          DATE,
    ADD COLUMN IF NOT EXISTS approx_started_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS pin_start_lat        DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS pin_start_lng        DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS pin_end_lat          DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS pin_end_lng          DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS declared_rate_plan   TEXT,
    ADD COLUMN IF NOT EXISTS plan_evidence_r2_key TEXT,
    ADD COLUMN IF NOT EXISTS review_status        TEXT NOT NULL DEFAULT 'received',
    ADD COLUMN IF NOT EXISTS match_status         TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS matched_trip_event_id BIGINT,
    ADD COLUMN IF NOT EXISTS matched_start_lat    DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS matched_start_lng    DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS matched_end_lat      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS matched_end_lng      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS expected_cents       INTEGER,
    ADD COLUMN IF NOT EXISTS rate_error_cents     INTEGER,
    ADD COLUMN IF NOT EXISTS rate_signature       TEXT,
    ADD COLUMN IF NOT EXISTS tax_cents            INTEGER,
    ADD COLUMN IF NOT EXISTS tax_finding          TEXT,
    ADD COLUMN IF NOT EXISTS analysis             JSONB;

ALTER TABLE discount_reports DROP CONSTRAINT IF EXISTS discount_reports_claim_shape_check;
ALTER TABLE discount_reports ADD CONSTRAINT discount_reports_claim_shape_check CHECK (
    claim_version = 1 AND ride_ended_at IS NOT NULL
    OR claim_version = 2
       AND vehicle_plate ~ '^[0-9]{7,10}$'
       AND trip_minutes BETWEEN 1 AND 600
       AND (subtotal_cents IS NOT NULL OR total_cents IS NOT NULL)
       AND charge_date IS NOT NULL
       AND plan_evidence_r2_key IS NOT NULL
);

ALTER TABLE discount_reports DROP CONSTRAINT IF EXISTS discount_reports_claim_values_check;
ALTER TABLE discount_reports ADD CONSTRAINT discount_reports_claim_values_check CHECK (
    (subtotal_cents IS NULL OR subtotal_cents BETWEEN 0 AND 100000)
    AND (total_cents IS NULL OR total_cents BETWEEN 0 AND 100000)
    AND (declared_rate_plan IS NULL OR declared_rate_plan IN
         ('resident', 'resident_plus', 'visitor', 'visitor_plus', 'equity', 'unknown'))
    AND review_status IN ('received', 'verified', 'uncertain', 'rejected')
    AND match_status IN ('pending', 'waiting', 'corroborated', 'partial',
                         'ambiguous', 'none', 'unknown_vehicle', 'not_equity_ride')
    AND (tax_finding IS NULL OR tax_finding IN ('tax_ok', 'tax_rounded_up', 'tax_unexplained'))
    AND ((pin_start_lat IS NULL) = (pin_start_lng IS NULL))
    AND ((pin_end_lat IS NULL) = (pin_end_lng IS NULL))
);

-- Phase 2 matches by plate + charge date.
CREATE INDEX IF NOT EXISTS idx_discount_reports_plate_date
    ON discount_reports (vehicle_plate, charge_date) WHERE vehicle_plate IS NOT NULL;

-- The rider's own list (My receipts, Phase 2).
-- (idx_discount_reports_account on (account_id) already exists from sql/013;
-- a new name, or IF NOT EXISTS would silently skip this one.)
CREATE INDEX IF NOT EXISTS idx_discount_reports_account_created
    ON discount_reports (account_id, created_at DESC);

COMMENT ON COLUMN discount_reports.vehicle_plate IS
    'Scooter code as printed on the receipt. Admin-only; public surfaces use vehicle_identifier. See sql/093.';

RESET lock_timeout;
