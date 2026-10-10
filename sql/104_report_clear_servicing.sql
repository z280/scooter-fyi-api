-- Negative reports clear on servicing found in the HISTORY, not only in the
-- current snapshot (owner, 2026-10-10). src/fleet_reports.py documents the
-- rules; this adds what they read.
--
--   last_range_meters  the vehicle's last reported charge, so ingest can see
--                      a rise between two readings (src/device_state.py);
--   last_serviced_at   when the charge last ROSE by at least
--                      fleet_reports.charge_rise_meters() (5% of a full
--                      charge) between two readings: a swap or a charge.
--                      Kept across an absence, so a vehicle that comes back
--                      on the map with more charge counts as serviced.
--
-- Before this, a report cleared only when the charge NOW exceeded the charge
-- at report time. A vehicle swapped to full and then ridden back down never
-- did, and every report filed before sql/100 (no charge recorded) needed a
-- 95% reading at the moment of the check, so 11 vehicles that had since
-- made 18-730 moves read high risk (2026-10-10).
--
-- REPLAY SAFETY: re-executed by the _pg fixtures; IF NOT EXISTS and the
-- seed only touches NULLs.
ALTER TABLE device_state ADD COLUMN IF NOT EXISTS last_range_meters INTEGER;
ALTER TABLE device_state ADD COLUMN IF NOT EXISTS last_serviced_at TIMESTAMPTZ;

-- Seed the last reading from the newest complete cycle, so the first cycle
-- after deploy compares against a real reading instead of treating every
-- vehicle as unseen (no servicing is inferred from a seed).
UPDATE device_state ds
   SET last_range_meters = r.current_range_meters
  FROM raw_telemetry_points r
 WHERE ds.last_range_meters IS NULL
   AND r.vehicle_identifier = ds.vehicle_identifier
   AND r.current_range_meters IS NOT NULL
   AND r.cycle_id = (SELECT oc.cycle_id
                       FROM observation_cycles oc
                       JOIN snapshot_metadata_core USING (cycle_id)
                      WHERE oc.job_status = 'complete'
                      ORDER BY snapshot_time DESC
                      LIMIT 1);
