-- Decommercialize. There is no paid tier, no supporter status, and no
-- Stripe integration. The only gates in this system are "signed in" and
-- "on the admin allowlist".
--
-- Support for the project, when it exists, will come from merchandise or a
-- direct donation with NO in-app incentive attached — nothing to model
-- here, and deliberately nothing to gate on. A donation link needs no
-- backend at all.
--
-- Verified before writing this migration: 0 rows in supporter_payments,
-- 0 accounts with supporter = true. Nothing of value is discarded. On an
-- instance whose history you don't know, check first:
--
--     SELECT count(*) FROM supporter_payments;
--     SELECT count(*) FROM accounts WHERE supporter;

-- One row per completed Stripe Checkout (sql/014). The webhook that wrote
-- it (src/stripe_webhook.py) and its /webhooks/stripe endpoint are gone.
DROP TABLE IF EXISTS supporter_payments;

-- Recurring-donation subscriptions (sql/019). Same story.
DROP TABLE IF EXISTS supporter_subscriptions;

-- Derived supporter state on the account itself.
ALTER TABLE accounts DROP COLUMN IF EXISTS supporter;
ALTER TABLE accounts DROP COLUMN IF EXISTS supporter_amount_cents;
ALTER TABLE accounts DROP COLUMN IF EXISTS supporter_since;
ALTER TABLE accounts DROP COLUMN IF EXISTS last_donation_at;
ALTER TABLE accounts DROP COLUMN IF EXISTS stripe_customer_id;
