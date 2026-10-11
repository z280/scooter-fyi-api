-- Reinstating a report gets the same two-capacity audit a resolution has.
--
-- sql/102 gave device_reports `reinstated_by_login`, because reinstatement
-- only existed on the /admin pages, whose GitHub-OAuth session has no rider
-- account. The in-app admin console reinstates through
-- /api/v1/private/reports/{id}/reinstate, and that session is an ACCOUNT —
-- so there has to be somewhere to put it. Mirrors
-- device_reports.resolved_by / resolved_by_login exactly: the account when an
-- admin acted through the app, the login when they acted through the portal,
-- and never an email copy (ON DELETE SET NULL, so the trail follows the
-- account and leaves with it).
--
-- REPLAY SAFETY: IF NOT EXISTS, like every file in sql/.
ALTER TABLE device_reports
    ADD COLUMN IF NOT EXISTS reinstated_by BIGINT
        REFERENCES accounts(id) ON DELETE SET NULL;
