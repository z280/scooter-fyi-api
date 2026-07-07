-- Retire the GitHub map-auth flow (API_REQUIREMENTS.md §2.5).
--
-- api_tokens held the bearer tokens minted by the GitHub OAuth "elevated
-- map" flow (src/map_auth.py) and verified by require_map_user
-- (src/map_auth_dep.py). Both are deleted; the /api/v1/private/* endpoints
-- they gated now require the Google `admin` session scope (require_admin in
-- src/accounts.py), and the /admin "Map tokens" management view is gone.
-- Nothing reads or writes api_tokens anymore, so drop it.
--
-- The tokens themselves were ephemeral (8h TTL) and re-mintable in the old
-- world, so there is nothing here worth preserving.

DROP TABLE IF EXISTS api_tokens;
