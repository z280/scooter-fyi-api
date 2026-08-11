-- Denver's authoritative address points, so "1226 E 10th Ave" resolves to a
-- door instead of a five-mile avenue.
--
-- WHY THIS EXISTS. Photon indexes OSM, and OSM does not have Denver's
-- addresses. Checked directly against the routing extract: East 10th Avenue
-- carries address nodes for 607, 609, 611, 613, 1412, 1424 and 3009 — and not
-- 1226. Citywide there are 250 addr:interpolation ways, none of them tagged
-- with a street. So the earlier diagnosis ("Photon cannot interpolate") was
-- only half right: there is nothing to interpolate. A geocoder cannot find an
-- address that was never mapped.
--
-- Denver publishes one: ODC_CITY_LOC_ADDRESSPUBLIC_P, 413,405 points, already
-- decomposed into number / predirectional / street / type, with coordinates.
-- Same class of city open data as the high-injury network and the tree canopy
-- this service already consumes.
--
-- THE SHAPE IS THE POINT. Two tables, not one, because the two halves of an
-- address want opposite treatment:
--
--   * STREET NAMES are few — 909 distinct, a few thousand once direction and
--     type are combined — and are what a rider misspells, abbreviates and
--     types half of. They want fuzzy, prefix-tolerant matching.
--   * HOUSE NUMBERS are many (413k) and are never approximate. Nobody means
--     1228 when they type 1226.
--
-- So: fuzzy-match the small table, then look the number up exactly against the
-- large one. Trigram-searching 413k full address strings would be both slower
-- and worse, because it would happily return the right number on the wrong
-- street — the exact failure that took the geocoder down earlier today.

CREATE TABLE IF NOT EXISTS address_streets (
    id               BIGSERIAL PRIMARY KEY,
    -- Canonical, uppercased components. The source is inconsistent about case
    -- ("10th", "109TH", "101ST" all appear) and carries NULL street names, so
    -- nothing here is trusted raw.
    predirectional   TEXT,
    street_name      TEXT NOT NULL,
    posttype         TEXT,
    postdirectional  TEXT,
    -- What the matcher actually compares against: components joined, stripped
    -- of punctuation and collapsed. "E 10TH AVE".
    search_key       TEXT NOT NULL,
    -- The street name alone, for prefix search while the rider is still
    -- typing and has not reached the "Ave" yet.
    name_key         TEXT NOT NULL,
    -- How to show it back: "E 10th Ave".
    display_name     TEXT NOT NULL,
    point_count      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (predirectional, street_name, posttype, postdirectional)
);

CREATE INDEX IF NOT EXISTS idx_address_streets_search ON address_streets (search_key);
CREATE INDEX IF NOT EXISTS idx_address_streets_name   ON address_streets (name_key);

CREATE TABLE IF NOT EXISTS address_points (
    id             BIGSERIAL PRIMARY KEY,
    street_id      BIGINT NOT NULL REFERENCES address_streets(id) ON DELETE CASCADE,
    -- Kept as an INTEGER for ordering and range work, and as text for the
    -- oddities: "1226A", "1/2", prefixed numbers. Both are populated.
    number         INTEGER,
    number_text    TEXT NOT NULL,
    unit           TEXT,
    lat            DOUBLE PRECISION NOT NULL,
    lon            DOUBLE PRECISION NOT NULL,
    address_type   TEXT,
    full_address   TEXT
);

-- The lookup this whole design exists to make instant: street then number.
CREATE INDEX IF NOT EXISTS idx_address_points_street_number
    ON address_points (street_id, number);

-- Reverse lookup ("what is this pin near?") scans a bounding box before
-- measuring distance, since there is no PostGIS here.
CREATE INDEX IF NOT EXISTS idx_address_points_latlon ON address_points (lat, lon);

-- When the dataset was last rebuilt, so /api/v1/meta can say how fresh the
-- address index is and a stale one is visible rather than silent — the same
-- failure mode that let Geofabrik go four days stale unnoticed.
COMMENT ON TABLE address_points IS
    'Denver open data ODC_CITY_LOC_ADDRESSPUBLIC_P. Rebuilt by '
    'src.cli refresh_address_points; freshness recorded in system_state under '
    'address_points_refreshed_at.';
