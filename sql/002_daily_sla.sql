-- Daily 6 AM-9 AM Denver SLA window rollup.
-- Populated by src/daily_sla.py — one row per Denver-local date, computed
-- at 9:00 AM Denver time when the morning window closes.

CREATE TABLE IF NOT EXISTS daily_sla_compliance (
    sla_date              DATE PRIMARY KEY,        -- Denver-local date the window covers
    window_start_ts       TIMESTAMPTZ NOT NULL,    -- 6 AM Denver as UTC
    window_end_ts         TIMESTAMPTZ NOT NULL,    -- 9 AM Denver as UTC
    snapshot_count        INTEGER NOT NULL,        -- # of cycles whose snapshot_time fell in the window

    -- Averaged citywide counts (over snapshots in window)
    avg_total_devices_denver     NUMERIC(10,2),
    avg_total_devices_v1         NUMERIC(10,2),
    avg_total_devices_v2         NUMERIC(10,2),
    avg_total_bike_denver        NUMERIC(10,2),
    avg_total_bike_v1            NUMERIC(10,2),
    avg_total_bike_v2            NUMERIC(10,2),
    avg_total_scooter_denver     NUMERIC(10,2),
    avg_total_scooter_v1         NUMERIC(10,2),
    avg_total_scooter_v2         NUMERIC(10,2),
    avg_total_not_in_denver      NUMERIC(10,2),

    -- Averaged percentages — the compliance metrics
    avg_percent_all_devices_v1   NUMERIC(5,2),     -- THE 30% SLA metric (Exhibit B)
    avg_percent_all_devices_v2   NUMERIC(5,2),
    avg_percent_all_bikes_v1     NUMERIC(5,2),
    avg_percent_all_bikes_v2     NUMERIC(5,2),
    avg_percent_all_scooters_v1  NUMERIC(5,2),
    avg_percent_all_scooters_v2  NUMERIC(5,2),
    avg_percent_bikes_denver     NUMERIC(5,2),
    avg_percent_scooters_denver  NUMERIC(5,2),
    avg_percent_bikes_v1         NUMERIC(5,2),
    avg_percent_scooters_v1      NUMERIC(5,2),
    avg_percent_bikes_v2         NUMERIC(5,2),
    avg_percent_scooters_v2      NUMERIC(5,2),

    -- Pass/fail flags vs the 30% threshold (NULL when no data / window empty)
    compliance_v1_pass    BOOLEAN,
    compliance_v2_pass    BOOLEAN,

    computed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_daily_sla_date ON daily_sla_compliance (sla_date DESC);
