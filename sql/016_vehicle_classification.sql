-- Vehicle model classification: in-app display name + accessibility-relevant
-- use type.
--
-- vehicle_model_name — Veo's own in-app display name for the physical
--   vehicle model, visually confirmed per vehicle_type_id (see
--   src/ingest.py _KNOWN_VEHICLE_TYPES): "Astro" (kick scooter), "Cosmo"
--   (throttle e-bike, no pedals), "Apollo" (two-person pedal e-bike,
--   seated, ~18mph), "Rover" (three-wheeled trike, seated).
--
--   NOTE: id=5 read "Cosmo" here between 2026-07-16 and 2026-07-29; it is
--   a Rover. See sql/064 for the reclassification and sql/063 for the
--   vehicle_type_id column that stops this from recurring.
--
--   NULL for vehicle_type_ids we haven't visually
--   confirmed yet.
--
-- vehicle_use_type — "sitting" | "standing": whether a rider sits or
--   stands to operate the vehicle. Deliberately tracked as its OWN
--   dimension, independent of form_factor (bicycle/scooter) — today every
--   bicycle happens to be "sitting" and every scooter "standing", but the
--   two are conceptually distinct (form_factor is Veo's GBFS vocabulary,
--   already known to be unreliable for at least one vehicle_type_id;
--   use_type is the operative accessibility distinction for compliance
--   purposes) and could diverge if a future vehicle class doesn't follow
--   the current pattern (e.g. a seated scooter).

ALTER TABLE raw_telemetry_points
    ADD COLUMN IF NOT EXISTS vehicle_use_type    TEXT,
    ADD COLUMN IF NOT EXISTS vehicle_model_name  TEXT;

CREATE INDEX IF NOT EXISTS idx_raw_vehicle_use_type
    ON raw_telemetry_points (vehicle_use_type);

ALTER TABLE device_state
    ADD COLUMN IF NOT EXISTS current_vehicle_use_type    TEXT,
    ADD COLUMN IF NOT EXISTS current_vehicle_model_name  TEXT;

ALTER TABLE device_history
    ADD COLUMN IF NOT EXISTS vehicle_use_type    TEXT,
    ADD COLUMN IF NOT EXISTS vehicle_model_name  TEXT;
