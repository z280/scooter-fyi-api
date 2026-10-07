-- Receipt claims no longer require a screenshot of the rider's plan.
--
-- Owner, 2026-10-07: "Remove the plan screenshot/requirement for the same, we
-- will trust the user's plan." `declared_rate_plan` — what the rider says they
-- were on — stands on its own.
--
-- WHY THIS IS A REAL IMPROVEMENT AND NOT A LOWERED BAR. The plan screenshot was
-- required (sql/093, owner 2026-10-06) because the Equity Area rate applies
-- whatever tier you are on, so the tier is what makes a claim stand. But the
-- claim is checked against the FEED, not against the rider's paperwork: the
-- arithmetic in `receipt_claims.py` prices the minutes at the Equity Area rate
-- and compares, and Phases 2–3 corroborate against our own trip observations.
-- The screenshot was a second image of someone's account page, held for 18
-- months, that no automated step read. Collecting an image nothing consumes is a
-- privacy cost with no evidentiary return.
--
-- THE COLUMN STAYS. Claims already filed may carry a key, and
-- `cleanup_receipts` finds images only through table rows — dropping the column
-- would orphan those objects in the bucket past their 18 months, which is the
-- exact failure sql/093's own insert path takes care to avoid. It becomes
-- write-never, read-by-cleanup. A later migration can drop it once the last
-- key has aged out.
--
-- Only the CHECK changes: the shape rule stops demanding the key. Everything
-- else about a v2 claim — the plate, the minutes, a cost, the charge date — is
-- untouched, and is still enforced here as well as in the endpoint.

SET lock_timeout = '10s';

ALTER TABLE discount_reports DROP CONSTRAINT IF EXISTS discount_reports_claim_shape_check;
ALTER TABLE discount_reports ADD CONSTRAINT discount_reports_claim_shape_check CHECK (
    claim_version = 1 AND ride_ended_at IS NOT NULL
    OR claim_version = 2
       AND vehicle_plate ~ '^[0-9]{7,10}$'
       AND trip_minutes BETWEEN 1 AND 600
       AND (subtotal_cents IS NOT NULL OR total_cents IS NOT NULL)
       AND charge_date IS NOT NULL
);

COMMENT ON COLUMN discount_reports.plan_evidence_r2_key IS
    'Historical. Plan screenshots were required 2026-10-06..07 and are no longer '
    'collected; the column is kept so cleanup_receipts can still delete images '
    'already stored. Never written by new claims.';
