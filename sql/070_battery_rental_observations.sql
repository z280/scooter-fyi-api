-- Battery observations mined from reservation episodes.
--
-- src/battery_model.py used to define a trip as an OBSERVATION GAP, on the
-- premise that "a rented vehicle drops out of GBFS free_bike_status". Veo does
-- not: it keeps the vehicle listed for the whole rental, sampled every 2
-- minutes, moving, with is_reserved true (see sql/069 and the correction note
-- in API_REQUIREMENTS.md). A real rental produces no gap at all, so the old
-- anchor was mining feed outages that happened to coincide with movement.
--
-- The window compounded it. MIN_DURATION_S=10min / MAX_DURATION_S=30min were
-- calibrated for the pre-2026-07-07 10-minute ingest cadence, where that meant
-- 1-3 missed observations. At the 2-minute cadence it demanded a vehicle be
-- missing for 5-15 consecutive cycles. Yield: 31 rows in this table, in total,
-- ever; battery_model_coefficients empty; /api/v1/route reporting
-- battery_model "unavailable" since the day it shipped.
--
-- Anchored on reservation episodes over the same fleet on 2026-08-09: 29,754
-- episodes with both battery endpoints, 24,954 usable after the SoC filters.
--
-- Two columns are needed for that data to land.

-- 1. `source`. The existing CHECK allows only 'feed_mined' and 'donated_ride'.
--    Rows mined from a reservation episode are neither: 'feed_mined' means the
--    old gap model, and keeping the two under one label would make it
--    impossible to tell an outage-derived observation from a rental-derived
--    one when auditing the fit. Old rows keep their label; nothing is
--    rewritten.
ALTER TABLE battery_trip_observations
    DROP CONSTRAINT IF EXISTS battery_trip_observations_source_allowed;

ALTER TABLE battery_trip_observations
    ADD CONSTRAINT battery_trip_observations_source_allowed
    CHECK (source IS NULL OR source IN ('feed_mined', 'donated_ride', 'gbfs_rental'));

-- 2. `waypoint_count`. Because the vehicle stays in the feed while it is
--    ridden, we get its position every 2 minutes for the whole rental, and
--    route THROUGH those points rather than origin-to-destination. That is not
--    a cosmetic difference: measured over 250 episodes the waypoint route is
--    1.32x the direct route at p50 and 3.87x at p90, and 6% of episodes are
--    loops ending within 400 m of their origin while covering over 800 m.
--
--    Routing through via-points fails on ~2% of episodes, which fall back to
--    the direct two-point route. Those rows carry a systematically SHORTER
--    distance for the same burn, so the fit needs to be able to see which is
--    which. NULL = direct route (no track, or the track would not thread);
--    a count = that many via-points were used.
ALTER TABLE battery_trip_observations
    ADD COLUMN IF NOT EXISTS waypoint_count INTEGER;

COMMENT ON COLUMN battery_trip_observations.waypoint_count IS
    'Number of in-ride GBFS samples used as Valhalla via-points for '
    'route_distance_meters. NULL means the direct origin-to-destination route '
    'was used instead - either no in-ride track was available (donated rides, '
    'old feed_mined rows) or the via-point route failed to thread. Those rows '
    'understate ridden distance; see src/battery_model.py WHY THE WAYPOINTS '
    'MATTER.';

CREATE INDEX IF NOT EXISTS idx_battery_obs_source
    ON battery_trip_observations (source);
