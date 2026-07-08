-- Admin allowlist — the source of truth for which account emails are
-- authorized for the admin surface (the /api/v1/private/* endpoints and the
-- admin-only fields on /api/v1/user/devices/current). Managed from the
-- GitHub-gated admin portal (/admin/admins) and `python -m src.cli admin`.
-- Replaces the ADMIN_EMAILS environment variable as the source of truth.
--
-- Emails are stored normalized (trimmed + lowercased — see
-- accounts.normalize_email). `added_by` records who added the entry: a
-- GitHub login from the portal, or 'cli' from the command line.
CREATE TABLE IF NOT EXISTS admin_allowlist (
    email     TEXT PRIMARY KEY,
    added_by  TEXT,
    added_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
