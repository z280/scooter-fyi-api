-- The movement ring is 25 m, and the column comment now says which 25 m.
--
-- WHY A NEW FILE RATHER THAN AN EDIT TO sql/072. Migrations are tracked by
-- FILENAME in schema_migrations (src/pg.py), so a database that has already
-- applied 072 will never re-read it. Editing that file would have changed the
-- comment in the repository and nowhere else — including production, where the
-- comment is the thing an operator actually reads off the column.
--
-- WHAT CHANGED, AND WHY. Three numbers in this codebase claimed to be "how far
-- is moved": 16 m in config.json, 25 m in sql/072's own header, 50 m in
-- device_state.py. 072 contradicted itself — its header describes a validation
-- computed at 25 m ("9.1% never get 25 m from the kerb", 214,846 reservation
-- episodes) while its column comment said the counter used
-- stationary_threshold_meters, which was 16 m. The owner's decision on
-- 2026-10-06 is 25 m, so the ingest now counts at the radius its own
-- validation was computed at.
--
-- The 50 m in device_state.py (IN_PLACE_RADIUS_M, JITTER_RADIUS_M) is NOT this
-- ring and is deliberately unchanged: it answers "did this rental go anywhere
-- at all, or is this GPS noise?", and that number is measured rather than
-- chosen. See the comment above those constants.
--
-- THE COUNTERS ARE NOT RETROACTIVE. rentals_observed and rentals_no_go have
-- accumulated at 16 m since 072 and are left exactly as they are: this
-- migration does not reset them, because whether to reset or to stamp the
-- changeover is a data decision and not one a schema file should make
-- quietly. Until that is settled, a lifetime rate spans both definitions, and
-- the comment below says so on the column itself rather than in a document
-- somebody has to find.

COMMENT ON COLUMN device_state.rentals_no_go IS
    'Of those, how many ended within the ingest''s movement radius of where '
    'the rider unlocked it: the scooter did not go. Fleet baseline ~9%. That '
    'radius is 25 m from 2026-10-06 (sql/088) and was 16 m before it, so '
    'lifetime totals span both definitions; see sql/088.';
