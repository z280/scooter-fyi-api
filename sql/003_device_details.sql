-- Additional per-device fields captured from the free_bike_status payload
-- and the vehicle_types lookup. These were already present in every GBFS
-- response we fetch; we just weren't keeping them.
--
-- IDENTIFIER MODEL ----------------------------------------------------------
-- vehicle_plate      — raw visible plate number printed on the scooter (e.g.
--                      "1025543"), extracted from rental_uris. INTERNAL ONLY.
--                      Never returned by an unauthenticated endpoint.
-- vehicle_identifier — sha256(vehicle_plate)[:16]. Deterministic, unsalted
--                      so anyone who already has access to Veo's public GBFS
--                      feed can rederive the mapping themselves. Safe to
--                      expose publicly. This is the stable cross-cycle ID
--                      that the public API surfaces (the rotating GBFS
--                      bike_id is NOT stable).

ALTER TABLE raw_telemetry_points
    ADD COLUMN IF NOT EXISTS vehicle_plate         TEXT,
    ADD COLUMN IF NOT EXISTS vehicle_identifier    TEXT,
    ADD COLUMN IF NOT EXISTS is_disabled           BOOLEAN,
    ADD COLUMN IF NOT EXISTS is_reserved           BOOLEAN,
    ADD COLUMN IF NOT EXISTS current_range_meters  INTEGER,
    ADD COLUMN IF NOT EXISTS propulsion_type       TEXT;

CREATE INDEX IF NOT EXISTS idx_raw_vehicle_identifier
    ON raw_telemetry_points (vehicle_identifier);
