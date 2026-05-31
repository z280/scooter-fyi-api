-- Citizen-submitted negative reports about a scooter at a location.
--
-- Public POST endpoint at /api/v1/reports — no auth, no rate limiting
-- in this iteration (a follow-up will add per-IP rate limit + consensus
-- surfacing before public launch). Stored verbatim; the public
-- display layer only surfaces a report when the device is STILL in
-- the same h3_10 cell as the report position AND the report is ≤24h
-- old. Older reports remain queryable from the private endpoint.
--
-- problem_tags is a free-form text[] for now. Validation will land
-- when we know what the canonical tag set looks like in practice.

CREATE TABLE IF NOT EXISTS negative_reports (
    id                   BIGSERIAL PRIMARY KEY,
    reported_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Vehicle resolution: at most one of these is supplied by the caller,
    -- and we always resolve to and store BOTH for downstream queries.
    vehicle_identifier   TEXT,
    vehicle_plate        TEXT,
    -- Position at the time of reporting (the reporter's clicked location).
    -- Required.
    report_lat           DOUBLE PRECISION NOT NULL,
    report_lon           DOUBLE PRECISION NOT NULL,
    -- H3 cells stored alongside each report. ALWAYS computed server-side
    -- (any caller-supplied h3_*_index values are discarded). The canonical
    -- "where this complaint applies" cell is the SCOOTER's current h3 at
    -- report time — looked up from device_state when we know the device
    -- — falling back to the reporter's lat/lon if we don't. This means
    -- has_negative_report on /devices/current matches even when the
    -- reporter stood a few meters off the scooter (across a res-10 cell
    -- boundary).
    h3_8_index           BIGINT NOT NULL,
    h3_9_index           BIGINT NOT NULL,
    h3_10_index          BIGINT NOT NULL,
    -- Free-form for now; will gain a validating join in a future migration.
    problem_tags         TEXT[] NOT NULL DEFAULT '{}',
    problem_description  TEXT,
    -- Provenance (no PII, just instrumentation)
    reporter_ip          INET,
    reporter_user_agent  TEXT
);

CREATE INDEX IF NOT EXISTS idx_negative_reports_vehicle_identifier
    ON negative_reports (vehicle_identifier);
CREATE INDEX IF NOT EXISTS idx_negative_reports_h3_10
    ON negative_reports (h3_10_index);
CREATE INDEX IF NOT EXISTS idx_negative_reports_reported_at
    ON negative_reports (reported_at DESC);
-- Composite for the "live report at current location" lookup on
-- /api/v1/devices/current (vehicle_identifier + h3_10_index + reported_at).
CREATE INDEX IF NOT EXISTS idx_negative_reports_live_lookup
    ON negative_reports (vehicle_identifier, h3_10_index, reported_at DESC);
