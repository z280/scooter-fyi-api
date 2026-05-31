-- H3 hexagonal cell indexes at resolutions 8 / 9 / 10 for every device
-- observation. Computed at ingest from lat/lon; stable across cycles for a
-- stationary device, changes by ride distance for a moving one.
--
-- Why three resolutions:
--   res  8: ~0.74 km² hexagons — neighborhood-scale aggregation
--   res  9: ~0.10 km² hexagons — block-scale (~210m edge)
--   res 10: ~0.015 km² hexagons — corner-scale (~75m edge); chosen as the
--           privacy granularity for "same spot" comparisons (matches
--           the negative-report locality model)
--
-- H3 v4 cell IDs are 64-bit values; in practice (resolutions 0–15 over
-- normal cells) the mode bits never push the value over 2^63, so signed
-- BIGINT is safe. If we ever start emitting non-cell modes (edges,
-- vertices), revisit.

ALTER TABLE raw_telemetry_points
    ADD COLUMN IF NOT EXISTS h3_8_index   BIGINT,
    ADD COLUMN IF NOT EXISTS h3_9_index   BIGINT,
    ADD COLUMN IF NOT EXISTS h3_10_index  BIGINT;

CREATE INDEX IF NOT EXISTS idx_raw_h3_8  ON raw_telemetry_points (h3_8_index);
CREATE INDEX IF NOT EXISTS idx_raw_h3_9  ON raw_telemetry_points (h3_9_index);
CREATE INDEX IF NOT EXISTS idx_raw_h3_10 ON raw_telemetry_points (h3_10_index);

ALTER TABLE device_state
    ADD COLUMN IF NOT EXISTS current_h3_8_index   BIGINT,
    ADD COLUMN IF NOT EXISTS current_h3_9_index   BIGINT,
    ADD COLUMN IF NOT EXISTS current_h3_10_index  BIGINT;

CREATE INDEX IF NOT EXISTS idx_device_state_h3_10
    ON device_state (current_h3_10_index);

ALTER TABLE device_history
    ADD COLUMN IF NOT EXISTS h3_8_index   BIGINT,
    ADD COLUMN IF NOT EXISTS h3_9_index   BIGINT,
    ADD COLUMN IF NOT EXISTS h3_10_index  BIGINT;

CREATE INDEX IF NOT EXISTS idx_device_history_h3_10
    ON device_history (h3_10_index);
