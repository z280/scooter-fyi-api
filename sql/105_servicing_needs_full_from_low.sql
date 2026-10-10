-- Servicing = reading FULL after reading <= 50% parked (sql/104 corrected,
-- 2026-10-10). sql/104 stamped device_state.last_serviced_at on any rise of
-- 5% of a full charge between two readings. The feed's range sags under load
-- during a ride and climbs back for minutes afterwards (by up to ~25% of a
-- full charge), so that fired on most rides: 46 vehicles in its first cycle,
-- one of them a real swap. src/device_state.py now requires a parked reading
-- at full (95%) after a parked low at or below 50% since the last full one;
-- range_low_since_full carries that low.
--
-- Every stamp written under sql/104's rule is cleared (the one real swap
-- among them is lost; the next one is recorded correctly). None of them had
-- cleared a report: the only standing report (#44) still read high risk.
--
-- REPLAY SAFETY: re-executed by the _pg fixtures before each test writes its
-- own rows; IF NOT EXISTS, and the seed only touches NULLs.
ALTER TABLE device_state ADD COLUMN IF NOT EXISTS range_low_since_full INTEGER;

UPDATE device_state SET last_serviced_at = NULL WHERE last_serviced_at IS NOT NULL;

UPDATE device_state SET range_low_since_full = last_range_meters
 WHERE range_low_since_full IS NULL AND last_range_meters IS NOT NULL;
