-- Recurring supporter subscriptions (API_REQUIREMENTS.md §4.1 update:
-- single fixed-price monthly plan with a 30-day free trial, replacing the
-- one-time pay-what-you-want Payment Link as the primary supporter path).
--
-- One row per Stripe Subscription. accounts.supporter becomes TRUE iff
-- EITHER an unrefunded one-time payment exists (supporter_payments, kept
-- for anyone who already supported that way) OR a subscription row here
-- has status IN ('trialing', 'active') — a trialing rider is a supporter
-- for the trial's duration, no payment required yet.
--
-- account_id is nullable: Stripe can deliver customer.subscription.created
-- before checkout.session.completed (event ordering isn't guaranteed), and
-- only the checkout session carries client_reference_id (our account id).
-- The webhook upserts by stripe_subscription_id from whichever event
-- arrives first, then fills in account_id when checkout.session.completed
-- lands. A row with account_id IS NULL is not yet supporter-eligible.
CREATE TABLE IF NOT EXISTS supporter_subscriptions (
    id                      BIGSERIAL PRIMARY KEY,
    account_id              BIGINT REFERENCES accounts(id) ON DELETE CASCADE,
    stripe_subscription_id  TEXT NOT NULL UNIQUE,
    stripe_customer_id      TEXT NOT NULL,
    status                  TEXT,
    trial_end               TIMESTAMPTZ,
    current_period_end      TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    canceled_at             TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_supporter_subscriptions_account
    ON supporter_subscriptions (account_id);
CREATE INDEX IF NOT EXISTS idx_supporter_subscriptions_customer
    ON supporter_subscriptions (stripe_customer_id);
