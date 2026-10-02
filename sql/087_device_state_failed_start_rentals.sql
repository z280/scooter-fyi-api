-- Failed starts that end a rental, and the last few rentals' outcomes.
--
-- src/device_state.py counted a FAILED_START only when a vehicle that was
-- NOT reserved turned up within 16 m of its stored position with a rotated
-- bike_id. On Veo's feed that is the wrong shape for most failures: the
-- rider's unlock IS a reservation (is_reserved goes true for a cycle or
-- two), and when they give up the vehicle is released where it stood with a
-- new bike_id. That release took the MOVED branch, which reset
-- number_failed_starts to 0 and restarted the dwell clock, so the one event
-- that best predicts the next failure erased the evidence instead.
--
-- Measured over 95,198 reservation episodes in the R2 archive,
-- 2026-09-27 08:00Z .. 2026-10-01 08:00Z: a rental that never left a 50 m
-- circle around its origin AND came back with a rotated bike_id (2,878 of
-- them) is followed by another such failure 37.1% of the time, against 1.8%
-- after any other rental. The same circle without a rotation (586) is
-- followed by one 3.8% of the time: a reservation blip, not an attempt.
-- See IN_PLACE_RADIUS_M in src/device_state.py.
--
-- Telling those apart needs two facts the release cannot see on its own,
-- because sql/069 freezes the stored position at the origin and the
-- in-between samples are not kept:
--
--   rental_max_distance_m    the farthest the vehicle was seen from its
--                            origin during the current rental;
--   rental_origin_device_id  the bike_id it had when the rental started
--                            (current_device_id is refreshed at rental start,
--                            and Veo usually rotates at the release, but
--                            sometimes at the start).
--
-- Both are measured from a third new fact, the vehicle's last observed fix
-- outside a rental:
--
--   last_fix_lat/last_fix_lon  where the vehicle was last seen when not
--                              reserved, i.e. where a rental's rider unlocked
--                              it. Not current_lat/current_lon: that is the
--                              stop's position, which since this migration
--                              does not follow GPS drift (JITTER_RADIUS_M,
--                              UNROTATED_MOVE_M) and can sit tens of metres
--                              from the vehicle. Measured from it, a failure
--                              at a drifted vehicle looked like a 60 m ride.
--
-- The two rental columns are NULL outside a rental, and NULL rental_max_distance_m also means
-- "origin unknown" (a vehicle first seen mid-rental, or one already in a
-- rental when this migration ran). Such a release takes the old path, so
-- applying this migration changes nothing for rentals already in flight.
--
-- recent_no_go_mask is the outcome of the last 3 completed rentals, newest in
-- bit 0: 1 = the rental was a failed start (the definition above), 0 = it
-- went somewhere. Reservation blips push nothing. src/quality.py reads
-- popcount(mask) >= 2 as high_risk. Three bits on a row that is already
-- written on every release costs nothing, where "the last N rentals" from
-- trip_events would cost a scan per device per payload, and trip_events does
-- not record failures at all.

SET lock_timeout = '10s';

ALTER TABLE device_state
    ADD COLUMN IF NOT EXISTS rental_max_distance_m REAL;

ALTER TABLE device_state
    ADD COLUMN IF NOT EXISTS rental_origin_device_id TEXT;

ALTER TABLE device_state
    ADD COLUMN IF NOT EXISTS recent_no_go_mask SMALLINT NOT NULL DEFAULT 0;

ALTER TABLE device_state
    ADD COLUMN IF NOT EXISTS last_fix_lat DOUBLE PRECISION;

ALTER TABLE device_state
    ADD COLUMN IF NOT EXISTS last_fix_lon DOUBLE PRECISION;

COMMENT ON COLUMN device_state.rental_max_distance_m IS
    'Farthest distance (m) from the frozen origin observed during the current '
    'rental. NULL when not in a rental, or when the origin is unknown.';

COMMENT ON COLUMN device_state.rental_origin_device_id IS
    'bike_id at the start of the current rental, so the release can tell a '
    'rotated bike_id (a real attempt) from a reservation blip. NULL outside a rental.';

COMMENT ON COLUMN device_state.recent_no_go_mask IS
    'Outcomes of the last 3 completed rentals, newest in bit 0: 1 = failed '
    'start (released inside IN_PLACE_RADIUS_M with a rotated bike_id), 0 = went '
    'somewhere. Two or more set bits make reliability_tier high_risk.';

COMMENT ON COLUMN device_state.last_fix_lat IS
    'Latitude of the last observation NOT in a rental (frozen while reserved), '
    'so rental geometry is measured from where it was unlocked. NULL until the '
    'vehicle is next seen after sql/087; current_lat is used meanwhile.';

COMMENT ON COLUMN device_state.last_fix_lon IS
    'Longitude counterpart of last_fix_lat.';

RESET lock_timeout;
