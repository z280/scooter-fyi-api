-- Defensive de-dup of accounts.email — idempotent, expected no-op.
--
-- accounts.email has carried a UNIQUE constraint since sql/012, and every
-- write path normalizes via accounts.normalize_email() (strip + lowercase)
-- before insert/lookup, so true duplicates should not exist. This guards
-- against any row written outside that path (a manual INSERT, a pre-012
-- import, etc.) now that sql/025 has relaxed email off NOT NULL and given
-- phone_number a reason to coexist with it.
--
-- Tie-break: keep the row with the most recent last_login_at (NULL loses
-- to any non-null value; ties — including both NULL — keep the lowest id,
-- i.e. the original/oldest row). Losers are hard-deleted. accounts.id has
-- no ON DELETE RESTRICT/NO ACTION FK anywhere in the schema —
-- auth_sessions (sql/012), device_reports/discount_reports (sql/013),
-- supporter_payments/rides (sql/014), and supporter_subscriptions
-- (sql/019) are all ON DELETE CASCADE or ON DELETE SET NULL — so a plain
-- DELETE FROM accounts needs no additional manual cleanup.
DO $$
DECLARE
    dupe RECORD;
BEGIN
    FOR dupe IN
        SELECT LOWER(TRIM(email)) AS norm_email
        FROM accounts
        WHERE email IS NOT NULL
        GROUP BY LOWER(TRIM(email))
        HAVING COUNT(*) > 1
    LOOP
        DELETE FROM accounts
        WHERE LOWER(TRIM(email)) = dupe.norm_email
          AND id NOT IN (
              SELECT id FROM accounts
              WHERE LOWER(TRIM(email)) = dupe.norm_email
              ORDER BY last_login_at DESC NULLS LAST, id ASC
              LIMIT 1
          );
    END LOOP;
END $$;
