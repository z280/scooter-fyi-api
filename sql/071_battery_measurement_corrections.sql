-- The reported range is not a fuel gauge, and the model was fitting its
-- artifacts.
--
-- sql/070 measured burn as (last parked reading) - (first reading after the
-- reservation cleared). Both ends of that are wrong, for different reasons,
-- and together they produced an 8.93 pp intercept — a model claiming a rider
-- spends ~9% of a battery before travelling a metre.
--
-- WHAT THE FEED ACTUALLY DOES, measured on the R2 telemetry archive:
--
-- 1. The reading SAGS UNDER LOAD. Comparing the last parked reading with the
--    first reading once reserved, over 27,113 episodes:
--
--        displacement so far      n      mean SoC drop
--        under 25 m (unmoved)  19138          0.17 pp
--        25-100 m               3733          8.38 pp
--        100-400 m              3934          9.89 pp
--        over 400 m              308         10.67 pp
--
--    A vehicle that has woken but not moved shows no change, so this is not a
--    wake-up correction. One that has gone 25-100 m reads 8.4 pp lower.
--    Nothing consumes 8% of a pack in 100 m — the range collapses under load
--    and recovers at rest.
--
-- 2. The reading GOES STALE WHILE PARKED. 99.4% of parked 2-minute steps show
--    no change at all; the value is frozen rather than tracked. The longer a
--    vehicle sits, the more optimistic it reads relative to what it reports
--    once woken, and the difference lands in the next ride's burn:
--
--        parked before     n    mean burn   mean dist   burn/km
--        0-15 min        421      6.17 pp      3400 m      1.81
--        15-60 min       445      7.66 pp      3529 m      2.17
--        60-240 min      517      8.84 pp      3642 m      2.43
--        240-720 min     244     10.25 pp      4173 m      2.46
--        720+ min        169     10.83 pp      3964 m      2.73
--
--    Same distances, 51% more burn per km.
--
-- Corrected for both, swap-free, the intercept is ~1.4 pp rather than 8.93.

-- 1. Which post-ride sample supplied soc_end. The LATEST clean reading within
--    SETTLE_MAX_CYCLES, so a vehicle re-rented after four minutes still
--    contributes its post[2] reading instead of being dropped. Measured
--    swap-free over 1,662 episodes, mean burn by offset:
--
--        post   1      2      3      4      5      6      8     10
--        burn   9.15   8.38   8.17   8.08   8.07   8.06   8.04   8.05
--
--    It plateaus at 4-5. An earlier analysis appeared to show R2 DECLINING
--    past post[2] and stopped there; that decline was battery swaps during the
--    longer wait. Swap-free, R2 plateaus too (0.3938 at post[5]).
ALTER TABLE battery_trip_observations
    ADD COLUMN IF NOT EXISTS soc_end_offset_cycles SMALLINT;

COMMENT ON COLUMN battery_trip_observations.soc_end_offset_cycles IS
    'Which post-ride ingest cycle supplied soc_end: 1 = the drop-point sample '
    '(no settling available), up to SETTLE_MAX_CYCLES. Higher is better '
    'settled. NULL = written before sql/071, i.e. always the unsettled first '
    'sample, and biased ~1 pp high.';

-- 2. How stale the pre-ride reading was. Recorded rather than filtered on, so
--    every observation still trains the distance coefficient while the fit can
--    model the bias and PREDICT at parked = 0 — the burn a rider would see on
--    a vehicle whose reading is fresh.
ALTER TABLE battery_trip_observations
    ADD COLUMN IF NOT EXISTS parked_seconds_before DOUBLE PRECISION;

COMMENT ON COLUMN battery_trip_observations.parked_seconds_before IS
    'Seconds the vehicle sat available between its previous rental and this '
    'one. The reported range is frozen while parked, so a large value means a '
    'stale, optimistic pre-ride reading and an overstated burn (~0.17 pp per '
    'hour). NULL = written before sql/071, or no prior parked run was in the '
    'scan window.';

-- 3. The staleness term itself, so serving can predict at parked = 0.
ALTER TABLE battery_model_coefficients
    ADD COLUMN IF NOT EXISTS beta_parked_seconds DOUBLE PRECISION;

COMMENT ON COLUMN battery_model_coefficients.beta_parked_seconds IS
    'Burn attributable to how long the vehicle had been parked, per second. A '
    'measurement artifact, not consumption: estimate_burn_percent deliberately '
    'evaluates it at 0 so predictions describe a fresh reading. NULL on fits '
    'made before sql/071.';

-- Rows written before this migration used the unsettled reading and have no
-- staleness recorded. They are NOT rewritten — soc_end cannot be recovered
-- from a stored row, only by re-mining the archive — and train() excludes them
-- rather than mixing two measurement definitions in one fit.
CREATE INDEX IF NOT EXISTS idx_battery_obs_settled
    ON battery_trip_observations (soc_end_offset_cycles)
    WHERE soc_end_offset_cycles IS NOT NULL;
