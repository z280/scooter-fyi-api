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
-- add never enter the index.
--
-- `created_at` leads because the WHERE clause is the range; account_id is
-- the second key (the GROUP BY); and `points` is INCLUDEd rather than left
-- in the heap. INCLUDE is what makes the index actually cover this query:
-- the aggregate is SUM(points), so an index carrying only (created_at,
-- account_id) would still send Postgres to the heap for every row in the
-- window just to read the value being summed. `points` is a 4-byte INTEGER
-- and is never a search key here, so it costs one word per row and buys the
-- heap fetches back. (An index-only scan additionally wants the visibility
-- map to be current for the pages involved -- true in practice for this
-- table, which is append-only and never updated after insert, so pages go
-- all-visible on the first autovacuum and stay that way.)
CREATE INDEX IF NOT EXISTS idx_user_points_confirmed_created
    ON user_points (created_at DESC, account_id)
    INCLUDE (points)
    WHERE status = 'confirmed';
