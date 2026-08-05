-- Supporting index for GET /api/v1/leaderboard/regional/live
-- (src/api_leaderboard.py:leaderboard_regional_live).
--
-- Unlike its precomputed sibling GET /api/v1/leaderboard/regional -- which
-- reads sql/054's 25-row `regional_leaders` table written by the nightly
-- src/area_leaders.py:recompute -- the live endpoint aggregates the ledger
-- itself on every request: "who has earned how many confirmed points in the
-- trailing window, right now", with no wait for tomorrow's 09:15 run. That
-- query is
--
--     SELECT account_id, SUM(points), MIN(created_at)
--     FROM user_points
--     WHERE status = 'confirmed' AND created_at >= <window start>
--     GROUP BY account_id
--
-- and sql/028's existing indexes cannot serve it: idx_user_points_account
-- leads with account_id (no help for a time range across ALL accounts) and
-- idx_user_points_h3_8 is spatial. Without this index the endpoint is a seq
-- scan of the whole ledger per request.
--
-- Partial on `status = 'confirmed'` because that is the only status this
-- report (and sql/048's) ever counts -- the same rule area_leaders.py
-- applies -- so the 'pending_review' rows a future moderation workflow may
-- add never enter the index. account_id rides along as a second key so the
-- grouping can be satisfied from the index alone.
CREATE INDEX IF NOT EXISTS idx_user_points_confirmed_created
    ON user_points (created_at DESC, account_id)
    WHERE status = 'confirmed';
