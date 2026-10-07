-- Fleet analytics rollups (docs/PLAN_FLEET_ANALYTICS.md).
--
-- The owner's fleet dashboard (2026-10-07) needs rides, failed starts and
-- dwell by model AND by region over months. Placing an event in a region is a
-- point-in-polygon test; doing that per request over 11M trips is not a query
-- anyone can wait for. So each event is placed ONCE, when the rollup takes it
-- (src/analytics_rollups.py), and the dashboard reads sums.
--
--   region_type  'city' (every event, region_name 'Denver'), or one of the
--                official boundary layers: 'neighborhood', 'council_district',
--                'community_network'. An event outside every polygon of a
--                layer gets no row for that layer (it is still in 'city').
--   model        vehicle_model_name, 'Unknown' when the feed omits it.
--
-- Hours are UTC; the API buckets days/weeks/months in America/Denver.
--
-- Refreshed incrementally at the end of every ingest cycle (watermarks in
-- analytics_rollup_state); `cli analytics_backfill` fills the history once.
-- No vehicle identifier, no account, no position: sums by region and model.

SET lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS analytics_rides_hourly (
    hour        TIMESTAMPTZ NOT NULL,
    region_type TEXT        NOT NULL,
    region_name TEXT        NOT NULL,
    model       TEXT        NOT NULL,
    rides       INTEGER     NOT NULL DEFAULT 0 CHECK (rides >= 0),
    PRIMARY KEY (hour, region_type, region_name, model)
);
CREATE INDEX IF NOT EXISTS idx_analytics_rides_region_hour
    ON analytics_rides_hourly (region_type, region_name, hour);

-- Failed starts are counted when their stop CLOSES (device_history row gets
-- departed_at), placed at the stop.
CREATE TABLE IF NOT EXISTS analytics_failed_starts_hourly (
    hour                TIMESTAMPTZ NOT NULL,
    region_type         TEXT        NOT NULL,
    region_name         TEXT        NOT NULL,
    model               TEXT        NOT NULL,
    failed_starts       INTEGER     NOT NULL DEFAULT 0 CHECK (failed_starts >= 0),
    stops_with_failures INTEGER     NOT NULL DEFAULT 0 CHECK (stops_with_failures >= 0),
    PRIMARY KEY (hour, region_type, region_name, model)
);
CREATE INDEX IF NOT EXISTS idx_analytics_failed_starts_region_hour
    ON analytics_failed_starts_hourly (region_type, region_name, hour);

-- Dwell: arrival (snapshot_time) to departure (departed_at), per closed stop,
-- by the Denver-local day it closed. Stops over 30 days are excluded.
CREATE TABLE IF NOT EXISTS analytics_dwell_daily (
    day           DATE    NOT NULL,
    region_type   TEXT    NOT NULL,
    region_name   TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    dwells        INTEGER NOT NULL DEFAULT 0 CHECK (dwells >= 0),
    dwell_seconds BIGINT  NOT NULL DEFAULT 0 CHECK (dwell_seconds >= 0),
    PRIMARY KEY (day, region_type, region_name, model)
);
CREATE INDEX IF NOT EXISTS idx_analytics_dwell_region_day
    ON analytics_dwell_daily (region_type, region_name, day);

-- Vehicles on the map per region, per hour: the sum of count_total over the
-- hour's cycles and how many cycles, so an average is sum / cycles at any
-- grain. Averaging regional_metrics_narrow live took ~30 s for a week of 78
-- neighbourhoods.
CREATE TABLE IF NOT EXISTS analytics_region_devices_hourly (
    hour        TIMESTAMPTZ NOT NULL,
    region_type TEXT        NOT NULL,
    region_name TEXT        NOT NULL,
    devices_sum BIGINT      NOT NULL DEFAULT 0 CHECK (devices_sum >= 0),
    cycles      INTEGER     NOT NULL DEFAULT 0 CHECK (cycles >= 0),
    PRIMARY KEY (hour, region_type, region_name)
);
CREATE INDEX IF NOT EXISTS idx_analytics_region_devices_region_hour
    ON analytics_region_devices_hourly (region_type, region_name, hour);

CREATE TABLE IF NOT EXISTS analytics_rollup_state (
    name           TEXT PRIMARY KEY,
    watermark_id   BIGINT,
    watermark_time TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Vehicles seen in the last 7 days but absent from this cycle's feed (rented
-- out past the feed, in the shop, or gone). Written per cycle from this
-- migration on; NULL for earlier cycles, which is "not recorded", not zero.
ALTER TABLE device_status_snapshots ADD COLUMN IF NOT EXISTS off_map INTEGER;

RESET lock_timeout;
