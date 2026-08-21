-- The city's clarified, contractually-binding Equity Area map: metric group
-- `equity` (src/equity_groups.py OFFICIAL_GROUP), backed by
-- data/equity.geojson and served as boundary layer `equity`.
--
-- Same column shape as every other tracked group (sql/015 for er1..er6,
-- sql/017 for the sitting/standing dimension): totals INTEGER on the
-- per-cycle snapshot and NUMERIC(10,2) on the daily average, percents
-- NUMERIC(5,2) throughout.
--
-- UNLIKE er1..er6, this group DOES get a `compliance_equity_pass` boolean:
-- it is the boundary Exhibit B's 30% Equity Area Deployment target is
-- measured against now that the city has said which map it means. v1/v2
-- keep their own flags so the pre-clarification series stays readable
-- beside it -- nothing is dropped, the authoritative answer is just no
-- longer v1's.
--
-- Every column is nullable with no backfill here on purpose. Historical
-- snapshot rows predate the map, so their `*_equity` values stay NULL
-- until `python -m src.cli reprocess_equity_compliance` recomputes them
-- from device_history (src/equity_backfill.py). A NULL reads as "not
-- reprocessed yet", which is exactly true, and AVG() skips it -- so a
-- partially-reprocessed day cannot quietly average a hole as a zero.

ALTER TABLE snapshot_metadata_core
    ADD COLUMN IF NOT EXISTS total_devices_equity             INTEGER,
    ADD COLUMN IF NOT EXISTS total_bike_equity                INTEGER,
    ADD COLUMN IF NOT EXISTS total_scooter_equity             INTEGER,
    ADD COLUMN IF NOT EXISTS total_sitting_equity             INTEGER,
    ADD COLUMN IF NOT EXISTS total_standing_equity            INTEGER,
    ADD COLUMN IF NOT EXISTS percent_all_devices_equity       NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS percent_all_bikes_equity         NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS percent_all_scooters_equity      NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS percent_all_sitting_equity       NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS percent_all_standing_equity      NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS percent_bikes_equity             NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS percent_scooters_equity          NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS percent_sitting_equity           NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS percent_standing_equity          NUMERIC(5,2);

ALTER TABLE daily_sla_compliance
    ADD COLUMN IF NOT EXISTS avg_total_devices_equity         NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS avg_total_bike_equity            NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS avg_total_scooter_equity         NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS avg_total_sitting_equity         NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS avg_total_standing_equity        NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS avg_percent_all_devices_equity   NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS avg_percent_all_bikes_equity     NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS avg_percent_all_scooters_equity  NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS avg_percent_all_sitting_equity   NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS avg_percent_all_standing_equity  NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS avg_percent_bikes_equity         NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS avg_percent_scooters_equity      NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS avg_percent_sitting_equity       NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS avg_percent_standing_equity      NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS compliance_equity_pass           BOOLEAN;

-- The reprocessing job (src/equity_backfill.py) reconstructs the fleet at
-- each past cycle from device_history's stop intervals: the stops that
-- overlap a day are those NOT YET DEPARTED when it began, i.e.
-- `departed_at > day_start OR departed_at IS NULL`. Neither existing index
-- serves that -- sql/004 indexes (vehicle_identifier, snapshot_time),
-- snapshot_time alone, and open stops by vehicle_identifier -- and the
-- other half of the predicate (`snapshot_time < day_end`) selects nearly
-- the whole table for any day but the newest, so it cannot carry the scan.
-- A plain btree on departed_at indexes NULLs too, so it serves both the
-- still-parked arm and the range.
CREATE INDEX IF NOT EXISTS idx_device_history_departed_at
    ON device_history (departed_at);

-- snapshot_metadata_core already has idx_core_snapshot_time (sql/001) for
-- the per-cycle window select, so nothing is needed there.
