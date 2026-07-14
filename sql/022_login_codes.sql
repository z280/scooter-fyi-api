-- Email verification codes — a short AA000AA code the user TYPES to sign in
-- (vs the magic link they click). Parallel to magic_link_tokens, but the
-- code is low-entropy, so verification is scoped by email and hard-limited:
--   * code_hash is HMAC-SHA256(server secret, "email:CODE") — see
--     api_auth._hash_code — so a leaked table can't be brute-forced without
--     the secret, and the hash is bound to the email.
--   * `attempts` caps ONLINE guessing; the code is burned after too many
--     wrong tries or on first success (used_at set).
--   * only the newest code per email is live (issuing a new one burns the
--     prior unused ones).
CREATE TABLE IF NOT EXISTS login_codes (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT NOT NULL,
    code_hash   TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    attempts    INTEGER NOT NULL DEFAULT 0,
    request_ip  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_login_codes_email_live
    ON login_codes (email, created_at DESC)
    WHERE used_at IS NULL;
