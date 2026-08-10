-- Rental-aware trip detection: one rental, one trip.
--
-- src/device_state.py detects a "trip" as a MOVED transition — the device
-- is further than stationary_threshold_meters from where we last saw it,
-- which for a dockless fleet was taken to mean somebody rode it there.
-- That inference holds only if a rented vehicle is INVISIBLE while it is
-- being ridden, so the next observation is the drop point.
--
-- It isn't. Veo keeps a rented vehicle in free_bike_status for the whole
-- rental, sampled every 2 minutes, broadcasting its live moving position,
-- with is_reserved true (see sql/../src/ride_watch.py and the correction
-- note in API_REQUIREMENTS.md). So every 2-minute sample of a moving
-- rental cleared the threshold and appended its own trip_events row: one
-- rental became ~10 "trips", and one stop in device_history fragmented
-- into ~10 two-minute stops, which is what dwell_stats reads.
--
-- Measured on 2026-08-09: 187,820 MOVED steps over 16 m, of which 161,160
-- (86%) fall inside a reservation episode, against 30,566 reservation
-- episodes (median 6 min, mean 10 min, 70% between 4 and 40 min — a
-- credible rental-duration profile). Roughly a 6x over-count.
--
-- The fix needs one bit of memory across cycles: "this vehicle was already
-- in a rental when we last saw it". device_state is that per-vehicle
-- memory, so the flag goes here. While it is set, src/device_state.py
-- freezes the vehicle's stored position instead of chasing it, and the
-- rental collapses into a single MOVED — origin to drop point — the moment
-- is_reserved clears. That is exactly the behaviour the MOVED branch was
-- written for; it just never got the chance to happen.
--
-- NULL means "not in a rental", which is the correct reading for every
-- existing row: any vehicle mid-rental right now simply starts its
-- bookkeeping at the next cycle that observes it reserved.
--
-- HISTORICAL trip_events / device_history rows are deliberately NOT
-- rewritten here. The raw telemetry needed to do it is in the R2 archive
-- (raw/YYYY/MM/DD/, is_reserved included, back to 2026-06-30), so a
-- recompute is possible — but it restates already-published trip counts
-- and compliance figures, and that is a decision, not a migration.

ALTER TABLE device_state
    ADD COLUMN IF NOT EXISTS rental_started_at TIMESTAMPTZ;

COMMENT ON COLUMN device_state.rental_started_at IS
    'Set to the snapshot_time of the first cycle that observed this vehicle '
    'with is_reserved true; NULL when the vehicle is not in a rental. While '
    'set, src/device_state.py freezes current_lat/current_lon so the rental '
    'produces exactly one MOVED (and one trip_events row) on release rather '
    'than one per 2-minute sample.';

-- Every open trip is a rental in progress, so the index only ever holds a
-- few thousand rows out of ~7k devices, and only the partial-index form is
-- worth having.
CREATE INDEX IF NOT EXISTS idx_device_state_in_rental
    ON device_state (rental_started_at)
    WHERE rental_started_at IS NOT NULL;
