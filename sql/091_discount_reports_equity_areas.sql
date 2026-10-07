-- Missed-discount reports against the city's OFFICIAL Equity Area map.
--
-- sql/013 limited discount_reports.zone_version to 'v1' / 'v2', the two
-- disadvantaged-area layers this app estimated before the city published
-- which areas bind the Veo contract (August 2026). Those layers are retired
-- in the frontend; the map a rider now sees, and the one the daily
-- compliance numbers use, is the official Equity Area map. A report from it
-- is zone_version 'equity'.
--
-- region_name records WHICH Equity Area ("EQ_014"), so a reviewer can check
-- the claim against the boundary without reverse-geocoding the end point.
-- It is a public area identifier, not personal data. NULL for v1/v2 rows
-- and for clients that do not send it.

SET lock_timeout = '10s';

ALTER TABLE discount_reports
    DROP CONSTRAINT IF EXISTS discount_reports_zone_version_check;

ALTER TABLE discount_reports
    ADD CONSTRAINT discount_reports_zone_version_check
    CHECK (zone_version IN ('v1', 'v2', 'equity'));

ALTER TABLE discount_reports
    ADD COLUMN IF NOT EXISTS region_name TEXT;

ALTER TABLE discount_reports
    DROP CONSTRAINT IF EXISTS discount_reports_region_name_check;

ALTER TABLE discount_reports
    ADD CONSTRAINT discount_reports_region_name_check
    CHECK (region_name IS NULL OR region_name ~ '^EQ_[0-9]{3}$');

COMMENT ON COLUMN discount_reports.region_name IS
    'Official Equity Area the reported ride ended in (EQ_NNN), when zone_version = ''equity''.';

RESET lock_timeout;
