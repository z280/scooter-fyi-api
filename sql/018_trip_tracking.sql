-- Trip/popularity tracking.
--
-- trip_events: one row per detected "successful trip" — a MOVED
-- transition in src/device_state.py (the vehicle's position changed by
-- more than the stationary threshold between consecutive cycles). This
-- is the same event that already closes a device_history stop and opens
-- a new one; trip_events exists as its own append-only log so daily
-- popularity rollups don't have to infer "was this stop caused by a
-- trip, or is this the vehicle's very first-ever observation" from
-- device_history (whose first row per vehicle is a sighting, not a trip).
--
-- distance_meters is straight-line (the same flat-earth approximation
-- device_state.py already uses for the movement-threshold check), not an
-- odometer reading — the vehicle may have taken a longer real path.

CREATE TABLE IF NOT EXISTS trip_events (
    id                  BIGSERIAL PRIMARY KEY,
    vehicle_identifier  TEXT NOT NULL,
    vehicle_plate       TEXT,
    cycle_id            UUID REFERENCES observation_cycles(cycle_id),
    detected_at         TIMESTAMPTZ NOT NULL,   -- snapshot_time of the cycle that detected the move
    form_factor         TEXT,
    vehicle_use_type    TEXT,
    vehicle_model_name  TEXT,
    from_lat            DOUBLE PRECISION,       -- null if the prior position was somehow unset
    from_lon            DOUBLE PRECISION,
    to_lat              DOUBLE PRECISION NOT NULL,
    to_lon              DOUBLE PRECISION NOT NULL,
    distance_meters     DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_trip_events_vehicle
    ON trip_events (vehicle_identifier, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_trip_events_detected_at
    ON trip_events (detected_at DESC);

-- Daily rollup, computed at 9:00 AM Denver time alongside the compliance
-- SLA job (src/daily_trips.py, invoked from src/cli.py's daily_sla
-- command / the same crontab slot). One full Denver-local calendar day
-- per row — this is a general popularity metric, not scoped to the
-- narrow 6am-9am SLA window daily_sla_compliance uses.
CREATE TABLE IF NOT EXISTS daily_trip_summary (
    trip_date                  DATE PRIMARY KEY,
    total_trips                INTEGER NOT NULL,
    distinct_vehicles_tripped  INTEGER NOT NULL,
    computed_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One row per (day, vehicle) that had >= 1 trip. Vehicles with zero trips
-- that day are simply absent, same convention as regional_metrics_narrow
-- omitting zero-count days elsewhere in this schema.
CREATE TABLE IF NOT EXISTS daily_vehicle_trip_counts (
    trip_date            DATE NOT NULL,
    vehicle_identifier   TEXT NOT NULL,
    vehicle_plate        TEXT,
    form_factor          TEXT,
    vehicle_use_type     TEXT,
    vehicle_model_name   TEXT,
    trip_count           INTEGER NOT NULL,
    popularity_rank      INTEGER NOT NULL,   -- 1 = most trips that day; ties share a rank
    PRIMARY KEY (trip_date, vehicle_identifier)
);
CREATE INDEX IF NOT EXISTS idx_daily_vehicle_trip_counts_rank
    ON daily_vehicle_trip_counts (trip_date, popularity_rank);
