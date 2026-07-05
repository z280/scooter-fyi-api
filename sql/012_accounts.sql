-- Accounts + bearer sessions + magic-link tokens + rate-limit event log.
-- API_REQUIREMENTS.md §2 (accounts & sessions) and §5 (rate limiting).
--
-- Two sign-in doors (Google ID token, Postmark magic link), one session
-- model. Sessions are opaque bearer tokens stored ONLY as sha256 hashes —
-- same convention as api_tokens (sql/005). Scopes live on the session row;
-- the `supporter` scope is NOT stored (derived from accounts.supporter at
-- read time, so a Stripe webhook flip applies to live sessions instantly).

CREATE TABLE IF NOT EXISTS accounts (
    id             BIGSERIAL PRIMARY KEY,
    email          TEXT NOT NULL UNIQUE,          -- stored lowercased
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at  TIMESTAMPTZ,

    -- §2.4 profile — client-writable through PUT /api/v1/profile
    rate_plan      TEXT NOT NULL DEFAULT 'visitor'
                   CHECK (rate_plan IN ('resident', 'visitor', 'equity')),
    theme          TEXT,
    favorites      JSONB NOT NULL DEFAULT '[]',

    -- §4.1 supporter — written ONLY by the Stripe webhook handler
    supporter               BOOLEAN NOT NULL DEFAULT FALSE,
    supporter_amount_cents  INTEGER,
    supporter_since         TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_sha256  TEXT PRIMARY KEY,
    account_id    BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    scopes        TEXT[] NOT NULL DEFAULT '{rider}',
    method        TEXT NOT NULL CHECK (method IN ('google', 'magic_link')),
    -- Rider sessions slide (each refresh re-extends 30 days); admin sessions
    -- have a fixed 24h expiry — refresh rotates the token but never extends.
    sliding       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    issued_ip     INET,
    user_agent    TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_account
    ON auth_sessions (account_id);
-- Expired/revoked rows are pruned opportunistically on refresh/signout.

CREATE TABLE IF NOT EXISTS magic_link_tokens (
    token_sha256  TEXT PRIMARY KEY,
    email         TEXT NOT NULL,                  -- lowercased
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,           -- created_at + 15 min
    used_at       TIMESTAMPTZ,                    -- single-use: set on redeem
    request_ip    INET
);

CREATE INDEX IF NOT EXISTS idx_magic_link_tokens_email
    ON magic_link_tokens (email, created_at DESC);

-- Fixed-window rate limiting (§5). One row per counted event; windows are
-- computed by COUNT(*) over (bucket, key, at >= now - window). Rows older
-- than the longest window in use are deleted opportunistically on insert.
CREATE TABLE IF NOT EXISTS rate_limit_events (
    id      BIGSERIAL PRIMARY KEY,
    bucket  TEXT NOT NULL,
    key     TEXT NOT NULL,
    at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rate_limit_events_lookup
    ON rate_limit_events (bucket, key, at DESC);
