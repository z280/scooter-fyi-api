-- Reclassify vehicle_type_id=5 from "Cosmo" to "Rover" across stored history.
--
-- "Rover" is Veo's name for the vehicle. It was carried as "Trike" while this
-- change was in progress -- descriptively accurate (it is a three-wheeled
-- seated trike) but not what the product is called, and this column holds
-- product names: Astro, Cosmo, Apollo. Nothing ever shipped reading "Trike",
-- so there is no second label to clean up -- the guard below is on 'Cosmo',
-- which is what is actually on disk.
--
-- id=5 was labelled "Cosmo" from 2026-07-16 until 2026-07-29, when plate
-- 1036661 was field-confirmed to be a three-wheeled seated trike (a Rover).
-- See
-- src/ingest.py._KNOWN_VEHICLE_TYPES for the full reasoning and the
-- corroborating pricing_plan_id evidence. Ingest now emits "Rover" for id=5,
-- so this migration only has to fix what is already on disk.
--
-- WHY A MIGRATION IS REQUIRED (device_state does not self-heal):
-- src/device_state.py only rewrites current_vehicle_model_name on the NEW,
-- MOVED and FAILED_START paths. The STATIONARY path — same spot, same
-- bike_id — deliberately touches almost nothing. A trike parked for a week
-- would therefore keep reading "Cosmo" for a week. This is not a
-- wait-for-the-next-cycle situation.
--
-- WHY THE PLATE LIST IS HARD-CODED:
-- vehicle_type_id was not persisted before sql/063, so stored rows carry only
-- the derived trio, and id=3 (a genuine Cosmo) and id=5 produced an identical
-- trio — bicycle / sitting / "Cosmo". Nothing on disk distinguishes them. The
-- only available discriminator is Veo's live feed, where vehicle_type_id is
-- still present, joined back to our rows on the painted plate. Plate is a
-- sound join key here: it is the number physically printed under the QR code,
-- so it is stable for the life of the vehicle and never migrates between
-- models.
--
-- The 52 plates below are the UNION of two captures of every
-- vehicle_type_id=5 unit in Veo's free_bike_status feed: 2026-07-29T23:41Z
-- and 2026-08-06T17:54Z. A union, not the newer capture, because the two
-- disagree in both directions -- the second adds 2 vehicles that entered the
-- fleet in between, and is missing 8 that left it. A vehicle that has left
-- the feed still has history in these tables and still needs correcting, so
-- dropping it would leave rows mislabelled that we can still identify.
--
-- KNOWN LIMITATION: any id=5 vehicle that had already left the feed before
-- the FIRST capture cannot be identified and keeps its "Cosmo" label. There
-- is no way to recover those -- the discriminator no longer exists for them.
-- Going forward sql/063 stores the id, so no future reclassification needs
-- this technique, and no future drift needs a second capture.
--
-- Every UPDATE is guarded on vehicle_model_name = 'Cosmo' so this is
-- idempotent and cannot overwrite any other label.
--
-- NOT touched: model_reports.resolved_model_name. Those rows are what a
-- human rider actually submitted; rewriting them would be falsifying
-- evidence, not correcting a derivation.

-- NO explicit BEGIN/COMMIT here. src/pg.py.run_migrations() applies every
-- pending file on one connection and commits ONCE at the end, so the whole
-- batch is all-or-nothing; committing mid-file would silently give that up
-- (and BEGIN inside the open transaction just warns). The scratch tables are
-- therefore dropped by hand at the bottom rather than via ON COMMIT DROP.
DROP TABLE IF EXISTS _rover_plates;
CREATE TEMPORARY TABLE _rover_plates (vehicle_plate TEXT PRIMARY KEY);

INSERT INTO _rover_plates (vehicle_plate) VALUES
        ('1018412'),
        ('1034090'),
        ('1034203'),
        ('1034213'),
        ('1034217'),
        ('1034788'),
        ('1035535'),
        ('1035620'),
        ('1036015'),
        ('1036250'),
        ('1036294'),
        ('1036342'),
        ('1036431'),
        ('1036601'),
        ('1036628'),
        ('1036631'),
        ('1036632'),
        ('1036636'),
        ('1036637'),
        ('1036638'),
        ('1036641'),
        ('1036642'),
        ('1036645'),
        ('1036646'),
        ('1036649'),
        ('1036651'),
        ('1036652'),
        ('1036656'),
        ('1036657'),
        ('1036659'),
        ('1036661'),
        ('1036662'),
        ('1036663'),
        ('1036664'),
        ('1036791'),
        ('1036792'),
        ('1036793'),
        ('1036795'),
        ('1036801'),
        ('1036802'),
        ('1036804'),
        ('1036805'),
        ('1036815'),
        ('1036816'),
        ('1036820'),
        ('1036821'),
        ('1036822'),
        ('1036823'),
        ('1036824'),
        ('1036825'),
        ('1036831'),
        ('1036837');

-- Resolve plates to the stable HMAC identifier the trip-side tables key on.
DROP TABLE IF EXISTS _rover_vids;
CREATE TEMPORARY TABLE _rover_vids (vehicle_identifier TEXT PRIMARY KEY);

INSERT INTO _rover_vids (vehicle_identifier)
SELECT DISTINCT ds.vehicle_identifier
FROM device_state ds
JOIN _rover_plates p ON p.vehicle_plate = ds.vehicle_plate
WHERE ds.vehicle_identifier IS NOT NULL;

-- Seed the newly-added id column (sql/063) for the cohort, so the next
-- reclassification is a one-liner keyed on the id instead of a plate list.
UPDATE device_state ds
SET current_vehicle_type_id = '5'
FROM _rover_plates p
WHERE p.vehicle_plate = ds.vehicle_plate
  AND ds.current_vehicle_type_id IS DISTINCT FROM '5';

UPDATE device_state ds
SET current_vehicle_model_name = 'Rover'
FROM _rover_plates p
WHERE p.vehicle_plate = ds.vehicle_plate
  AND ds.current_vehicle_model_name = 'Cosmo';

UPDATE raw_telemetry_points r
SET vehicle_model_name = 'Rover',
    vehicle_type_id    = COALESCE(r.vehicle_type_id, '5')
FROM _rover_plates p
WHERE p.vehicle_plate = r.vehicle_plate
  AND r.vehicle_model_name = 'Cosmo';

UPDATE device_history h
SET vehicle_model_name = 'Rover'
FROM _rover_plates p
WHERE p.vehicle_plate = h.vehicle_plate
  AND h.vehicle_model_name = 'Cosmo';

-- Trip-side tables key on vehicle_identifier, not plate.
UPDATE trip_events te
SET vehicle_model_name = 'Rover'
FROM _rover_vids v
WHERE v.vehicle_identifier = te.vehicle_identifier
  AND te.vehicle_model_name = 'Cosmo';

UPDATE daily_vehicle_trip_counts d
SET vehicle_model_name = 'Rover'
FROM _rover_vids v
WHERE v.vehicle_identifier = d.vehicle_identifier
  AND d.vehicle_model_name = 'Cosmo';

-- Relabelling these rows re-groups them for the battery model's per-model
-- intercept (src/battery_model.py fits one offset per vehicle_model_name).
-- That is the point: Rover trips stop being averaged into the Cosmo's
-- intercept. Until the next refit, "Rover" is absent from model_offsets and
-- estimate_burn_percent falls back to the "_default" offset.
UPDATE battery_trip_observations b
SET vehicle_model_name = 'Rover'
FROM _rover_vids v
WHERE v.vehicle_identifier = b.vehicle_identifier
  AND b.vehicle_model_name = 'Cosmo';

DROP TABLE _rover_plates;
DROP TABLE _rover_vids;
