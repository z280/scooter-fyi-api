-- Per-cycle range rankings on each device row. All 7 fields are TEXT
-- because the ranking format is "x/y" or one of the quartile labels
-- ("0","25","50","75").
--
-- See src/ranking.py for the computation. Tie semantics: when multiple
-- devices share a range value, they all receive the rank of the LAST
-- (highest-range-ward) member of the tie group — i.e. 20 devices tied
-- for the highest range in a fleet of 100 all show "100/100".

ALTER TABLE raw_telemetry_points
    ADD COLUMN IF NOT EXISTS range_percentile_by_type      TEXT,
    ADD COLUMN IF NOT EXISTS range_rank_unique_by_type     TEXT,
    ADD COLUMN IF NOT EXISTS range_rank_all_by_type        TEXT,
    ADD COLUMN IF NOT EXISTS range_rank_all_devices        TEXT,
    ADD COLUMN IF NOT EXISTS range_rank_h3_8_peers         TEXT,
    ADD COLUMN IF NOT EXISTS range_rank_h3_9_peers         TEXT,
    ADD COLUMN IF NOT EXISTS range_rank_h3_10_peers        TEXT;
