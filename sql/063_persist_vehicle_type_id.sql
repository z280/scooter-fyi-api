-- Persist Veo's raw `vehicle_type_id`.
--
-- WHY: the id is the primary key of Veo's own product catalogue and it is
-- what src/ingest.py._KNOWN_VEHICLE_TYPES corrects against, but until now
-- ingest computed it (TaggedDevice.vehicle_type_id) and then dropped it on
-- the floor — no table stored it. Everything downstream only ever saw the
-- *derived* trio (form_factor / vehicle_use_type / vehicle_model_name).
--
-- The cost of that showed up on 2026-07-29, when id=5 turned out to be a
-- trike rather than the Cosmo it had been labelled since 2026-07-16 (see
-- sql/064). Because the derived trio for id=3 (a real Cosmo) and id=5 were
-- byte-identical — bicycle / sitting / "Cosmo", same 67000m junk max-range —
-- the question "which of our vehicles are id=5?" was UNANSWERABLE from our
-- own database. It had to be re-derived by re-fetching Veo's live feed and
-- matching on plate, which only sees vehicles that are *currently* in the
-- feed; anything retired in the meantime stays mislabelled forever.
--
-- Storing the id makes any future reclassification a one-line UPDATE keyed
-- on the id itself, over the full history, with no dependency on the live
-- feed still listing the vehicle.
--
-- TEXT, not an integer: GBFS defines vehicle_type_id as an opaque string
-- (Veo happens to use "0".."5" today) and ingest already carries it as str.
-- Nullable: pre-existing rows genuinely don't know, and a device can reach
-- ingest with no vehicle_type_id at all.

ALTER TABLE raw_telemetry_points
    ADD COLUMN IF NOT EXISTS vehicle_type_id TEXT;

ALTER TABLE device_state
    ADD COLUMN IF NOT EXISTS current_vehicle_type_id TEXT;

-- device_state is one row per physical vehicle (~8.5k), so this index is
-- cheap and makes "the whole id=N cohort" a direct lookup. Deliberately NOT
-- added to raw_telemetry_points: that table is in the tens of millions of
-- rows, is pruned to R2 on a 24h horizon anyway, and is queried by
-- snapshot_time / vehicle_identifier rather than by type.
CREATE INDEX IF NOT EXISTS idx_device_state_vehicle_type_id
    ON device_state (current_vehicle_type_id);
