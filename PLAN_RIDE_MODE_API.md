# Ride Mode Overhaul — API Plan (scooter-fyi-api)

Companion to `RIDE_MODE_OVERHAUL_PLAN.md` (the master program plan — read it first; the vision,
glossary, chain-format spec, sequencing graph, and risks live there). This document is the
actionable API-side plan: **four big phases (A1–A4)**, each independently mergeable and deployable,
each divisible into parallel lanes for multiple implementing agents.

House rules that bind every phase:

- Migrations are idempotent, applied in sorted order at boot, recorded in `schema_migrations`.
  Never put an inline `CHECK` inside `ADD COLUMN IF NOT EXISTS` (silently skipped when the column
  exists) — use the named-constraint guard shape from `sql/040`–`042`: read `pg_constraint`, drop
  the auto-named check, `ADD CONSTRAINT <explicit_name> CHECK (...)`, and backfill data **before**
  adding any constraint that requires it (`sql/041` ordering rule).
- Tests: fake-cursor unit tests by default; `_pg.py` files are integration tests gated on
  `VEO_TEST_PG_DSN`. One test file per module. `tests/test_migration_replay_pg.py` must keep
  passing (replay idempotence).
- Per-PR doc duties (FEATURE_PLAN "Sequencing"): endpoint table row in `README.md`, full shapes +
  error codes in `API.md`, new env vars in both `.env.example` and `docker-compose.yml`, an
  `API_REQUIREMENTS.md` status-table row, and a comment block in `crontab` for any new job.
- **Three-address rule** (`src/api_meta.py` header): any new stored field is a retention rule —
  update `src/cli.py` (cleanup/de-id jobs), `src/api_meta.py:_PRIVACY`, and
  `src/templates/legal/privacy_policy.html` together.
- **Even-points invariant (owner rule)**: every point award must be even. Enforced three ways in
  this program: `CHECK (points % 2 = 0)` on `user_points` (sql/052), an assertion in
  `credit_points()`, and a unit test sweeping every `POINTS_*` constant and formula output.

Migration ownership: this program ships `sql/046`, `047`, `048`–`052`. `sql/045` stays reserved for
SMS sign-in (FEATURE_PLAN §9) and is not touched.

---

## Phase A1 — Ride session foundation, contracts & geocoding

**Goal:** everything frontend F2/F3 needs to run the wizard and record locally: per-ride signing
material, ride options storage, §10 reported fields, turn-by-turn maneuvers, self-hosted
geocoding, pricing/points metadata, and Usuals.

**Migrations: `sql/046`, `sql/048`, `sql/049`**

- `sql/046_tracked_rides_reported_fields.sql` — exactly as FEATURE_PLAN §10:
  `reported_minutes INTEGER` (0–1440, named CHECK), `reported_plan TEXT`
  (`resident|visitor|equity`, named CHECK).
- `sql/048_ride_sessions.sql`:

  ```sql
  ALTER TABLE tracked_rides
      ADD COLUMN IF NOT EXISTS track_nonce                    TEXT,        -- 16-byte hex, server random
      ADD COLUMN IF NOT EXISTS track_key                      TEXT,        -- base64url 32-byte HMAC key
      ADD COLUMN IF NOT EXISTS track_key_issued_at            TIMESTAMPTZ,
      ADD COLUMN IF NOT EXISTS reported_start_battery_percent NUMERIC(4,1),
      ADD COLUMN IF NOT EXISTS feed_start_battery_percent     INTEGER,     -- stamped from device_state at start
      ADD COLUMN IF NOT EXISTS ride_options                   JSONB NOT NULL DEFAULT '{}',
      ADD COLUMN IF NOT EXISTS validation_status              TEXT NOT NULL DEFAULT 'pending',
      ADD COLUMN IF NOT EXISTS validation_reasons             JSONB NOT NULL DEFAULT '[]',
      ADD COLUMN IF NOT EXISTS validated_at                   TIMESTAMPTZ;
  -- separate guarded named constraints (sql/040 shape):
  --   tracked_rides_reported_start_battery_range  CHECK (reported_start_battery_percent IS NULL
  --                                                      OR (reported_start_battery_percent BETWEEN 0 AND 100))
  --   tracked_rides_validation_status_allowed     CHECK (validation_status IN
  --       ('pending','pending_feed','eligible','ineligible','error'))
  ```

  `ride_options` blob (client-owned; server echoes it back and reads only the booleans it gates
  on): `{"cost_hud":bool, "speedometer":"classic"|"digital"|"none",
  "theme":"light"|"dark"|"auto", "navigation":bool, "save_tracks":bool, "battery_modeling":bool,
  "nav_improvement":bool, "end_survey":bool, "own_device":bool}`. Size cap 4 KB, enforced in the
  handler.
- `sql/049_ride_mode_usuals.sql` — drop/re-add `user_preferences_kind_allowed` adding
  `'ride_mode_usual'`; extend `user_preferences_name_matches_kind` so `ride_mode_usual` requires a
  name; partial unique index
  `idx_user_prefs_usual_name ON user_preferences (account_id, name) WHERE kind = 'ride_mode_usual'`.
  Cardinality cap `MAX_RIDE_USUALS = 10` per account, enforced in `src/api_preferences.py` exactly
  like `MAX_SAVED_MAP_SETTINGS`.

**Endpoint work**

- `POST /api/v1/tracked-rides` (changed): request gains optional
  `reported_start_battery_percent` (0–100) and `ride_options`. Handler stamps
  `feed_start_battery_percent` from `device_state`, generates `track_nonce` (16 random bytes hex)
  and `track_key` (32 random bytes base64url). Response gains:

  ```json
  "track_signing": {"alg": "HS256", "key_id": "<ride_id>", "key": "<b64url 32B>",
                    "nonce": "<hex>", "issued_at": "..."},
  "validation": {"status": "pending", "reasons": []}
  ```

  `GET .../active` and `GET .../{id}` (already owner-only) return the same `track_signing` block so
  a reloaded client can resume signing; `_row_to_ride` gains `ride_options` + `validation`.
  `track_signing` **never** appears in list responses.
- `PATCH /api/v1/tracked-rides/{ride_id}/end` (changed): accepts §10 `reported_minutes`,
  `reported_plan`; computes a provisional `validation_status`. (Award supersession happens in A2 —
  until A2 lands, existing awards keep working unchanged.)
- `GET /api/v1/route` (changed): new query param `maneuvers=true` adds `properties.maneuvers`:

  ```json
  [{"instruction": "Turn right onto Champa Street", "type": 10,
    "street_names": ["Champa Street"], "length_meters": 412.0, "time_seconds": 96.0,
    "begin_shape_index": 14, "end_shape_index": 22}]
  ```

  Implementation: new `valhalla.trip_maneuvers(trip)`. **Load-bearing subtlety**: `trip_shape()`
  concatenates legs and drops the duplicated shared vertex between legs, so leg-local maneuver
  shape indices must be re-offset by `(cumulative points so far − legs joined so far)` when
  flattening. A dedicated test covers a multi-leg trip.
  Also close the standing gap: `ratelimit.enforce` by client IP — bucket `route_ip` 30/min,
  `route_profiles_ip` 60/min. 429 carries `Retry-After` like existing buckets.
- `GET /api/v1/geocode/search` (new, `src/api_geocode.py`; public; bucket `geocode_ip` 20/min/IP):
  params `q` (2–100 chars), `lat`/`lon` (optional bias), `limit` (≤8, default 6). Proxies
  `GET http://photon:2322/api?q=&lat=&lon=&limit=&bbox=<map bounds>`, normalizes to:

  ```json
  {"results": [{"label": "1701 Champa St, Denver", "lat": 39.747, "lon": -104.992,
                "kind": "house|street|poi|locality", "in_coverage": true}]}
  ```

  `in_coverage` = point inside the routing `graph_bbox` (so the client greys out un-routable picks
  instead of failing at Screen 4). In-process LRU cache (512 entries, 24 h TTL) keyed on
  `(normalized q, round(lat,2), round(lon,2))`. Sidecar timeout 3 s → 503
  `{"error": "geocoder_unavailable"}`. Config block: `"geocode": {"upstream":
  "http://photon:2322", "enabled": true}` — upstream swappable by config alone.
- `GET /api/v1/meta/pricing` (new; public): `{"tax_rate": <n>, "currency": "USD", "as_of": "..."}`
  from a new `config.json` `"pricing"` block. The frontend bakes the same default for offline use.
  Rate plans themselves stay client-side (unchanged).
- `GET /api/v1/points/schedule` (new; public): the authoritative action → points map including
  formulas, e.g. `{"battery_contribution": {"base": 8, "per_step": 2, "step_km": 2}, ...}`,
  generated from `src/points.py` constants so UI copy can never drift. A1 ships current values;
  A2 adds the new actions.
- Usuals (`src/api_preferences.py`, mirroring the map-settings pattern):
  `GET /api/v1/profile/ride-usuals` (list), `GET/PUT/DELETE /api/v1/profile/ride-usuals/{name}`.
  Blob = `ride_options` + `label`; 16 KB cap reused; cap 10 per account (409 at cap).

**Photon sidecar (self-hosted geocoding)** — mirror the Valhalla pattern already in
`docker-compose.yml` (expose-only sidecar; one-shot R2 fetch gated by
`service_completed_successfully`; named volume; mem limits; cli fetch/refresh commands):

- `docker/photon/Dockerfile`: `eclipse-temurin:21-jre` + a **pinned, checksum-verified** Photon
  release jar. No third-party community image in the trust chain.
- Compose services: `photon_index_fetch` (one-shot, worker image,
  `python -m src.cli fetch_photon_index`, 256 MiB, volume `photon_files`) and `photon`
  (build `./docker/photon`, `expose: "2322"`, healthcheck on `/status`, `-Xmx1536m`,
  `mem_limit: 2048m`, `depends_on: photon_index_fetch: service_completed_successfully`). New named
  volume `photon_files`. `docker-compose.override.yml.example` gains a host-port mapping for local
  dev. Nothing is publicly exposed; riders reach it only through `/api/v1/geocode/search`.
- Index artifact: `photon/photon-index-<YYYYMMDD>.tar.zst` in the existing private `R2_MAP_BUCKET`
  (same scoped token as the Valhalla assets). `fetch_photon_index` mirrors `fetch_map_pbf`: ETag
  marker in the volume, download + unpack only on change, loud log that the `photon` container must
  be restarted to load a new index.
- Seeding runbook `scripts/build_photon_index.md` (manual, one-time + quarterly): Geofabrik
  `colorado-latest.osm.pbf` (~250 MB) → throwaway Nominatim import container →
  `photon -nominatim-import` → tar `photon_data/` → upload to R2. Colorado-scoped keeps the index
  low-GB and the JVM under 2 GiB; a full-US extract is explicitly rejected.
- Cron: `0 5 * * *` `refresh_photon_index` (ETag check, no-op most days — same shape as
  `refresh_routing_graph` at 04:30).

**Files:** `sql/046`, `sql/048`, `sql/049`, `src/api_tracked_rides.py`, `src/api_route.py`,
`src/valhalla.py`, new `src/api_geocode.py`, `src/api_meta.py`, `src/api_points.py`,
`src/api_preferences.py`, `src/cli.py`, `config.json`, `src/config.py`, `docker-compose.yml`,
`docker-compose.override.yml.example`, new `docker/photon/Dockerfile`, new
`scripts/build_photon_index.md`, `crontab`, `API.md`, `README.md`, `.env.example`,
`API_REQUIREMENTS.md`.

**Tests:** extend `tests/test_api_tracked_rides_validation.py` (options blob shape + 4 KB cap,
start-battery bounds, §10 round-trip); new `tests/test_route_maneuvers.py` (**multi-leg index
re-offset**), `tests/test_geocode_proxy.py` (normalization, rate limit, sidecar-down 503, bbox/bias
params, cache), `tests/test_ride_usuals.py` (CRUD, cap 409, name rules),
`tests/test_meta_pricing.py`, `tests/test_points_schedule.py`, `tests/test_cli_photon_fetch.py`
(ETag no-op / refresh); extend the `_pg` tracked-rides lifecycle test with a `track_signing`
round-trip (start → GET active returns the same key).

**Acceptance:** start → `GET active` returns the same signing key; `/route?maneuvers=true` indices
address the returned LineString exactly on a multi-leg route; `/geocode/search` returns
Denver-biased results with correct `in_coverage`; Usuals round-trip; migration replay green.

**Parallel lanes (5):** ① sessions/signing + §10 fields ② maneuvers + route rate limits
③ Photon (compose + Dockerfile + cli + runbook + proxy endpoint) ④ Usuals ⑤ pricing + points
schedule.

---

## Phase A2 — Donation, verification, validation, points reshape, de-id

**Goal:** the contribution funnel — bulk donation of the signed chain, server verification,
validation states that drive Screen 10, the new points economy, battery-model ingestion, and the
de-identification sweep.

**Migrations: `sql/050`, `sql/052`**

- `sql/050_track_donations.sql`:

  ```sql
  CREATE TABLE IF NOT EXISTS track_donations (
      id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tracked_ride_id    UUID   REFERENCES tracked_rides(id) ON DELETE CASCADE,  -- nulled by de-id
      account_id         BIGINT REFERENCES accounts(id)      ON DELETE CASCADE,  -- nulled by de-id
      vehicle_model      TEXT,                        -- kept post-de-id (battery model needs it)
      chain_root_hash    TEXT NOT NULL,               -- final rolling hash, audit anchor
      batch_count        INTEGER NOT NULL,
      waypoint_count     INTEGER NOT NULL,
      distance_meters    DOUBLE PRECISION,            -- clamped haversine, server-computed
      verification       JSONB NOT NULL DEFAULT '{}', -- per-check results + reasons
      points_awarded     INTEGER NOT NULL DEFAULT 0,
      donated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      points_settled_at  TIMESTAMPTZ,
      deidentified_at    TIMESTAMPTZ
  );
  CREATE UNIQUE INDEX IF NOT EXISTS idx_track_donations_ride
      ON track_donations (tracked_ride_id) WHERE tracked_ride_id IS NOT NULL;  -- one donation per ride
  CREATE INDEX IF NOT EXISTS idx_track_donations_deid
      ON track_donations (points_settled_at) WHERE deidentified_at IS NULL;

  CREATE TABLE IF NOT EXISTS donated_track_points (
      donation_id  UUID NOT NULL REFERENCES track_donations(id) ON DELETE CASCADE,
      seq          INTEGER NOT NULL,
      recorded_ms  BIGINT NOT NULL,        -- client epoch ms; de-id coarsens to minute
      lat          DOUBLE PRECISION NOT NULL CHECK (lat BETWEEN -90 AND 90),
      lon          DOUBLE PRECISION NOT NULL CHECK (lon BETWEEN -180 AND 180),
      accuracy_m   REAL,
      PRIMARY KEY (donation_id, seq)
  );

  ALTER TABLE battery_trip_observations ADD COLUMN IF NOT EXISTS source TEXT;
  -- guarded named CHECK: source IS NULL OR source IN ('feed_mined','donated_ride')
  ```

  Raw JWS strings are **discarded after verification** — only `chain_root_hash` and the
  `verification` summary persist (retained signatures add nothing once verified and would be one
  more identifying artifact to sweep).
- `sql/052_ride_mode_points.sql` — drop/re-add `user_points_action_allowed` adding
  `'battery_contribution'`, `'nav_route_feedback'`, `'nav_qualitative_feedback'`,
  `'nav_distance_bonus'`, `'ride_survey'` (old values stay — historical rows are forever); add
  named CHECK `user_points_points_even CHECK (points % 2 = 0)` (all historical rows are already
  even: 2/4/10/20/100).

**Endpoint work**

- `POST /api/v1/tracked-rides/{ride_id}/track` (new; owner-only; bucket
  `track_donation_account` 6/h; body cap 2 MB; ≤600 batches). Request:
  `{"batches": ["<compact JWS>", ...]}`. Preconditions: ride ended (`user_reported_ended_at` set),
  no prior donation (409 `already_donated`), `ride_options.save_tracks` was on (422
  `tracking_not_opted`). Pipeline: verify chain (below) → write `track_donations` +
  `donated_track_points` → recompute `validation_status` → award points → ingest battery
  observation. Response:

  ```json
  {"donation_id": "...",
   "verification": {"chain": "ok", "monotonic": "ok", "speed": "ok",
                    "gbfs_start": "ok", "gbfs_end": "pending_feed", "volume": "ok"},
   "validation": {"status": "eligible", "reasons": []},
   "distance_meters": 4312.5, "waypoint_count": 512,
   "points": [{"action": "battery_contribution", "points": 14}]}
  ```

  Errors: 422 `chain_invalid` (with the failing check + batch seq), 409, 413, 404, 429. If GBFS
  hasn't resolved yet (`status='left_feed'`): donation accepted, distance-dependent points held,
  `validation.status = "pending_feed"` — finished later by `finalize_validation` (below). This is
  Screen 10's "waiting on validation from the live feed" branch.
- `PATCH .../end`: stops calling `credit_waypoint_points` / `credit_gbfs_validation_points`
  (functions retained for history/tests; `API.md` documents the supersession).
- Deprecation: `POST .../waypoints` (600/h single-waypoint) remains for the legacy HUD until
  frontend F3 ships, then one more release with an `API.md` deprecation note. It stops awarding
  points as of A2. It is **not** the transport for ride-mode tracks — those never stream.

**Verification (`src/track_verify.py` — pure module, fake-cursor unit-testable):**
`verify_track_chain(cur, ride_row, batches) -> VerificationResult` with checks, in order:

1. **Signature** — recompute HMAC-SHA256 per batch with `track_key`; header/`rid`/`non` must match
   the ride. Any failure → `chain_invalid`.
2. **Chain integrity** — `seq` contiguous from 0; each `prev` equals sha256 of the predecessor's
   compact JWS; recomputed final `H_n` becomes `chain_root_hash`.
3. **Monotonicity + bounds** — `t0 ≤ t1` per batch; timestamps strictly increasing across the
   flattened track; `t0(first) ≥ started_at − 120 s`; `t1(last) ≤ user_reported_ended_at + 120 s`.
   `started_at` is server-stamped at key issuance, so a chain cannot claim to predate the ride.
4. **Physical plausibility** — per-segment speed = haversine/Δt; hard-reject any segment > 20 m/s
   after accuracy adjustment (subtract `acc_i + acc_j` from segment distance first); if >10% of
   segments exceed 11 m/s sustained → points status `pending_review` (flag, not reject). Distance
   uses the `_measured_path` clamping approach and respects the sql/041 caps.
5. **GBFS correlation** (primary anti-fabrication control — mirrors the
   `credit_gbfs_validation_points` geometry): first waypoint ≤150 m of `start_lat/lon`; when
   resolved, last waypoint ≤150 m of `gbfs_end_lat/lon` and `t1(last)` within ±10 min of
   `gbfs_reappeared_at`; ride start within 10 min of `gbfs_left_feed_at`. Unresolved feed →
   `pending_feed`.
6. **Volume** — `waypoint_count ≥ 10`, distance ≥ 500 m, duration ≥ 3 min; else
   `too_few_waypoints` / `trip_too_short`.

Reason vocabulary (Screen 10 renders directly from these): `start_mismatch`, `end_mismatch`,
`tracking_not_opted`, `too_few_waypoints`, `trip_too_short`, `chain_invalid`, `internal_error`;
plus status `pending_feed`.

**Points (`src/points.py`):**

```
POINTS_BATTERY_CONTRIBUTION_BASE = 8      # + 2 per started 2 km
POINTS_NAV_ROUTE_FEEDBACK        = 4
POINTS_NAV_QUALITATIVE           = 6      # even-points rule: owner corrected 5 -> 6
POINTS_NAV_DISTANCE_PER_STEP     = 2      # per started 3 km
POINTS_RIDE_SURVEY               = 4

battery_contribution = 8 + 2 * ceil(distance_m / 2000)   # source: track_donations row
nav_distance_bonus   =     2 * ceil(distance_m / 3000)   # source: track_donations row, requires ride_route
```

Preconditions: `battery_contribution` requires a verified donation + both batteries known +
`ride_options.battery_modeling` + not own-device. `nav_*` require `ride_options.nav_improvement` +
a `ride_routes` row. One ledger row per action (itemized, matching the ℹ copy); `lat/lng/h3` = the
ride start point (consistent with existing rows; exactly what §11 aggregates). All awards route
through `credit_points` → `_apply_ride_cap` with `source_table/source_id` dedupe; `credit_points`
gains an `assert points % 2 == 0`. Worst case (10 km ride, everything on): 18 + 18 + 4 = **40**,
comfortably under the unchanged `MAX_POINTS_PER_RIDE = 100`.

**Battery ingestion** — on a verified donation with both batteries known, insert a
`battery_trip_observations` row `source='donated_ride'`: distance = verified track distance;
elevation gain re-derived by map-matching via Valhalla `trace_attributes` (reuse the shade-scoring
trace path; NULL if the trace fails); burn = `feed_start_battery_percent` (fallback
`reported_start_battery_percent`) − `reported_battery_percent`; temperature from
`hourly_temperature`. The weekly `train_battery_model` consumes these unchanged — donated rows are
strictly better than feed-mined observation-gap rows.

**Validation finisher** — new `finalize_validation(cur, ride_id)` called from `ride_watch.py`'s
resolve path **and** the `expire_stale_watches` cron, so `pending_feed` rides settle (award or
`end_mismatch`) without user action.

**De-id sweep** — new cron `15 * * * *` `python -m src.cli deidentify_donations`: for
`track_donations` and their `ride_routes` where `points_settled_at < NOW() − INTERVAL '4 hours'`
**or** `donated_at < NOW() − INTERVAL '28 hours'` (force-floor even if points never settled): set
`account_id = NULL, tracked_ride_id = NULL, deidentified_at = NOW()`; coarsen
`donated_track_points.recorded_ms` to minute precision. Pre-de-id, `ON DELETE CASCADE` honors
hard-delete; post-de-id the artifact has no owner. Three-address rule updates: donated tracks
("de-identified ≤28 h after donation"), ride routes (same), surveys ("kept with the account,
deleted with it").

**Files:** `sql/050`, `sql/052`, new `src/track_verify.py`, `src/api_tracked_rides.py`,
`src/points.py`, `src/ride_watch.py`, `src/battery_model.py`, `src/cli.py`, `crontab`,
`src/api_meta.py`, `src/templates/legal/privacy_policy.html`, `API.md`, `README.md`,
`API_REQUIREMENTS.md`.

**Tests:** `tests/test_track_verify.py` (golden chains from the shared fixtures: valid,
flipped-bit, signed-with-foreign-key, reordered, teleport, out-of-bounds timestamps,
recovered-batch `rec:true`); `tests/test_track_donation_pg.py` (full lifecycle incl. pending_feed
settle); `tests/test_points_ride_mode.py` (ceil math, gates, cap interplay, supersession,
**even-points sweep over every constant + formula output**); `tests/test_cli_deidentify.py`
(4 h / 28 h boundaries exact, cascade-before/ownerless-after).

**Acceptance:** any single flipped bit or foreign-key-signed batch is rejected; superseded actions
are never awarded on new rides; de-id severs FKs and the artifact survives account deletion; the
even-points CHECK holds against all historical rows.

**Parallel lanes (4):** ① `track_verify.py` + golden vectors ② points module + supersession
③ de-id cron + three-address updates ④ battery ingestion + `finalize_validation`. The donation
endpoint integrates all four last.

---

## Phase A3 — Routes persistence & surveys

**Goal:** what Screen 4 stores and Screen 9 submits. Independent of A2 (both depend only on A1);
A2's `nav_distance_bonus` checks for a `ride_routes` row defensively, so landing order between
A2/A3 doesn't matter.

**Migration: `sql/051_ride_surveys_routes.sql`**

```sql
CREATE TABLE IF NOT EXISTS ride_routes (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tracked_ride_id           UUID   REFERENCES tracked_rides(id) ON DELETE CASCADE, -- nulled by de-id
    account_id                BIGINT REFERENCES accounts(id)      ON DELETE CASCADE, -- nulled by de-id
    profile                   TEXT NOT NULL,       -- safe|range|shade|express (validated vs config)
    origin_lat  DOUBLE PRECISION NOT NULL, origin_lon DOUBLE PRECISION NOT NULL,
    dest_lat    DOUBLE PRECISION NOT NULL, dest_lon  DOUBLE PRECISION NOT NULL,
    route_polyline            TEXT NOT NULL,       -- precision-5, src/polyline.py convention
    distance_meters           DOUBLE PRECISION,
    duration_seconds          DOUBLE PRECISION,
    battery_percent_estimate  DOUBLE PRECISION,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deidentified_at           TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS ride_surveys (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tracked_ride_id  UUID NOT NULL UNIQUE REFERENCES tracked_rides(id) ON DELETE CASCADE,
    account_id       BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    vehicle_model    TEXT,
    would_ride_again BOOLEAN,
    was_perfect      BOOLEAN,
    issues           JSONB NOT NULL DEFAULT '[]',   -- validated against the 16-item vocabulary
    model_bonus      JSONB NOT NULL DEFAULT '{}',
    nav_route_rating INTEGER CHECK (nav_route_rating BETWEEN 1 AND 10),
    nav_deviated     BOOLEAN,
    nav_deviated_needs_improvement BOOLEAN,
    nav_nps          INTEGER CHECK (nav_nps BETWEEN 0 AND 10),
    nav_qualitative  TEXT,                          -- free text, <=2000 chars (api-enforced)
    ride_route_id    UUID REFERENCES ride_routes(id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Surveys keep account linkage under normal hard-delete rules and are **not** de-identified (no
geometry). `ride_routes`, which has geometry, is de-identified by the A2 sweep.

**Endpoints**

- `POST /api/v1/ride-routes` (new; session; 30/h): request `{"tracked_ride_id": "...|null",
  "profile": "safe", "origin": [lat,lon], "destination": [lat,lon], "route_polyline": "...",
  "distance_meters": n, "duration_seconds": n, "battery_percent_estimate": n|null}` →
  `{"ride_route_id": "..."}`. The client calls it **only** when `nav_improvement` is on — that
  consent is what makes storing a route acceptable. 400 if the polyline decodes to <2 points or
  endpoints fall outside `graph_bbox`.
- `POST /api/v1/tracked-rides/{ride_id}/survey` (new; owner; single-shot — second POST 409):
  request mirrors `ride_surveys`. `issues` validated against the fixed vocabulary
  (`app_veo, acceleration, basket, battery, bell, brakes, connectivity, customer_service, dirty,
  kickstand, pedals, phone_holder, price, speedometer, scooterfyi_issue, vandalized`);
  `model_bonus` keys validated per `vehicle_model` (`cosmo_front_basket` bool /
  `apollo_top_speed_mph` numeric / `astro_landscape_holder` bool). Awards: `ride_survey` 4 (any
  scooter-feedback answer present ∧ `ride_options.end_survey` ∧ not own-device);
  `nav_route_feedback` 4 (`nav_route_rating` present ∧ `ride_route_id` resolves);
  `nav_qualitative_feedback` 6 (`nav_qualitative` ≥ 20 meaningful chars — length gate only).
  Response echoes the row + points array. Ride payloads gain a `survey_submitted` flag.

**Files:** `sql/051`, new `src/api_ride_surveys.py` (or extend `src/api_tracked_rides.py` — keep
the router count sane, implementer's choice), `src/api_rides_routes.py` (new router for
`/ride-routes`), `src/points.py` (survey award functions), `src/main.py` (mount), `API.md`,
`README.md`, `API_REQUIREMENTS.md`.

**Tests:** `tests/test_ride_routes.py` (validation, bbox, consent-only call pattern documented),
`tests/test_ride_surveys.py` (vocabulary, model-bonus matrix per model, single-shot 409, award
gates incl. own-device and 20-char qualitative threshold).

**Acceptance:** a survey with rating + qualitative + a stored route awards exactly 4+4+6; a second
POST 409s; issues outside the vocabulary 422.

**Parallel lanes (2):** ① ride-routes ② surveys + awards.

---

## Phase A4 — §11 H3 r8 Leaderboard

**Goal:** implement FEATURE_PLAN_2026-07.md §11 as specified — with one response-shape extension
for the frontend Leaderboard view. Independent of A2/A3 mechanics (reads the ledger only); can land
any time after A2 (earlier is harmless — new actions simply appear as they start being awarded).

**Migration: `sql/047_h3_r8_area_leaders.sql`** — exactly §11.2: `h3_r8_area_report`,
`h3_r8_area_leaders` (top **3** per cell), `h3_r8_area_leader_runs`, plus
`idx_user_points_h3_8_created`.

**Recompute:** `src/area_leaders.py:recompute(window_days=28)` per §11.3 — universe =
`DISTINCT h3_8_index FROM device_history` ∪ `device_state.current_h3_8_index` ∪
`user_points.h3_8_index`; only `status='confirmed'` rows; tie-break
`points DESC, first_point_at ASC, account_id ASC`; full-replace transaction (the
`daily_trips.compute_for_date` idiom); run row stamps the window. Cron: `15 9 * * *`
`recompute_area_leaders` with a crontab comment block.

**Endpoints:**

- `GET /api/v1/leaderboard/map` (public) — §11.4 **with one extension**: carry the full eligible
  top-3 per cell, not just the leader, so the choropleth *and* the click-through detail come from
  one fetch (no per-cell endpoint):

  ```json
  {"computed_at": "...", "window_start": "...", "window_end": "...",
   "cells": {
     "8828308281fffff": {
       "total_points": 143, "distinct_earners": 4,
       "leader":     {"display_name": "Duke Swift Otter 🦦", "royalty_title": "Duke",
                      "points": 88, "ruling_color": "#7b3ff2",
                      "ruling_border_color": "#4b21a0", "ruling_alpha": 0.6},
       "runners_up": [{"display_name": "...", "royalty_title": null, "points": 31,
                       "ruling_color": null, "ruling_border_color": null, "ruling_alpha": null}]
     },
     "8828308283fffff": {"total_points": 0, "distinct_earners": 0, "leader": null, "runners_up": []}
   }}
  ```

  Semantics (per §11.4, restated so the extension can't drift): cell keys are canonical **h3
  strings** (`h3.int_to_str` — JS `MAX_SAFE_INTEGER`); privacy applied at **read time** — accounts
  with `show_in_leaderboards` off (or hidden username per `show_public_username`) are omitted
  entirely and the next stored rank falls through into `leader`; `runners_up` = the remaining
  eligible stored ranks in order (`leader` + `runners_up` ≤ 3 total); colors live-joined from
  `accounts` and **null when unclaimed — the API never invents a default color** (frontend
  decision); `royalty_title` included so the detail view renders the composed name generously.
  ~720 cells × ≤3 entries ≈ 40 KB gzipped. ETag on latest `h3_r8_area_leader_runs.computed_at`,
  `Cache-Control: public, max-age=600` (same as `/h3/aggregates`).
- `GET /api/v1/private/area-leaders` (admin) per §11.4, unchanged.

**Files:** `sql/047`, new `src/area_leaders.py`, new `src/api_leaderboard.py`, `src/api_private.py`
(admin sibling), `src/cli.py`, `crontab`, `src/main.py` (mount), `API.md`, `README.md`,
`API_REQUIREMENTS.md`.

**Tests:** `tests/test_area_leaders_logic.py` (tie-break, window, confirmed-only),
`tests/test_area_leaders_pg.py` (universe union, full-replace idempotence, account-delete cascade),
`tests/test_api_leaderboard_map.py` (privacy fall-through to rank 2/3, runners-up omit ineligible,
all-opted-out → `leader: null`, ETag/304, h3 string keys, null-color passthrough).

**Acceptance:** with production-shaped data (a handful of ledger rows), the endpoint returns ~720
cells, nearly all `leader: null`, in one ETagged fetch; flipping `show_in_leaderboards` off hides
the account on the very next request without a recompute.

**Parallel lanes (2):** ① recompute + tables ② endpoint + privacy semantics.

---

## Cross-cutting: what the frontend is promised

The complete frontend-facing contract table lives in `RIDE_MODE_OVERHAUL_PLAN.md` §1.5. Any change
to a request/response shape in this plan must be reflected there (both repo copies) and in
`API.md` in the same PR.
