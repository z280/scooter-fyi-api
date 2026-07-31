-- Regional (entire-database) leaderboard, alongside sql/048's per-r8-cell
-- report. Clarified by @zNeill on scooter-fyi-api#37 (the deferred
-- area_leaders.py review finding): the per-cell report already answers
-- "for each hexagon where points were earned, who leads it" correctly
-- (no spatial_status filter was ever needed -- the universe is already
-- exactly "cells that have observed points/devices"). What was actually
-- missing is a SECOND dashboard: one ranked leaderboard across the whole
-- database, not split by hex.
--
-- Computed in the SAME transaction, SAME trailing-28-day window, as
-- h3_r8_area_report/h3_r8_area_leaders (src/area_leaders.py:recompute) --
-- both dashboards always describe the same run. There is no separate runs
-- table for this one; read it against h3_r8_area_leader_runs' latest row,
-- same as h3_r8_area_leaders does.
--
-- FULL-REPLACE each recompute, same idiom as h3_r8_area_leaders. Top
-- MAX_REGIONAL_LEADERS (25, src/area_leaders.py) overall, not just a
-- top-3 -- a whole-database dashboard is exactly the place riders expect
-- a real leaderboard, not a 3-entry podium.

CREATE TABLE IF NOT EXISTS regional_leaders (
    rank            SMALLINT NOT NULL PRIMARY KEY CHECK (rank BETWEEN 1 AND 25),
    account_id      BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    points          INTEGER NOT NULL CHECK (points > 0),
    first_point_at  TIMESTAMPTZ NOT NULL   -- tie-break provenance, same rule as h3_r8_area_leaders
);
