-- Rental outcomes, per hour, per place, per model, at the radius they were
-- counted at.
--
-- WHY THIS CANNOT WAIT. device_state.rentals_observed / rentals_no_go (sql/072)
-- only ever count up, so they answer "since the counters started" and nothing
-- else: not last Tuesday, not 6 pm against 9 am, not this neighbourhood. Every
-- time-series question the stats drawer and the rider-story figure need is
-- unanswerable from them, and raw_telemetry_points is truncated at ~48 h, so
-- none of it can be derived later. Each day without this table is a day of
-- history that cannot be recovered. (docs/ANALYTICS_TIER2_BACKLOG.md §1; the
-- frontend's docs/ANALYTICS_PLAN.md §1 tier 2.)
--
-- HOW IT IS WRITTEN. By src/device_state.py, in the SAME transaction that
-- increments the per-vehicle counters, at the moment each rental completes:
-- counted at the source, never derived by scanning. A row is upserted per
-- (hour, h3_9 cell, model, radius); counts add.
--
-- THE GRAIN.
--   hour      date_trunc('hour') of the cycle that saw the rental END, UTC.
--             Hourly because the story has a shape within a day; rolling up
--             to days and weeks is free, the reverse is impossible.
--   h3_9      the cell where the rider UNLOCKED it (the last fix before the
--             rental). For a vehicle FIRST SEEN mid-rental the unlock point
--             was never observed, so its rental is attributed to where it was
--             first sighted and counted in origin_unknown (below), which an
--             equity cut excludes. Origin, because a
--             no-go happens where the vehicle was, and because attributing at
--             write time is what makes an equity cut honest: a vehicle's
--             CURRENT location says nothing about where its past rentals were.
--             Resolution 9 rather than 10: a rate needs a denominator, and r10
--             cells would mostly be single digits.
--   model     vehicle_model_name, 'Unknown' when the feed omits it.
--   radius_m  stationary_threshold_meters at write time (25 m today), NUMERIC
--             so a fractional radius still matches exactly. ON EVERY
--             ROW, and in the key. The lifetime counters spanned 16 m and 25 m
--             with no way to tell which share was which, until sql/089 had to
--             reset them; a table built from scratch does not repeat that, and
--             the column is impossible to add retroactively.
--
-- THE COUNTS.
--   rentals     completed rentals (every release).
--   no_gos      of those, END displacement within radius_m: the drop point is
--               within radius_m of the unlock point. The SAME definition as
--               device_state.rentals_no_go, so the two always agree.
--   no_gos_max  of those, MAXIMUM displacement within radius_m: the vehicle
--               never got farther than radius_m from where it was unlocked
--               (the running maximum sql/087 keeps, plus the final fix). This
--               is what "never left the kerb" describes. no_gos - no_gos_max
--               is the round trips counted as no-gos, which is how big the gap
--               between the published copy and the counter is.
--   max_known   rentals for which the maximum is known (NULL for a rental whose
--               origin was unknown, or that began before sql/087). no_gos_max is
--               only meaningful over these.
--   origin_unknown  rentals whose unlock point was never observed (the vehicle's
--               first-ever sighting was already mid-rental), so h3_9 is where it
--               was first seen, not where it was unlocked. Anything attributing
--               rentals to PLACES (the equity cut) excludes these.
--
-- NO PERSONAL DATA: no vehicle identifier, no account, no position finer than
-- an r9 cell (~0.1 km²).

SET lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS rental_outcomes_hourly (
    hour        TIMESTAMPTZ NOT NULL,
    h3_9        BIGINT      NOT NULL,
    model       TEXT        NOT NULL,
    radius_m    NUMERIC(6,2) NOT NULL,
    rentals     INTEGER     NOT NULL DEFAULT 0 CHECK (rentals >= 0),
    no_gos      INTEGER     NOT NULL DEFAULT 0 CHECK (no_gos >= 0),
    no_gos_max  INTEGER     NOT NULL DEFAULT 0 CHECK (no_gos_max >= 0),
    max_known   INTEGER     NOT NULL DEFAULT 0 CHECK (max_known >= 0),
    origin_unknown INTEGER  NOT NULL DEFAULT 0 CHECK (origin_unknown >= 0),
    PRIMARY KEY (hour, h3_9, model, radius_m),
    CHECK (no_gos <= rentals AND max_known <= rentals AND no_gos_max <= max_known
           AND origin_unknown <= rentals)
);

-- Time windows are served by the primary key (it leads with hour). Places over
-- a long window (the per-place, per-week figure; the equity cut over months)
-- need the cell first.
CREATE INDEX IF NOT EXISTS idx_rental_outcomes_hourly_cell_hour
    ON rental_outcomes_hourly (h3_9, hour);

COMMENT ON TABLE rental_outcomes_hourly IS
    'Completed rentals per (hour, unlock-point h3_9 cell, model, radius), written '
    'by device_state.py at release in the same transaction as the per-vehicle '
    'counters. See sql/090.';

RESET lock_timeout;
