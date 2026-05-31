-- Bearer tokens minted by the map-auth flow (src/map_auth.py).
--
-- A token is a 32-byte random value, returned to the user once via URL
-- fragment (#token=...) and discarded by the server immediately afterward.
-- Only the sha256 of the raw token is stored, so a database leak does not
-- expose live tokens — an attacker would still need to find a preimage.
--
-- Tokens have an 8h TTL by default (config.map_auth.token_ttl_hours).

CREATE TABLE IF NOT EXISTS api_tokens (
    token_sha256       TEXT PRIMARY KEY,           -- sha256(raw_token), hex
    github_login       TEXT NOT NULL,
    github_user_id     BIGINT,
    github_orgs        TEXT[] NOT NULL DEFAULT '{}',
    issued_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at         TIMESTAMPTZ NOT NULL,
    revoked_at         TIMESTAMPTZ,
    last_used_at       TIMESTAMPTZ,
    issued_ip          INET,
    user_agent         TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_tokens_login ON api_tokens (github_login);
CREATE INDEX IF NOT EXISTS idx_api_tokens_expires_at ON api_tokens (expires_at);
