-- Track the maximum current_range_meters ever observed per physical bike.
--
-- WHY -----------------------------------------------------------------------
-- Veo's Denver fleet mixes two bike form factors that the public GBFS feed
-- does not distinguish: the lighter pedal-less e-bike and the heavier
-- pedal+2nd-seat e-bike with a noticeably larger battery. Both report under
-- form_factor="bicycle"; vehicle_type_id rotates with the bike_id rotation
-- and is unreliable for typing. The only signal left is the per-device
-- charge level — across many cycles, the pedal/2nd-seat bikes will reach
-- meaningfully higher current_range_meters than the smaller-battery model.
--
-- This migration adds a soak-tracking column on device_state so we can let
-- the system observe for a few days and then classify each bike from the
-- highest charge level it ever reported.

ALTER TABLE device_state
    ADD COLUMN IF NOT EXISTS max_observed_range_meters  INTEGER,
    ADD COLUMN IF NOT EXISTS max_observed_range_at      TIMESTAMPTZ;

-- Includes vehicle_identifier as the tie-breaker so the ranking endpoint's
-- ORDER BY (max_observed_range_meters DESC, vehicle_identifier) is fully
-- satisfied by an index scan — no extra sort step even at the 20k LIMIT cap.
CREATE INDEX IF NOT EXISTS idx_device_state_max_observed_range
    ON device_state (max_observed_range_meters DESC NULLS LAST, vehicle_identifier);
