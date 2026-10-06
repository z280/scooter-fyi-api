-- Per-vehicle rental outcomes: the ledger behind the Smart Ride Grade.
--
-- A rental either takes the rider somewhere or it doesn't. Measured over
-- 214,846 reservation episodes across 8 days of archive, 9.1% never get 25 m
-- from the kerb — someone unlocked a scooter that wouldn't go, or that was
-- unrideable once they got on it. (is_reserved is IN USE on this feed, not a
-- held booking, so these are attempts rather than changes of mind.)
--
-- That outcome is the only reliability signal that has survived validation:
--
--   * it PERSISTS per vehicle — a vehicle's no-go rate in one week predicts
--     its rate the next at r=+0.275 over 7,534 vehicles, with the best
--     quartile landing at 6.9% and the worst at 11.0% against an 8.1% fleet
--     baseline;
--   * it is CONCENTRATED — the worst 10% of vehicles account for 32.4% of all
--     no-gos, and 123 vehicles fail 40%+ of the time;
--   * the shipped reliability_tier separates it, which is how we know the
--     tier is worth keeping: ok 7.4%, unknown 13.1%, high_risk 50.0%.
--
-- Cell-relative dwell was tested as a second factor and REJECTED: correcting
-- it so that a van collection censors rather than counts as demand halved its
-- persistence (r=+0.149 -> +0.074 on identical runs). It measures location
-- noise more than vehicle condition, and a coefficient that looks like
-- knowledge and behaves like noise is worse than no coefficient.
--
-- COUNTED AT THE SOURCE, not by scanning. sql/069 already freezes a vehicle's
-- position for the duration of a rental and computes the origin-to-drop-point
-- distance when it ends, so src/device_state.py knows the outcome of every
-- rental at the moment it completes. Two counters cost one UPDATE that is
-- already happening. Deriving this from raw_telemetry_points instead would be
-- impossible beyond ~48 hours, since archive_if_due truncates that table.

ALTER TABLE device_state
    ADD COLUMN IF NOT EXISTS rentals_observed INTEGER NOT NULL DEFAULT 0;

ALTER TABLE device_state
    ADD COLUMN IF NOT EXISTS rentals_no_go INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN device_state.rentals_observed IS
    'Rentals seen to completion since sql/072. Counts up from zero — a vehicle '
    'with few observations has no grade rather than a flattering one.';

COMMENT ON COLUMN device_state.rentals_no_go IS
    'Of those, how many ended within stationary_threshold_meters of where the '
    'rider unlocked it: the scooter did not go. Fleet baseline ~9%.';

-- Both counters are read together, on every device, for the map payload.
CREATE INDEX IF NOT EXISTS idx_device_state_rental_outcomes
    ON device_state (rentals_observed)
    WHERE rentals_observed > 0;
