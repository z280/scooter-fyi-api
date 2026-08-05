-- Territory control goes live: the nightly job keeps only the half that has
-- to be nightly, and everything derived from the points ledger moves to read
-- time.
--
-- sql/048 split one job across two very different costs. The UNIVERSE — every
-- r8 cell that has ever had an observed device or a point, ALL-TIME — needs
-- `SELECT DISTINCT h3_8_index FROM device_history` over ~7.3M unindexed rows,
-- "a seq scan of a few seconds, once a day, off-peak" (src/area_leaders.py).
-- The LEADERS need only the trailing 28 days of `user_points`, which sql/059
-- already indexes for exactly this shape.
--
-- Storing the leaders bought nothing and cost the thing riders actually
-- wanted: territory could not change until 09:15 the next morning, so nobody
-- could watch themselves take a hexagon. Computing them per request costs one
-- indexed range scan, and it is the SAME scan the regional leaderboard was
-- already paying for on its own.
--
-- So the derived tables lose their writer, and this migration drops them
-- rather than leaving stale copies for someone to read by mistake. Nothing
-- here is a source of truth: every row these tables held is reconstructible
-- from `user_points`, which is the append-only ledger and is untouched.
--
--   h3_r8_area_leaders  -- per-cell top 3; now ranked at read time
--   regional_leaders    -- sql/054's whole-database top 25; likewise
--
-- `h3_r8_area_report` survives as what it always really was: the cell
-- universe. Its two derived counters go the same way as the tables above --
-- `total_points`/`distinct_earners` are a 28-day window's facts, and a window
-- is a read-time idea, not something a nightly run can pin down.
--
-- `h3_r8_area_leader_runs` stays an append-only audit log of universe
-- refreshes, but `window_start`/`window_end`/`led_cells` described the leader
-- half that no longer runs here. A universe refresh has no window and leads
-- no cells; keeping the columns would mean writing three numbers that mean
-- nothing.

DROP TABLE IF EXISTS h3_r8_area_leaders;
DROP TABLE IF EXISTS regional_leaders;

ALTER TABLE h3_r8_area_report DROP COLUMN IF EXISTS total_points;
ALTER TABLE h3_r8_area_report DROP COLUMN IF EXISTS distinct_earners;

ALTER TABLE h3_r8_area_leader_runs DROP COLUMN IF EXISTS window_start;
ALTER TABLE h3_r8_area_leader_runs DROP COLUMN IF EXISTS window_end;
ALTER TABLE h3_r8_area_leader_runs DROP COLUMN IF EXISTS led_cells;

-- sql/059's index, widened by one INCLUDE column. It was built for the
-- regional tally, which groups by account alone; the per-cell read groups by
-- (h3_8_index, account_id) over the same rows, so without `h3_8_index` in the
-- index that query would go to the heap for every row in the window and undo
-- exactly what sql/059's INCLUDE was added to fix. One index now covers both
-- reads -- they are the same scan, differing only in how far they group.
DROP INDEX IF EXISTS idx_user_points_confirmed_created;
CREATE INDEX IF NOT EXISTS idx_user_points_confirmed_created
    ON user_points (created_at DESC, account_id)
    INCLUDE (points, h3_8_index)
    WHERE status = 'confirmed';
