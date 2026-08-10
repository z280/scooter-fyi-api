-- Which post-release sample supplied soc_end.
--
-- sql/070 took the end-of-ride battery from the FIRST available sample after
-- the reservation cleared. That is the right sample for the DROP POINT — where
-- and when the vehicle became available — but the wrong one for the battery: a
-- pack just off load reads low and recovers over the next few minutes.
--
-- Measured on 1,854 episodes across 6 days, holding distance, elevation and
-- temperature fixed and varying only which post-release sample supplies
-- soc_end:
--
--   post   ~min   mean burn   intercept   pp/km    R2
--      1      2      12.102       8.930   0.800   0.207   <- sql/070
--      2      4       9.189       5.357   1.016   0.354   <- now
--      3      6       8.742       4.641   0.996   0.344
--      6     12       8.267       4.146   1.058   0.275
--      8     16       7.925       4.112   1.031   0.222
--
-- One extra cycle moves the intercept 8.93 -> 5.36 pp, lifts the distance
-- coefficient by 27% (0.800 -> 1.016 pp/km) and R2 by 71%. Waiting longer buys
-- almost nothing and then makes it worse — R2 peaks at post[2] and declines,
-- because a longer wait lets self-discharge and charging bleed into a number
-- that is supposed to describe one ride.
--
-- This was originally checked and dismissed: an earlier comparison found the
-- MEDIAN under-read to be 0 metres at every offset. It is — most readings are
-- already settled, and the effect lives in a large minority. The median was
-- the wrong statistic for a question about a mean-shifting bias.
--
-- post[2] exists for 96.1% of episodes. The run of available samples is
-- partitioned by episode, so post[2] can never belong to a LATER rental (a
-- re-reservation opens a new episode). The remaining 3.9% fall back to post[1]
-- and carry a burn biased ~3 pp high, so they are marked rather than mixed in
-- silently — the same argument as sql/070's waypoint_count.

ALTER TABLE battery_trip_observations
    ADD COLUMN IF NOT EXISTS soc_end_offset_cycles SMALLINT;

COMMENT ON COLUMN battery_trip_observations.soc_end_offset_cycles IS
    'How many ingest cycles after the vehicle first became available the '
    'soc_end reading was taken. 1 = the settled reading (normal). 0 = the '
    'first available sample, used only when no second one existed (~3.9%); '
    'those rows read ~3 percentage points high. NULL = written before '
    'sql/071, when every row used the unsettled first sample.';

-- Rows written before this migration all used the unsettled reading and are
-- NOT rewritten here: soc_end cannot be recovered from a stored row, only by
-- re-mining the archive. They stay NULL, which is what makes them
-- distinguishable, and train() can exclude them once enough settled
-- observations exist to fit on.
