-- First-party user analytics: frontend behavioral events + per-request API
-- metrics, plus the daily rollups the admin dashboard reads.
--
-- NAMING: "telemetry" here means USER telemetry (what people do in the web
-- app). It is unrelated to the vehicle-ingest sense of the word already in
-- this schema (raw_telemetry_points, the GBFS point archive). Nothing in
-- this migration reads or joins vehicle data.
--
-- PRIVACY SHAPE — the promises in src/api_meta.py's _PRIVACY payload are
-- kept structurally, not procedurally:
--
--   * No account_id column exists on any table here. Signed-in state is a
--     boolean, so events can never be joined back to an account.
--   * No IP and no raw user-agent is stored. The only visitor identity is
--     visitor_hash = sha256(daily_salt || ip || ua), computed at ingest.
--     The salt lives in telemetry_salt for two days (yesterday's rollup
--     needs yesterday's salt to still exist while it runs) and is then
--     deleted by the cleanup_telemetry cron — after which the hash cannot
--     be recomputed by anyone, us included. Distinct-visitor counts stay;
--     re-identification does not.
--   * Event props are validated against bounded vocabularies in
--     src/api_telemetry.py (allowlisted names, capped key counts and value
--     lengths — product limits live in code, per house convention). No
--     free text, no coordinates, no ride content, no preference values.
--
-- CARDINALITY: request_metrics.route stores the ROUTE TEMPLATE
-- ("/api/v1/devices/{device_id}"), never the raw path, so the column's
-- distinct values are bounded by the number of routes in the app.
--
-- city_id is carried per MULTI_TENANCY_PLAN.md §9: nullable, NULL = Denver
-- today, populated when multi-city lands — a tenant DIMENSION for
-- analytics, not a scoping key.
--
-- Raw tables are plain heaps, no partitioning: at this deployment's
-- traffic a daily-pruned table with two indexes is far below the size
-- where partitions pay for themselves. Retention (cleanup_telemetry in
-- src/cli.py, crontab 03:45): telemetry_events 90 days, request_metrics
-- 30 days, telemetry_salt 2 days. The *_daily rollups are aggregate and
-- identity-free and are kept indefinitely.

CREATE TABLE IF NOT EXISTS telemetry_salt (
    day  DATE PRIMARY KEY,
    salt TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry_events (
    id               BIGSERIAL PRIMARY KEY,
    city_id          BIGINT,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    name             TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    visitor_hash     TEXT NOT NULL,
    device_class     TEXT NOT NULL DEFAULT 'other',
    os_family        TEXT NOT NULL DEFAULT 'other',
    viewport         TEXT NOT NULL DEFAULT 'other',
    referrer_host    TEXT NOT NULL DEFAULT 'direct',
    -- Client-reported "a session token was present in the browser" — a
    -- population split, not an authentication claim.
    is_authenticated BOOLEAN NOT NULL DEFAULT FALSE,
    props            JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS telemetry_events_received_at_idx
    ON telemetry_events (received_at);
CREATE INDEX IF NOT EXISTS telemetry_events_name_received_at_idx
    ON telemetry_events (name, received_at);

CREATE TABLE IF NOT EXISTS request_metrics (
    id               BIGSERIAL PRIMARY KEY,
    city_id          BIGINT,
    at               TIMESTAMPTZ NOT NULL,
    route            TEXT NOT NULL,
    method           TEXT NOT NULL,
    status           SMALLINT NOT NULL,
    duration_ms      INTEGER NOT NULL,
    device_class     TEXT NOT NULL DEFAULT 'other',
    os_family        TEXT NOT NULL DEFAULT 'other',
    -- "The request PRESENTED a bearer token", not "the token was valid":
    -- the middleware runs before auth dependencies, so validity is
    -- unknowable at capture time. Good enough for an authed/anon split.
    is_authenticated BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS request_metrics_at_idx
    ON request_metrics (at);
CREATE INDEX IF NOT EXISTS request_metrics_route_at_idx
    ON request_metrics (route, at);

-- Daily rollups, recomputed idempotently by `python -m src.cli
-- rollup_analytics` (ON CONFLICT full-replace, same contract as
-- daily_trips). city_id joins the key via COALESCE(-1) in a unique index
-- because Postgres treats NULLs as distinct in plain UNIQUE constraints.
--
-- prop_summary holds top-k value counts per prop key for the day, e.g.
-- {"drawer": {"filters": 812, "areas": 240}} — enough to answer "which
-- drawer/mode/action" after the raw rows are pruned.

CREATE TABLE IF NOT EXISTS telemetry_daily (
    day          DATE NOT NULL,
    city_id      BIGINT,
    name         TEXT NOT NULL,
    prop_summary JSONB NOT NULL DEFAULT '{}',
    events       INTEGER NOT NULL DEFAULT 0,
    visitors     INTEGER NOT NULL DEFAULT 0,
    sessions     INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS telemetry_daily_key_idx
    ON telemetry_daily (day, name, COALESCE(city_id, -1));

CREATE TABLE IF NOT EXISTS request_metrics_daily (
    day             DATE NOT NULL,
    city_id         BIGINT,
    route           TEXT NOT NULL,
    method          TEXT NOT NULL,
    -- '2xx' | '3xx' | '4xx' | '5xx'
    status_class    TEXT NOT NULL,
    requests        INTEGER NOT NULL DEFAULT 0,
    p50_ms          INTEGER NOT NULL DEFAULT 0,
    p95_ms          INTEGER NOT NULL DEFAULT 0,
    authed_requests INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS request_metrics_daily_key_idx
    ON request_metrics_daily (day, route, method, status_class, COALESCE(city_id, -1));
