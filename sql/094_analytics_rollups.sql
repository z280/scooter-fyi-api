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

-- CLOSE QUEUE for failed starts and dwell. A stop's departed_at is NOT a
-- usable watermark: the absent rule stamps it with the vehicle's last
-- observed time, and close_ghost_stops (a backstop re-run any time) or a
-- device_state outage can stamp it days or weeks behind "now", so a
-- departed_at watermark would skip those closes forever. Instead every close
-- is queued here in commit order by a trigger; the rollup consumes the queue
-- (deleting what it folds in, in the same transaction) after a 6 h settle,
-- and an in-place-release REOPEN (departed_at back to NULL) removes a close
-- that has not been folded in yet, so the reopened stop is counted once,
-- when it finally closes. A reopen after the settle double-counts that stop;
-- in-place releases are short rentals, so that is rare. No index on the large
-- device_history table is needed: the queue holds ~6 h of closes.
CREATE TABLE IF NOT EXISTS analytics_stop_closes (
    seq       BIGSERIAL PRIMARY KEY,
    stop_id   BIGINT      NOT NULL,
    closed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_analytics_stop_closes_stop ON analytics_stop_closes (stop_id);

CREATE OR REPLACE FUNCTION analytics_queue_stop_close() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.departed_at IS NOT NULL
       AND (TG_OP = 'INSERT' OR OLD.departed_at IS NULL) THEN
        INSERT INTO analytics_stop_closes (stop_id) VALUES (NEW.id);
    ELSIF TG_OP = 'UPDATE' AND NEW.departed_at IS NULL AND OLD.departed_at IS NOT NULL THEN
        DELETE FROM analytics_stop_closes WHERE stop_id = NEW.id;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_analytics_queue_stop_close ON device_history;
CREATE TRIGGER trg_analytics_queue_stop_close
    AFTER INSERT OR UPDATE OF departed_at ON device_history
    FOR EACH ROW EXECUTE FUNCTION analytics_queue_stop_close();

-- Stops closed before this migration are not in the queue. The backfill
-- folds them in by departed_at up to this cutover; the queue takes stops
-- whose departed_at is after it. (One rule each, so nothing is counted by
-- both.)
INSERT INTO analytics_rollup_state (name, watermark_time)
VALUES ('stops_cutover', NOW())
ON CONFLICT (name) DO NOTHING;

-- Vehicles seen in the last 7 days but absent from this cycle's feed (rented
-- out past the feed, in the shop, or gone). Written per cycle from this
-- migration on; NULL for earlier cycles, which is "not recorded", not zero.
ALTER TABLE device_status_snapshots ADD COLUMN IF NOT EXISTS off_map INTEGER;

RESET lock_timeout;
