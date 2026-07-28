-- Backfill tracked_rides.distance_meters for rides that ended BEFORE
-- sql/034 added the column.
--
-- sql/034 added distance_meters/distance_source and nothing else, so every
-- ride completed before it ran carries user_reported_ended_at IS NOT NULL
-- with distance_meters IS NULL. src/badges.py:_ride_badges counts those as
-- 0 m — but still feeds their end dates into the streak set — so a rider
-- with real history reads as having ridden zero miles. That is the mileage
-- badges silently starving on their own historical data.
--
-- WHAT THIS MEASURES: start -> user-reported end, as the crow flies,
-- tagged 'straight_line'. This is the same fallback PATCH
-- /api/v1/tracked-rides/{id}/end applies to a ride with no waypoints, and
-- it uses the identical flat-earth formula as src/geo.py:distance_meters
-- (111 320 m per degree of latitude, scaled east-west by the cosine of the
-- midpoint latitude) so backfilled and live numbers are comparable.
--
-- Rides that DID upload waypoints are backfilled the same way and
-- deliberately still tagged 'straight_line': path_polyline holds a
-- Google-encoded polyline (src/polyline.py) which plain SQL cannot decode,
-- and claiming 'waypoints' for a number we did not measure along the track
-- would put a lie in the provenance column that sql/034 exists to keep
-- honest. Undercounting is the kinder failure here — it only ever delays a
-- badge — and 'straight_line' says exactly that out loud.
--
-- Rows with no end coordinates are left NULL: there is nothing to measure
-- between, and inventing a 0 would be indistinguishable from a real
-- zero-distance ride.
--
-- REPLAY SAFETY: `distance_meters IS NULL` is the entire idempotency
-- guard. Re-running matches nothing the first pass already filled, and a
-- ride that ends after this migration gets its distance from the API, not
-- from here. It never overwrites a computed distance, so no data is lost.
UPDATE tracked_rides
   SET distance_meters = sqrt(
           power((end_lat - start_lat) * 111320.0, 2)
           + power(
               (end_lon - start_lon) * 111320.0
               * cos(radians((start_lat + end_lat) / 2.0)),
               2)
       ),
       distance_source = 'straight_line'
 WHERE user_reported_ended_at IS NOT NULL
   AND distance_meters IS NULL
   AND start_lat IS NOT NULL AND start_lon IS NOT NULL
   AND end_lat IS NOT NULL AND end_lon IS NOT NULL;
