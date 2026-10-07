-- rental_outcomes_hourly: the Equity Area of each rental's UNLOCK POINT, in
-- the key.
--
-- WHY. sql/090 attributes a rental to its unlock-point r9 cell (~0.1 km²).
-- The first equity cut (PR #114) classified those cells against the official
-- Equity Areas, counting a cell "inside" only when its whole hexagon was. The
-- adversarial review measured that on 28 days of production trip origins:
-- the 30 areas are small (22 under 1.5 km²), so only 1.5% of rentals landed
-- "inside", 30% straddled a boundary, about nine in ten rentals that really
-- started in an Equity Area were dropped, 14 of the 30 areas contributed
-- nothing, and one area was half the sample. Labelled "the official Equity
-- Areas", that is not what it measured.
--
-- The point is known at the moment of release (device_state.py has the last
-- fix before the rental), and only then: raw fixes are truncated within ~48 h,
-- so the area cannot be recovered later. The table is hours old; this is the
-- same "now or never" sql/090 made for radius_m.
--
-- VALUES.
--   'EQ_001' .. 'EQ_030'  the official Equity Area containing the unlock point.
--   'outside'             the unlock point is in no Equity Area.
--   'unknown'             no unlock point to test: the vehicle was first seen
--                         mid-rental (origin_unknown), no fix was available, or
--                         the boundary layer could not be read. Excluded from
--                         any place comparison, exactly — these rentals get
--                         their own rows instead of contaminating others'.
--   'unrecorded'          rows written before this migration.
--
-- PRIVACY. An Equity Area is coarser than the r9 cell already stored; no new
-- precision about any rental is kept.
--
-- DEPLOY WINDOW. Old code's ON CONFLICT names the old key and fails against
-- the new one. A device_state cycle that fails rolls back whole, vehicles
-- stay IN_RENTAL in device_state, and the next cycle (2 min) records the
-- release: at worst one cycle is retried, nothing is lost or double counted.

SET lock_timeout = '10s';

ALTER TABLE rental_outcomes_hourly
    ADD COLUMN IF NOT EXISTS equity_area TEXT NOT NULL DEFAULT 'unrecorded';

ALTER TABLE rental_outcomes_hourly DROP CONSTRAINT IF EXISTS rental_outcomes_hourly_pkey;
ALTER TABLE rental_outcomes_hourly
    ADD CONSTRAINT rental_outcomes_hourly_pkey
    PRIMARY KEY (hour, h3_9, model, radius_m, equity_area);

-- Every writer from here on must say which; no silent default.
ALTER TABLE rental_outcomes_hourly ALTER COLUMN equity_area DROP DEFAULT;

COMMENT ON COLUMN rental_outcomes_hourly.equity_area IS
    'Official Equity Area of the unlock point (EQ_nnn), outside, unknown (no '
    'unlock point to test), or unrecorded (before sql/092). See sql/092.';

RESET lock_timeout;
