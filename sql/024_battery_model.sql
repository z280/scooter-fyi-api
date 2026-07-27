-- Empirical battery-burn model.
--
-- Replaces the static per-type battery estimate with a regression fit on
-- observed trips:
--
--     dSoC% = b0 + b1*distance_m + b2*elevation_gain_m + b3*temperature_C
--
-- Three things about the inputs drive this schema:
--
-- 1. The dependent variable is state-of-charge PERCENT, not
--    current_range_meters. Veo emits exactly 100 distinct range values
--    fleet-wide (data/range_soc_lut.json) — an integer SoC percent pushed
--    through one vendor lookup table, identical for every vehicle model. The
--    raw metres are a nonlinear re-encoding of that percent, so regressing on
--    them would fit the vendor's curve rather than physics.
--
-- 2. A trip is an OBSERVATION GAP, not a row in a trip table. GBFS
--    free_bike_status lists only available vehicles, so a rented scooter
--    disappears for the length of the ride; two consecutive observations of one
--    vehicle 10-30 minutes apart, with a position jump between them, bracket a
--    trip and the gap IS the duration.
--
--    Neither trip table works. trip_events records detected_at but no duration
--    at all. device_history looked right (it has departed_at) but measured over
--    1.37M stops its departed_at equals the NEXT stop's snapshot_time at p50,
--    p90 AND mean — it stores the cycle that detected the move, not the moment
--    of departure — so zero stops fall in the 10-30 minute band.
--
-- 3. distance/elevation are Valhalla's routed values, not straight-line.
--    trip_events.distance_meters is explicitly a flat-earth approximation and
--    understates the path actually ridden.

-- One row per accepted trip observation. Populated by
-- `python -m src.cli extract_battery_trips`; kept so a refit never has to
-- re-query Valhalla for trips it has already routed.
CREATE TABLE IF NOT EXISTS battery_trip_observations (
    id                     BIGSERIAL PRIMARY KEY,
    vehicle_identifier     TEXT NOT NULL,
    vehicle_model_name     TEXT,
    departed_at            TIMESTAMPTZ NOT NULL,
    arrived_at             TIMESTAMPTZ NOT NULL,
    duration_seconds       DOUBLE PRECISION NOT NULL,
    from_lat               DOUBLE PRECISION NOT NULL,
    from_lon               DOUBLE PRECISION NOT NULL,
    to_lat                 DOUBLE PRECISION NOT NULL,
    to_lon                 DOUBLE PRECISION NOT NULL,
    -- Valhalla routed values (the regressors).
    route_distance_meters  DOUBLE PRECISION NOT NULL,
    elevation_gain_meters  DOUBLE PRECISION,
    temperature_c          DOUBLE PRECISION,
    -- State of charge at each end, in percent, via data/range_soc_lut.json.
    soc_start_percent      DOUBLE PRECISION NOT NULL,
    soc_end_percent        DOUBLE PRECISION NOT NULL,
    -- Positive = battery consumed. The regression target.
    burn_percent           DOUBLE PRECISION NOT NULL,
    implied_mph            DOUBLE PRECISION,
    -- Set by the map-matching check when GPS breadcrumbs exist for the ride
    -- (§3G): the fraction of matched edge length that fell on the proposed
    -- route. NULL means "never checked", which is the common case.
    adherent               BOOLEAN,
    adherence_fraction     DOUBLE PRECISION,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- A vehicle can only make one trip out of a given stop.
    UNIQUE (vehicle_identifier, departed_at)
);

CREATE INDEX IF NOT EXISTS idx_battery_obs_departed
    ON battery_trip_observations (departed_at DESC);
CREATE INDEX IF NOT EXISTS idx_battery_obs_adherent
    ON battery_trip_observations (adherent) WHERE adherent IS NOT NULL;

-- Append-only fit history; the newest row is the live model. Keeping old fits
-- makes it possible to see a model degrade rather than discovering it via
-- rider complaints.
CREATE TABLE IF NOT EXISTS battery_model_coefficients (
    id                  BIGSERIAL PRIMARY KEY,
    fitted_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    window_start        TIMESTAMPTZ NOT NULL,
    window_end          TIMESTAMPTZ NOT NULL,
    n_observations      INTEGER NOT NULL,
    intercept           DOUBLE PRECISION NOT NULL,   -- b0
    beta_distance       DOUBLE PRECISION NOT NULL,   -- b1, %/metre
    beta_elevation      DOUBLE PRECISION NOT NULL,   -- b2, %/metre climbed
    beta_temperature    DOUBLE PRECISION NOT NULL,   -- b3, %/degree C
    r_squared           DOUBLE PRECISION,
    residual_std        DOUBLE PRECISION,
    -- Mean training temperature, used as the fallback when the live
    -- temperature lookup fails at request time.
    mean_temperature_c  DOUBLE PRECISION,
    -- Share of candidate trips whose SoC delta was exactly zero. The SoC grid
    -- is ~1 percentage point, so short trips can burn less than one step; a
    -- high value here means the fit is mostly quantization noise.
    zero_delta_fraction DOUBLE PRECISION,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_battery_model_fitted
    ON battery_model_coefficients (fitted_at DESC);

-- Hourly Denver temperature cache (Open-Meteo ERA5 archive). Small enough to
-- keep indefinitely; ~8,760 rows a year.
CREATE TABLE IF NOT EXISTS hourly_temperature (
    observed_hour  TIMESTAMPTZ PRIMARY KEY,
    temperature_c  DOUBLE PRECISION NOT NULL,
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
