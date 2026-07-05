-- Supporter payments + ride history (API_REQUIREMENTS.md §4).
--
-- supporter_payments: one row per completed Stripe Checkout (Payment Link,
-- pay-what-you-want). accounts.supporter is derived: TRUE iff the account
-- has >= 1 non-refunded payment. stripe_session_id is NOT NULL + UNIQUE so
-- webhook retries are idempotent — Postgres allows multiple NULLs in a
-- UNIQUE column, which would silently defeat ON CONFLICT DO NOTHING for
-- any malformed event; the app layer also refuses to insert without one
-- (src/stripe_webhook.py), this is the schema-level backstop.
--
-- rides: supporter-logged ride history. Route polylines are the most
-- sensitive data this system holds — DELETE endpoints are HARD deletes,
-- and nothing else in the codebase may read this table for analytics
-- (privacy commitment; see /api/v1/meta/privacy).

CREATE TABLE IF NOT EXISTS supporter_payments (
    id                     BIGSERIAL PRIMARY KEY,
    account_id             BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    stripe_session_id      TEXT NOT NULL UNIQUE,
    stripe_payment_intent  TEXT,
    amount_cents           INTEGER,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    refunded_at            TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_supporter_payments_intent
    ON supporter_payments (stripe_payment_intent);
CREATE INDEX IF NOT EXISTS idx_supporter_payments_account
    ON supporter_payments (account_id);

CREATE TABLE IF NOT EXISTS rides (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id       BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at       TIMESTAMPTZ NOT NULL,
    ended_at         TIMESTAMPTZ NOT NULL,
    duration_s       INTEGER NOT NULL CHECK (duration_s >= 0),
    distance_m       INTEGER NOT NULL CHECK (distance_m >= 0),
    est_cost_cents   INTEGER CHECK (est_cost_cents >= 0),
    rate_plan        TEXT NOT NULL CHECK (rate_plan IN ('resident', 'visitor', 'equity')),
    started_in_zone  BOOLEAN NOT NULL,
    ended_in_zone    BOOLEAN NOT NULL,
    polyline         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rides_account_started
    ON rides (account_id, started_at DESC);
