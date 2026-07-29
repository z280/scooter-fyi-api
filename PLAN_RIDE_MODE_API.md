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

- `sql/046_tracked_rides_reported_fields.sql` — FEATURE_PLAN §10's columns and semantics exactly
  (`reported_minutes INTEGER`, 0–1440; `reported_plan TEXT`, `resident|visitor|equity`; `EndRideIn`
  / `_RIDE_COLS` / `_row_to_ride` wiring per §10) — but **not** §10's published DDL, which inlines
  both CHECKs in `ADD COLUMN IF NOT EXISTS` in violation of the house rule above. Install them as
  separate guarded named constraints (`tracked_rides_reported_minutes_range`,
  `tracked_rides_reported_plan_allowed`) instead — the minutes bound behind the sql/041 step-4
  conname-only guard, the plan list behind the value-checked guard: the same two-shape split the
  sql/048 comment below spells out, and per sql/041 the two guards must not be made to match.
- `sql/048_ride_sessions.sql`:

  ```sql
  ALTER TABLE tracked_rides
      ADD COLUMN IF NOT EXISTS track_nonce                    TEXT,        -- 16-byte hex, server random
      ADD COLUMN IF NOT EXISTS track_key                      TEXT,        -- base64url 32-byte HMAC key
      ADD COLUMN IF NOT EXISTS track_key_issued_at            TIMESTAMPTZ,
      ADD COLUMN IF NOT EXISTS reported_start_battery_percent NUMERIC(4,1),
      ADD COLUMN IF NOT EXISTS feed_start_battery_percent     INTEGER,     -- derived from the feed at start (see POST below)
      ADD COLUMN IF NOT EXISTS feed_start_lat                 DOUBLE PRECISION,  -- vehicle's last feed position at start —
      ADD COLUMN IF NOT EXISTS feed_start_lon                 DOUBLE PRECISION,  --   the feed-anchored start for A2's check 5
      ADD COLUMN IF NOT EXISTS ride_options                   JSONB NOT NULL DEFAULT '{}',
      ADD COLUMN IF NOT EXISTS validation_status              TEXT NOT NULL DEFAULT 'pending',
      ADD COLUMN IF NOT EXISTS validation_reasons             JSONB NOT NULL DEFAULT '[]',
      ADD COLUMN IF NOT EXISTS validated_at                   TIMESTAMPTZ;
  -- separate guarded named constraints — two DIFFERENT guard shapes, per sql/041's
  -- step-3/step-4 rule (an enumerated list is guarded on its value so a replay can
  -- re-add a missing member; a numeric bound is guarded on conname ALONE so a later
  -- migration that moves the bound sticks):
  --   tracked_rides_reported_start_battery_range  (sql/041 step-4 shape, conname-only guard)
  --       CHECK (reported_start_battery_percent IS NULL
  --              OR (reported_start_battery_percent BETWEEN 0 AND 100))
  --   tracked_rides_validation_status_allowed     (sql/040/042 shape, value-checked guard)
  --       CHECK (validation_status IN
  --       ('pending','pending_feed','eligible','ineligible','error'))
  ```

  `ride_options` blob (client-owned; server echoes it back and reads only the booleans it gates
  on): `{"cost_hud":bool, "speedometer":"classic"|"digital"|"none",
  "theme":"light"|"dark"|"auto", "navigation":bool, "save_tracks":bool, "battery_modeling":bool,
  "nav_improvement":bool, "end_survey":bool, "own_device":bool}`. Size cap 4 KB, enforced in the
  handler.
- `sql/049_ride_mode_usuals.sql` — widen `user_preferences_kind_allowed` to add
  `'ride_mode_usual'` and extend `user_preferences_name_matches_kind` so `ride_mode_usual` requires
  a name — both via the sql/042 value-checked guard (read `pg_get_constraintdef`, rewrite only when
  `'ride_mode_usual'` is missing, so a replay after a later widening is a no-op; sql/043 named both
  constraints explicitly, so there is no auto-named twin to drop); partial unique index
  `idx_user_prefs_usual_name ON user_preferences (account_id, name) WHERE kind = 'ride_mode_usual'`.
  Cardinality cap `MAX_RIDE_USUALS = 10` per account, enforced in `src/api_preferences.py` exactly
  like `MAX_SAVED_MAP_SETTINGS`.

**Endpoint work**

- `POST /api/v1/tracked-rides` (changed): request gains optional
  `reported_start_battery_percent` (0–100) and `ride_options`. Handler stamps
  `feed_start_battery_percent` from the vehicle's latest feed observation —
  `quality.compute_battery_percent(current_range_meters)` off the newest `raw_telemetry_points`
  row, the exact idiom `ride_watch.py` uses to stamp `gbfs_end_battery_percent`; `device_state`
  stores no battery/range column (only `max_observed_range_meters`), so it cannot be the source.
  NULL when the feed has no fresh observation. The same newest-telemetry read also stamps
  `feed_start_lat`/`feed_start_lon` — a feed-anchored start position the rider cannot supply
  (A2's check 5 prefers it over the client-supplied `start_lat/lon`). Handler also generates
  `track_nonce` (16 random bytes hex) and `track_key` (32 random bytes base64url). Response gains:

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

  Implementation: new `valhalla.trip_maneuvers(trip)`. **Units**: `valhalla.route()` pins
  `directions_options.units: "kilometers"`, so each native maneuver's `length` arrives in
  **kilometers** — convert `×1000` to `length_meters` exactly as `trip_summary()` already does for
  `summary.length` (`time` is already seconds → `time_seconds`). **Load-bearing subtlety**:
  `trip_shape()` concatenates legs and drops the duplicated shared vertex between legs — but
  conditionally (only when the boundary vertex actually repeats, and empty-shape legs are skipped
  entirely) — so leg-local maneuver shape indices must be re-offset by the number of points already
  emitted before that leg, computed **in the same pass with the same conditional drop logic as
  `trip_shape()`**, not by assuming one dropped vertex per join. A dedicated test covers a
  multi-leg trip.
  Also close the standing gap: `ratelimit.enforce` by client IP — bucket `route_ip` 30/min,
  `route_profiles_ip` 60/min. Note both handlers are currently DB-free: `enforce` needs an open
  cursor, so each gains a short `connection()` block, keyed
  `key=real_client_ip(request) or "?"` (`src/client_ip.py` — `request.client.host` is the
  cloudflared loopback; the `device_report_ip` idiom). 429 carries `Retry-After` like existing
  buckets. `route_ip` 30/min accommodates Screen 4's four parallel profile fetches plus the
  ≤1/min off-route re-route.
- `GET /api/v1/geocode/search` (new, `src/api_geocode.py`; public; bucket `geocode_ip` 20/min/IP):
  params `q` (2–100 chars), `lat`/`lon` (optional bias), `limit` (≤8, default 6). Proxies
  `GET http://photon:2322/api?q=&lat=&lon=&limit=&bbox=` — Photon's bbox filter param takes
  `minLon,minLat,maxLon,maxLat`, filled from the config `envelope.denver_core` bounds,
  deliberately **wider** than the routing `graph_bbox` (filtering on `graph_bbox` itself would
  make every returned hit in-coverage and the flag below vacuous). Normalizes to:

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
  generated from `src/points.py` constants so UI copy can never drift. **A1 ships the complete
  schedule**, existing actions plus all five ride-mode constants (`POINTS_BATTERY_CONTRIBUTION_BASE`
  etc. — §A2's block is the normative list; the values are locked by master Decision 6 and nothing
  about the numbers waits on award machinery): frontend F2's Screen 2 ℹ copy interpolates these
  the day it deploys, and master §1.5 marks the endpoint an F2 dependency for exactly that reason.
  A2/A3 wire the awards; the schedule needs no further edits from them.
- Usuals (`src/api_preferences.py`, mirroring the map-settings pattern):
  `GET /api/v1/profile/ride-usuals` (list), `GET/PUT/DELETE /api/v1/profile/ride-usuals/{name}`.
  Blob = `ride_options` + `label`; 16 KB cap reused; cap 10 per account (409 at cap).

**Photon sidecar (self-hosted geocoding)** — mirror the Valhalla pattern already in
`docker-compose.yml` (expose-only sidecar; one-shot R2 fetch gated by
`service_completed_successfully`; named volume; mem limits; cli fetch/refresh commands):

- `docker/photon/Dockerfile`: `eclipse-temurin:21-jre` + a **pinned, checksum-verified** Photon
  release jar. No third-party community image in the trust chain. Install `curl` in the image:
  the temurin JRE base ships neither curl nor wget, and the compose healthcheck below execs
  inside this container — without it the service can never report healthy.
- Compose services: `photon_index_fetch` (one-shot, worker image,
  `python -m src.cli fetch_photon_index`, 256 MiB, volume `photon_files`) and `photon`
  (build `./docker/photon`, `expose: "2322"`, healthcheck on `/status`, `-Xmx1536m`,
  `mem_limit: 2048m`, `depends_on: photon_index_fetch: service_completed_successfully`). New named
  volume `photon_files`, mounted by both services **and writable in `scheduler`** — the 05:00
  refresh cron below runs in that container, the exact reason `docker-compose.yml` already gives
  `scheduler` a writable `valhalla_files` for `refresh_routing_graph` (`pipeline_worker` needs no
  mount at all: it reaches Photon over HTTP). `docker-compose.override.yml.example` gains a
  host-port mapping for local dev. Nothing is publicly exposed; riders reach it only through
  `/api/v1/geocode/search`.
- Index artifact: `photon/photon-index-<YYYYMMDD>.tar.zst` in the existing private `R2_MAP_BUCKET`
  (same scoped token as the Valhalla assets). `fetch_photon_index` mirrors `fetch_map_pbf`: ETag
  marker in the volume, download + unpack only on change, loud log that the `photon` container must
  be restarted to load a new index. Unpacking `.zst` requires adding `zstandard` to
  `requirements.txt` — stdlib `tarfile` reads only gz/bz2/xz and the worker image ships no `zstd`
  binary.
- Seeding runbook `scripts/build_photon_index.md` (manual, one-time + quarterly): Geofabrik
  `colorado-latest.osm.pbf` (~250 MB) → throwaway Nominatim import container →
  `photon -nominatim-import` → tar `photon_data/` → upload to R2. Colorado-scoped keeps the index
  low-GB and the JVM under 2 GiB; a full-US extract is explicitly rejected.
- Cron: `0 5 * * *` `refresh_photon_index` (ETag check, no-op most days — same shape as
  `refresh_routing_graph` at 04:30).

**Files:** `sql/046`, `sql/048`, `sql/049`, `src/api_tracked_rides.py`, `src/api_route.py`,
`src/valhalla.py`, new `src/api_geocode.py`, `src/main.py` (mount the new router),
`src/api_meta.py`, `src/api_points.py`, `src/api_preferences.py`, `src/cli.py`, `src/r2_map.py`
(the index fetch lives beside `sync_map_assets`), `requirements.txt` (`zstandard`), `config.json`,
`src/config.py`, `docker-compose.yml`,
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

  ALTER TABLE tracked_rides ADD COLUMN IF NOT EXISTS track_donated_at TIMESTAMPTZ;
  -- durable already-donated marker: idx_track_donations_ride stops binding the
  -- moment de-id nulls tracked_ride_id, so the 409 needs a stamp that survives
  -- the sweep (hard-delete of the ride still removes it, commitment intact).
  ```

  Raw JWS strings are **discarded after verification** — only `chain_root_hash` and the
  `verification` summary persist (retained signatures add nothing once verified and would be one
  more identifying artifact to sweep).
- `sql/052_ride_mode_points.sql` — widen `user_points_action_allowed` adding
  `'battery_contribution'`, `'nav_route_feedback'`, `'nav_qualitative_feedback'`,
  `'nav_distance_bonus'`, `'ride_survey'` (old values stay — historical rows are forever), using
  the sql/040 guarded shape (rewrite only when `'battery_contribution'` is absent from the live
  definition — the plain drop/re-add sql/037 used would re-narrow the list on replay after any
  later widening); add named CHECK `user_points_points_even CHECK (points % 2 = 0)`, guarded on
  conname alone (sql/041 step-4 shape). Historical rows are all even — not because every row
  reads 2/4/10/20/100 (waypoint rows are 2×count, and cap-trimmed rows are arbitrary even
  remainders like 96) but because every constant is even and the 100 cap trims even-to-even. The
  ADD is validated, and sql/ replays at boot against the populated production table (sql/041's
  header warning), so a stray odd row would fail the API's **startup**, not a test —
  `tests/test_migration_replay_pg.py` runs against the `VEO_TEST_PG_DSN` fixture database, which
  holds no real ledger rows. Run the one-line audit
  (`SELECT COUNT(*) FROM user_points WHERE points % 2 = 1` — expect 0) against production before
  shipping; that is this constraint's sql/041 "backfill before ADD" step.

**Endpoint work**

- `POST /api/v1/tracked-rides/{ride_id}/track` (new; owner-only; bucket
  `track_donation_account` 6/h; body cap 2 MB; ≤600 batches — cap sanity: the longest honest ride
  is the 3 h watch window, which at 1 Hz seals ≤~432 25-pt batches ≈ 650 KB of compact JWS, so
  both caps clear it with headroom). Request:
  `{"batches": ["<compact JWS>", ...]}`. Preconditions: ride ended (`user_reported_ended_at` set),
  no prior donation (409 `already_donated` — checked against `tracked_rides.track_donated_at`,
  because `idx_track_donations_ride` stops binding once de-id nulls `tracked_ride_id`),
  `ride_options.save_tracks` was on (422 `tracking_not_opted`). Pipeline, in one transaction that
  opens with `pg_advisory_xact_lock(hashtextextended('ride_validation:<ride_id>', 0))` — the start
  handler's idiom; `finalize_validation` takes the same lock, so a ride_watch resolve landing
  mid-donation serializes instead of leaving the donation `pending_feed` forever (the finisher
  would otherwise look for a donation row that hasn't committed yet): verify chain (below) → write
  `track_donations` + `donated_track_points`, stamp `track_donated_at` → recompute
  `validation_status` → award points → ingest battery observation. Response:

  ```json
  {"donation_id": "...",
   "verification": {"chain": "ok", "monotonic": "ok", "speed": "ok",
                    "gbfs_start": "ok", "gbfs_end": "ok", "volume": "ok"},
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
- Deprecation: `POST .../waypoints` (600/h single-waypoint) has **no known client callers** —
  the denver-scooter-fyi frontend never wired it (verified: zero references in its `src/`), so
  there is no "legacy HUD" dependency and its schedule is decoupled from frontend F3. It is
  retained one release purely as caution for unknown external callers, with an `API.md`
  deprecation note landing in A2. Waypoints it records stop earning points as of A2 — precisely:
  this endpoint never wrote ledger rows itself; the per-waypoint award was always granted at
  `/end` via `credit_waypoint_points`, so the supersession lands in `/end` (above), not here. It
  is **not** the transport for ride-mode tracks — those never stream.

**Verification (`src/track_verify.py` — pure module, fake-cursor unit-testable):**
`verify_track_chain(cur, ride_row, batches) -> VerificationResult` with checks, in order:

1. **Signature** — recompute HMAC-SHA256 per batch with `track_key`; the ride binding is explicit
   and triple: header `kid` == this ride's id, payload `rid` == this ride's id, payload `non` ==
   this ride's `track_nonce` — and the key itself is per-ride, so a chain built for any other ride
   or account fails here, not in a later heuristic. Any failure → `chain_invalid`.
2. **Chain integrity** — `seq` contiguous from 0; each `prev` equals sha256 of the predecessor's
   compact JWS; recomputed final `H_n` becomes `chain_root_hash`. Known, accepted limit: nothing
   marks the final batch, so silently omitting *trailing* batches is undetectable here — it only
   shrinks the claimable distance, and the surviving last point must still pass check 5's GBFS end
   correlation, so truncation buys a forger nothing.
3. **Monotonicity + bounds** — `t0 ≤ t1` per batch; timestamps strictly increasing across the
   flattened track; `t0(first) ≥ started_at − 120 s`; `t1(last) ≤ user_reported_ended_at + 120 s`.
   `started_at` is server-stamped at key issuance, so a chain cannot claim to predate the ride.
4. **Physical plausibility** — per-segment speed = haversine/Δt; hard-reject any segment > 20 m/s
   after accuracy adjustment (subtract `acc_i + acc_j` from segment distance first, with each
   accuracy **clamped to ≤50 m** for the adjustment — `acc` is attacker-supplied, and unclamped it
   is a free speed-eraser: claim 10 km "accuracy" and any teleport becomes plausible); if >10% of
   segments exceed 11 m/s sustained → points status `pending_review` (flag, not reject). Distance
   uses `ride_limits.measure_path(cap_legs=True)` + `clamp_distance` — the sql/041 leg/ride caps —
   over the raw, un-adjusted points.
5. **GBFS correlation** (primary anti-fabrication control — same anchor points as the
   `credit_gbfs_validation_points` geometry, deliberately looser than that award's 20 m: this is
   an eligibility gate over GPS-noisy fixes, not the award): first waypoint ≤150 m of the
   **feed-anchored** start — `feed_start_lat/lon` when A1's start handler stamped them, falling
   back to the client-supplied `start_lat/lon` only when the feed had no fresh observation
   (client-vs-client comparison is the weaker check; the fallback keeps old rides verifiable);
   when resolved, last waypoint ≤150 m of `gbfs_end_lat/lon` and `t1(last)` within ±10 min of
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

battery_contribution = 8 + 2 * ceil(distance_m / 2000)   # distance_m from the track_donations row
nav_distance_bonus   =     2 * ceil(distance_m / 3000)   # distance_m from the track_donations row; requires ride_routes row
```

Preconditions: `battery_contribution` requires a verified donation + both batteries known +
`ride_options.battery_modeling` + not own-device. `nav_*` require `ride_options.nav_improvement` +
a `ride_routes` row. One ledger row per action (itemized, matching the ℹ copy); `lat/lng/h3` = the
ride start point (the master's Risk 3 rule — the ledger keeps start coords; note the superseded
awards filed at ride-*end* points, so this is a deliberate change, not a continuation — either is
fine for §11, which aggregates whatever coordinate the row carries). Every ride-mode award is
filed `source_table='tracked_rides'`, `source_id=<ride_id>` — **not** `'track_donations'` or
`'ride_surveys'`: `_RIDE_SOURCE_TABLES` in `src/points.py` is `{'tracked_rides','rides'}`, so any
other source_table silently bypasses `_apply_ride_cap`, and per-source headroom wouldn't
aggregate across one ride's awards anyway. The formulas read distance from the `track_donations`
row; the ledger attribution is the ride. All awards route through `credit_points` →
`_apply_ride_cap` with the `(source_table, source_id, action)` dedupe; `credit_points` gains an
`assert points % 2 == 0` (safe against cap trimming: every award and the 100 cap are even, so a
trimmed remainder is even). Typical case (10 km ride, everything on): 18 (battery) + 18 (nav:
4 + 6 + 2·ceil(10000/3000) = 8) + 4 (survey) = **40**. The
cap is not decorative: an 80 km ride requests 88 + 64 + 4 = 156 and is trimmed to the unchanged
`MAX_POINTS_PER_RIDE = 100` — which only happens if the awards carry the source_table above.

**Battery ingestion** — on a verified donation with both batteries known, insert a
`battery_trip_observations` row `source='donated_ride'`, mapped onto sql/024's real columns:
`vehicle_identifier` from the ride, `vehicle_model_name` from `track_donations.vehicle_model`
(itself stamped at donation from `device_state.current_vehicle_model_name` for that vehicle —
sql/016's column, the same source A3's surveys read; NULL for unconfirmed models);
`departed_at`/`arrived_at` = the ride's `started_at`/`user_reported_ended_at` (both are NOT NULL
and feed `UNIQUE (vehicle_identifier, departed_at)`, so they must be stated, not guessed);
`duration_seconds` derived; `from_`/`to_lat`/`lon` = first/last verified waypoints;
`route_distance_meters` = verified track distance; `elevation_gain_meters` re-derived by
map-matching via Valhalla `trace_attributes` (reuse the shade-scoring trace path; NULL if the
trace fails); `soc_start_percent` = `feed_start_battery_percent` (fallback
`reported_start_battery_percent`), `soc_end_percent` = `reported_battery_percent`,
`burn_percent` = the difference; `temperature_c` from `hourly_temperature`. Double-count guard —
the nightly `extract_battery_trips` will mine this SAME trip as an observation gap: the donation
transaction deletes any overlapping same-vehicle feed-mined row (its `departed_at` inside the
ride window; match `source IS DISTINCT FROM 'donated_ride'` — rows predating sql/050 carry NULL
and are all feed-mined), and `extract_battery_trips` skips gaps overlapping a
`source='donated_ride'` row —
that discrimination is what the `source` column exists for. The weekly `train_battery_model`
consumes these unchanged — donated rows are strictly better than feed-mined observation-gap rows.

**Validation finisher** — new `finalize_validation(cur, ride_id)` called from `ride_watch.py`'s
resolve path **and** the `expire_stale_watches` cron, so `pending_feed` rides settle (award or
`end_mismatch`) without user action. It stamps `points_settled_at` on settle **regardless of
outcome** — a denied donation still starts the 4 h de-id clock — and takes the same
`ride_validation:<ride_id>` advisory lock as the donation handler (race above) — acquired
**before** touching the ride row in every participant: the donation transaction is
lock-then-write, so a resolve path that ran its `gbfs_*` row UPDATE first and locked second
would deadlock against a mid-flight donation (Postgres aborts one and the watch retries next
cycle, but do not ship the inversion). On an **eligible** late settle the finisher also runs the
battery ingestion — the donation transaction only ingests when GBFS had already resolved, so the
finisher is the sole ingestion path for `pending_feed` donations. Cron wiring
detail: `expire_stale_watches`' ride-side UPDATE skips rides with `user_reported_ended_at` set —
a donated ride is already `status='completed'` — so the finisher hook must select on elapsed
watch windows (`watch_expires_at < NOW()`, no `gbfs_reappeared_at`), not on ride status.

**De-id sweep** — new cron `15 * * * *` `python -m src.cli deidentify_donations`: for
`track_donations` where `points_settled_at < NOW() − INTERVAL '4 hours'`
**or** `donated_at < NOW() − INTERVAL '28 hours'` (force-floor even if points never settled): set
`account_id = NULL, tracked_ride_id = NULL, deidentified_at = NOW()`; coarsen
`donated_track_points.recorded_ms` to minute precision. `ride_routes` sweep on their **own**
clock — `created_at < NOW() − INTERVAL '28 hours'` — not via a donation: a nav-improvement ride
whose track is never donated still stored geometry, and hanging its de-id off a donation that
doesn't exist would keep it account-linked forever, contradicting the master's "everything with
fine geometry loses account linkage within ≤28 h". This clock is also the de facto deadline for
`nav_distance_bonus`: a donation landing >28 h after Screen 4 finds its route row already
de-identified and forfeits that bonus — the Trip-data page's "limited window", disclosed
behavior, not a bug. Guard that arm with
`to_regclass('ride_routes')` — sql/051 is A3's and A2 may deploy first. Pre-de-id,
`ON DELETE CASCADE` honors hard-delete; post-de-id the artifact has no owner. Three-address rule
updates, **scoped to what A2 itself ships** (the `ride_routes`/`ride_surveys` `_PRIVACY` and
privacy-policy entries ship with sql/051 in A3 — A2-first must not publish privacy copy for
tables that don't exist yet): donated tracks ("de-identified ≤28 h after donation" — this entry
also discloses the derived battery observation: ride endpoints, distance and SoC kept
indefinitely with no account linkage, matching the owner's ℹ "Our Usage" copy); amend the
existing `tracked_rides` `_PRIVACY` entry (its hard-delete promise now cascades to a pre-de-id
donation but cannot reach a de-identified one, and it must say so); and add the missing
`user_points` retention entry (a pre-existing `_PRIVACY` gap: ledger rows keep account, start
coordinates and the r8 cell indefinitely, deleted only by account cascade, aggregated into the
public leaderboard subject to the visibility toggles — the master plan calls these rows "the
leaderboard record" and the privacy page must actually say so).

**Files:** `sql/050`, `sql/052`, new `src/track_verify.py`, `src/api_tracked_rides.py`,
`src/points.py` (award functions — the constants and `/points/schedule` entries landed in A1),
`src/ride_watch.py`, `src/battery_model.py`, `src/cli.py`,
`crontab`, `src/api_meta.py`, `src/templates/legal/privacy_policy.html`, `API.md`, `README.md`,
`API_REQUIREMENTS.md`.

**Tests:** `tests/test_track_verify.py` (golden chains from the shared fixture — the byte-shared
copy is the single file `tests/fixtures/track-chain-vectors.json`, the **program-wide canonical
path**, committed byte-identically at that same literal path in both repos (the frontend's Vitest
suite loads the same JSON) — do not invent a different name or split it into per-case files.
Vectors: valid,
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
A2's `nav_distance_bonus` checks for a `ride_routes` row defensively, and A3 carries its own
guarded action-vocabulary widening (in `sql/051`) and de-id arm (in `src/cli.py` — a cron job,
not migration SQL; below), so landing order between A2/A3 doesn't matter.

**Migration: `sql/051_ride_surveys_routes.sql`**

```sql
CREATE TABLE IF NOT EXISTS ride_routes (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tracked_ride_id           UUID   REFERENCES tracked_rides(id) ON DELETE CASCADE, -- nulled by de-id
    account_id                BIGINT REFERENCES accounts(id)      ON DELETE CASCADE, -- nulled by de-id
    profile                   TEXT NOT NULL,       -- a config.json valhalla.profiles key (safe|range|shade|express today)
    origin_lat  DOUBLE PRECISION NOT NULL, origin_lon DOUBLE PRECISION NOT NULL,
    dest_lat    DOUBLE PRECISION NOT NULL, dest_lon  DOUBLE PRECISION NOT NULL,
    route_polyline            TEXT NOT NULL,       -- precision-5, src/polyline.py convention
    distance_meters           DOUBLE PRECISION,
    duration_seconds          DOUBLE PRECISION,
    battery_percent_estimate  DOUBLE PRECISION,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deidentified_at           TIMESTAMPTZ
);
-- sql/050's index pair, mirrored: the ride lookup (A2's nav_distance_bonus, the
-- survey's already-linked check, tracked_rides delete cascades) and the sweep's
-- predicate (idx_track_donations_deid's twin — the hourly 28 h arm below).
CREATE INDEX IF NOT EXISTS idx_ride_routes_ride
    ON ride_routes (tracked_ride_id) WHERE tracked_ride_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ride_routes_deid
    ON ride_routes (created_at) WHERE deidentified_at IS NULL;
CREATE TABLE IF NOT EXISTS ride_surveys (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tracked_ride_id  UUID NOT NULL UNIQUE REFERENCES tracked_rides(id) ON DELETE CASCADE,
    account_id       BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    vehicle_model    TEXT,               -- stamped server-side from device_state.current_vehicle_model_name
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
geometry). `ride_routes`, which has geometry, is de-identified by the A2 sweep — but that sweep
ships in A2 and the phases land in either order: deployed A3-first, `ride_routes` would
accumulate account-linked geometry with **no** sweep, violating the master's "everything with
fine geometry loses account linkage within ≤28 h" (Risk 3). So A3 ships the hourly 28 h
`ride_routes` de-id arm itself: the same `deidentify_donations` command and `15 * * * *` crontab
slot A2 specifies, but carrying **only** the `to_regclass('ride_routes')`-guarded routes arm
(null `account_id`/`tracked_ride_id`, stamp `deidentified_at`; select on `deidentified_at IS
NULL` — the partial index above) — NOT A2's donations arm, whose `track_donations` table does
not exist in the A3-first order — so whichever phase lands second merges the shared arm as a
no-op and adds its own. Same landing-order
trap on points: `'ride_survey'`, `'nav_route_feedback'` and `'nav_qualitative_feedback'` enter
`user_points_action_allowed` via A2's `sql/052` — deployed A3-first, every survey award would
violate the live CHECK (sql/028, rewritten in sql/037) and 500. `sql/051` therefore widens the
constraint for these three actions itself, using the sql/040/042 value-checked guard keyed on
`'ride_survey'` (052's guard keys on `'battery_contribution'`, so the two migrations no-op
against each other's work in either order). The `POINTS_*` constants and the full
`/points/schedule` are **A1's** (single definition, no per-phase copies to drift): A3 only wires
its three award functions to constants that already exist. Three-address rule: `ride_surveys` (free text)
and `ride_routes` (geometry) are new stored data, so **this** PR carries their
`src/api_meta.py:_PRIVACY` entries and `src/templates/legal/privacy_policy.html` copy.

**Endpoints**

- `POST /api/v1/ride-routes` (new; session; bucket `ride_route_account` 30/h — the
  `tracked_ride_start_account` / `ride_screenshot_account` naming style): request
  `{"tracked_ride_id": "...|null",
  "profile": "safe", "origin": [lat,lon], "destination": [lat,lon], "route_polyline": "...",
  "distance_meters": n, "duration_seconds": n, "battery_percent_estimate": n|null}` →
  `{"ride_route_id": "..."}`. `tracked_ride_id` is null in the normal wizard flow — Screen 4
  precedes ride start — and the survey below is what links the row to its ride; when non-null
  (the S8 New-Destination loop re-runs Screen 4 mid-ride with the ride id known), it must
  resolve to a **caller-owned** ride, else 404 — the FK alone would accept any account's ride
  id, and the 404 (not 403) is the no-existence-oracle idiom every tracked-rides endpoint uses.
  Multi-row semantics, stated so nobody re-derives them: a New-Destination loop legitimately
  creates a **second** row for the same ride (each deliberate Screen 4 selection is one row;
  automatic off-route re-routes never POST — the frontend plan pins that client rule); the
  survey rates the leg its `ride_route_id` names, and `nav_distance_bonus` is awarded at most
  once per ride regardless of row count (the `(source_table='tracked_rides', source_id, action)`
  dedupe — it requires *a* route row, not a specific one). No uniqueness on `tracked_ride_id`
  is intended; `tests/test_ride_routes.py` covers the multi-route-per-ride case.
  The client calls
  it **only** when `nav_improvement` is on — that consent is what makes storing a route
  acceptable. 400 `unknown_profile` (the `/route` handler's payload) when `profile` isn't a
  `load().valhalla` profile key; 400 if the polyline decodes to <2 points (`src/polyline.py`
  `decode()` at its default precision 5 — `/route` returns GeoJSON, so the client encodes) or
  endpoints fall outside `graph_bbox` (`load().valhalla.contains`, the `/route` handler's own
  `out_of_coverage` rejection). Client-claimed metrics get 422 bounds — `distance_meters`
  0–80 000 (`MAX_RIDE_DISTANCE_METERS`), `duration_seconds` 0–10 800 (the 3 h watch window),
  `battery_percent_estimate` 0–100: no award reads them (`nav_distance_bonus` reads the
  *verified* donation distance), but stored numerics get bounds — the same rule
  `apollo_top_speed_mph` follows below.
- `POST /api/v1/tracked-rides/{ride_id}/survey` (new; owner; ride ended —
  `user_reported_ended_at` set, else 409 `ride_not_ended`; single-shot — second POST 409, the
  UNIQUE backstopping a `SELECT … FOR UPDATE` on the ride row per the `/end` idiom, so a
  concurrent double-POST 409s instead of surfacing the constraint as a 500): request mirrors
  `ride_surveys`. `issues` validated against the fixed vocabulary
  (`app_veo, acceleration, basket, battery, bell, brakes, connectivity, customer_service, dirty,
  kickstand, pedals, phone_holder, price, speedometer, scooterfyi_issue, vandalized`);
  `vehicle_model` stamped server-side from `device_state.current_vehicle_model_name` for the
  ride's `vehicle_identifier` — ingest's `_KNOWN_VEHICLE_TYPES` app names, capitalized
  (`"Astro"`/`"Cosmo"`/`"Apollo"`; NULL for unconfirmed models) — and `model_bonus` keys
  validated against that stored value (`cosmo_front_basket` bool / `apollo_top_speed_mph`
  numeric, api-bounded 0–40 / `astro_landscape_holder` bool; any key 422s when the model is
  NULL or doesn't match). Awards, filed `source_table='tracked_rides'`,
  `source_id=<ride_id>` (A2's cap rule, restated here because A3 can land first — any other
  source_table silently bypasses `_apply_ride_cap`): `ride_survey` 4 (any scooter-feedback
  answer present ∧ `ride_options.end_survey` ∧ not own-device — defensive, not a reachable
  honest path: an own-device ride never has a `tracked_rides` row to survey (`POST
  /tracked-rides` requires a feed `vehicle_identifier`; the master glossary keeps private rides
  local-only), so this gate server-enforces Screen 2's disable rule against a contradictory
  client-owned `ride_options` blob); `nav_route_feedback` 4 (`nav_route_rating` present ∧
  `ride_route_id` resolves to a row owned by the caller whose `tracked_ride_id` IS NULL —
  submitting stamps it to this ride, which is the link A2's `nav_distance_bonus` reads — or
  already equals this ride; a row linked to a *different* ride 422s, otherwise one stored route
  could be replayed on every later survey for repeat awards — and a row A3's own 28 h sweep has
  de-identified fails the ownership test and 422s the same way, since a stale id and a guessed
  one are indistinguishable: a >28 h-late survey retries without `ride_route_id`, forfeiting
  only the nav awards, the master's "limited window" rule working as designed);
  `nav_qualitative_feedback` 6
  (post-trim length of `nav_qualitative` ≥ 20 — a plain `len(text.strip()) >= 20` check;
  "meaningful" is not machine-checkable and no content heuristic is attempted). Screen 9
  precedes Screen 10, so the route link exists by donation time; donating without ever
  surveying forfeits only `nav_distance_bonus`, consistent with the owner's "complete a quick
  survey … and donate" copy. Response echoes the row + points array. Ride payloads gain a
  `survey_submitted` flag (`src/api_tracked_rides.py:_row_to_ride`, an EXISTS against
  `ride_surveys`).

**Files:** `sql/051`, new `src/api_ride_surveys.py` — decided, not implementer's choice: a
tracked-rides sub-resource lives in its own router file, the exact `src/api_ride_screenshots.py`
precedent (`/api/v1/tracked-rides/{ride_id}/screenshots` mounts separately in `main.py`) — new
`src/api_ride_routes.py` (router for `/ride-routes`; named after the `ride_routes` table, NOT
`api_rides_routes.py`, which reads as a sibling of `api_rides.py`, the off-feed module this
program must not touch), `src/api_tracked_rides.py` (`survey_submitted`), `src/points.py`
(constants + survey award functions), `src/api_points.py` (the three actions enter
`/points/schedule`, above), `src/cli.py` + `crontab` (the 28 h `ride_routes` de-id
arm, above), `src/api_meta.py` + `src/templates/legal/privacy_policy.html` (three-address
entries for both new tables), `src/main.py` (mount both routers), `API.md`, `README.md`,
`API_REQUIREMENTS.md`.

**Tests:** `tests/test_ride_routes.py` (validation incl. the metric bounds, profile-vs-config,
bbox, non-owned `tracked_ride_id` 404, consent-only call
pattern documented), `tests/test_ride_surveys.py` (vocabulary 422, model-bonus matrix per model
incl. NULL-model 422, single-shot 409, not-ended 409, award gates incl. own-device and the
post-trim 20-char threshold, `ride_route_id` ownership/linking incl. the cross-ride reuse 422),
`tests/test_cli_deidentify.py` (created here when A3 lands first; A2 extends it) covers the
28 h `ride_routes` arm boundary.

**Acceptance:** a survey with rating + qualitative + a stored route awards exactly 4+4+6 — on a
database that has applied `sql/051` but not `sql/052` (the A3-first order must not trip
`user_points_action_allowed`); a second POST 409s; issues outside the vocabulary 422; a
`ride_route_id` replayed from an earlier ride 422s.

**Parallel lanes (2):** ① ride-routes + the de-id arm and three-address entries ② surveys +
awards (incl. the `/points/schedule` entries).

---

## Phase A4 — §11 H3 r8 Leaderboard

**Goal:** implement FEATURE_PLAN_2026-07.md §11 as specified — with one response-shape extension
for the frontend Leaderboard view, plus two narrow §11 deviations argued where they occur (an
index reconciliation §11 omits; a content-keyed ETag where §11.4's run-keyed one would break its
own read-time-privacy rule). Independent of A2/A3 mechanics (reads the ledger only); can land
any time after A2 (earlier is harmless — new actions simply appear as they start being awarded).

**Migration: `sql/047_h3_r8_area_leaders.sql`** — exactly §11.2: `h3_r8_area_report`,
`h3_r8_area_leaders` (top **3** per cell), `h3_r8_area_leader_runs`, plus
`idx_user_points_h3_8_created` — with one reconciliation §11 doesn't state: `sql/028` already
ships a plain `idx_user_points_h3_8 ON user_points (h3_8_index)`, which the new composite
`(h3_8_index, created_at DESC)` strictly subsumes, so 047 also runs
`DROP INDEX IF EXISTS idx_user_points_h3_8` (idempotent, replay-safe) instead of leaving every
`user_points` insert maintaining two indexes over the same leading column.

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
       "total_points": 144, "distinct_earners": 4,
       "leader":     {"display_name": "Duke swift🦦", "points": 88,
                      "ruling_color": "#7c54cd", "ruling_border_color": "#382264",
                      "ruling_alpha": 0.6},
       "runners_up": [{"display_name": "...", "points": 30,
                       "ruling_color": null, "ruling_border_color": null, "ruling_alpha": null}]
     },
     "8828308283fffff": {"total_points": 0, "distinct_earners": 0, "leader": null, "runners_up": []}
   }}
  ```

  Semantics (per §11.4, restated so the extension can't drift): cell keys are canonical **h3
  strings** (`h3.int_to_str` — JS `MAX_SAFE_INTEGER`); privacy applied at **read time** — accounts
  with `show_in_leaderboards` off (or hidden username per `show_public_username`, or a `NULL`
  `display_name` — sql/025's never-backfilled-username case, which sql/044's generated column
  propagates; a nameless leader would render as a literal null) are omitted
  entirely and the next stored rank falls through into `leader`; `runners_up` = the remaining
  eligible stored ranks in order (`leader` + `runners_up` ≤ 3 total); colors live-joined from
  `accounts` and **null when unclaimed — the API never invents a default color** (frontend
  decision) — the pair is both-or-neither by `accounts_ruling_colors_coherent` (sql/044), but
  `ruling_alpha` is `NOT NULL DEFAULT 0.60` in the schema, so the handler **nulls it in the
  payload whenever the pair is null** (an alpha without a fill would just leak the column
  default; the example above is normative). No separate `royalty_title` field: `display_name` is
  read straight off sql/044's generated column, which **already composes**
  `COALESCE(royalty_title || ' ', '') || username_adjective || username_emoji` — the frontend's
  "generous leader section" renders the composed name it receives, and shipping the title again
  would be a second copy of the same fact that can only drift.
  ~720 cells × ≤3 entries ≈ 40 KB gzipped. `Cache-Control: public, max-age=600` as
  `/h3/aggregates` — but **not** §11.4's run-keyed ETag: `/h3/aggregates` may key its weak ETag
  on the cycle because its payload is a pure function of the cycle (`src/api_h3.py` says so
  explicitly); this payload is a **live join** — eligibility, colors, and re-rolled names all
  change it between runs — so an ETag on `h3_r8_area_leader_runs.computed_at` would answer
  `If-None-Match` with 304s that resurrect an opted-out rider until the next daily run,
  contradicting §11's own read-time-privacy rule. Key the weak ETag on the run **and** the
  rendered content (`W/"arealb:<computed_at epoch>:<sha256[:16] of the cells JSON>"`, reusing
  `src/api_public.py:_if_none_match_hit` like `api_h3.py` does). Both components are load-bearing:
  the hash is over a **canonical** serialization (`json.dumps(cells, sort_keys=True,
  separators=(",", ":"))` — anything process-dependent churns the tag and silently defeats every
  304), and the `computed_at` component keeps 304 honest when a daily run changes only
  `computed_at`/window fields while the cells happen to come out identical (near-certain at
  launch volumes — a cells-only tag would revalidate clients onto stale window dates, which the
  frontend detail panel renders). Shared caches still age out within `max-age=600`.
- `GET /api/v1/private/area-leaders` (admin) per §11.4, unchanged.

**Three-address rule:** `h3_r8_area_leaders` stores a new account-linked fact (account ↔ cell
rank, plus `first_point_at`), and `src/api_meta.py`'s header is explicit that a new stored field
counts as a retention rule even when derived — so this PR carries the `_PRIVACY` entry and the
`privacy_policy.html` copy (derived nightly from the points ledger, fully replaced each run,
deleted with the account via cascade, publicly exposed only through the read-time visibility
filters above). A1–A3 each carry their entries; "it's just a daily report" is not an exemption.

**Files:** `sql/047`, new `src/area_leaders.py`, new `src/api_leaderboard.py`, `src/api_private.py`
(admin sibling), `src/cli.py`, `crontab`, `src/main.py` (mount), `src/api_meta.py`,
`src/templates/legal/privacy_policy.html`, `API.md`, `README.md`,
`API_REQUIREMENTS.md`.

**Tests:** `tests/test_area_leaders_logic.py` (tie-break, window, confirmed-only),
`tests/test_area_leaders_pg.py` (universe union, full-replace idempotence, account-delete cascade),
`tests/test_api_leaderboard_map.py` (privacy fall-through to rank 2/3 — incl. ranks 1+2 both
hidden → rank 3 is `leader` with `runners_up: []`; runners-up omit ineligible;
all-opted-out → `leader: null`; a `NULL`-`display_name` account skipped exactly like an opt-out;
ETag/304 incl. an eligibility flip changing the ETag so a held
`If-None-Match` misses, and a new run (fresh `computed_at`, identical cells) also changing it;
h3 string keys; null-color passthrough with `ruling_alpha` nulled
alongside the pair).

**Acceptance:** with production-shaped data (a handful of ledger rows), the endpoint returns ~720
cells, nearly all `leader: null`, in one ETagged fetch; flipping `show_in_leaderboards` off hides
the account on the very next origin request without a recompute **and changes the ETag** — a
revalidating client must not be 304'd back to the old body (shared-cache copies age out within
the 600 s `max-age`; that bound is inherited from §11.4's chosen header).

**Parallel lanes (2):** ① recompute + tables ② endpoint + privacy semantics.

---

## Cross-cutting: what the frontend is promised

The complete frontend-facing contract table lives in `RIDE_MODE_OVERHAUL_PLAN.md` §1.5. Any change
to a request/response shape in this plan must be reflected there (both repo copies) and in
`API.md` in the same PR.
