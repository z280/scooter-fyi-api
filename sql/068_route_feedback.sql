-- Route feedback from rides the survey can't reach (sql/052's Screen 9 is
-- keyed to a tracked ride).
--
-- WHY THIS TABLE EXISTS ------------------------------------------------------
-- Screen 9's navigation pane — route rating, deviation, NPS, free text —
-- posts to /tracked-rides/{id}/survey, which is owner-only and single-shot
-- per TRACKED ride. A "My own Device" or guest ride is private by
-- definition: no tracked_rides row, no ride id, and therefore no way to say
-- "the shade route sent me down a staircase" even though the rider chose a
-- route and rode it. Those riders' navigation opinions are exactly as real
-- as anyone else's — arguably more valuable, since their own vehicles rule
-- out the scooter itself as the thing being reviewed.
--
-- This table takes the SAME navigation answers, minus everything that
-- presupposes a server ride: no tracked_ride_id, no ride_routes linkage, no
-- awards (private rides are never points-eligible — sql/028's "points are
-- never anonymous" and the private-ride rule both hold). The route is
-- described inline (profile + the client's distance/duration figures)
-- because for a private ride no ride_routes row was ever written.
--
-- NO GEOMETRY, ON PURPOSE: profile + distance is coarse enough that this
-- table stays outside the master's "fine geometry loses account linkage
-- within <=28h" rule (sql/052's de-id sweep) — nothing here needs sweeping.
-- Keep it that way; a lat/lon column added here buys a de-id arm with it.
--
-- account_id is nullable both because anonymous feedback is allowed (same
-- stance as device reports) and ON DELETE SET NULL so closing an account
-- doesn't erase routing knowledge the rider contributed.

CREATE TABLE IF NOT EXISTS route_feedback (
    id                  BIGSERIAL PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    account_id          BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
    reporter_ip         TEXT,
    reporter_user_agent TEXT,
    -- A config.json valhalla.profiles key (safe|range|shade|express today).
    -- TEXT, not an enum: profiles are config, and a feedback row about a
    -- profile that was later renamed is still evidence.
    route_profile       TEXT NOT NULL,
    -- The client's own figures for the route it rated — display-grade
    -- context, not measurements (there is no server route row to join).
    distance_m          DOUBLE PRECISION CHECK (distance_m >= 0),
    duration_s          DOUBLE PRECISION CHECK (duration_s >= 0),
    -- The survey's navigation vocabulary, verbatim (sql/052 ride_surveys):
    -- same names, same ranges, so cross-table analysis needs no mapping.
    nav_route_rating    INTEGER CHECK (nav_route_rating BETWEEN 1 AND 10),
    nav_deviated        BOOLEAN,
    nav_deviated_needs_improvement BOOLEAN,
    nav_nps             INTEGER CHECK (nav_nps BETWEEN 0 AND 10),
    nav_qualitative     TEXT CHECK (char_length(nav_qualitative) <= 2000)
);

CREATE INDEX IF NOT EXISTS idx_route_feedback_created
    ON route_feedback (created_at);
CREATE INDEX IF NOT EXISTS idx_route_feedback_profile
    ON route_feedback (route_profile, created_at);
