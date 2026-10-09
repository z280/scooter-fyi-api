# scooter-fyi-api

Denver micromobility spatial analytics pipeline. Polls the Veo GBFS feed
every 2 minutes, geo-tags each device against twelve boundary layers
(Disadvantaged Areas v1/v2, the DOTI Equity Index tiers er1–er6, the city's
official Equity Areas, Neighborhoods, Council Districts, Community
Networks), and stores cycle-by-cycle metadata + per-region counts in
Postgres. Cold storage of raw points goes to Cloudflare R2 as Parquet
every 24 hours. Around that core it also serves the scooter.fyi rider API
(accounts, ride tracking, points, routing and geocoding), public fleet
analytics, and a GitHub-OAuth admin panel.

> Formerly the `veo-audit` repo. **"Veo Audit" remains the public
> dataset/report brand** — only the repo/image names moved. Names that
> identify live resources deliberately did *not*: the Compose project
> (`veo-audit`, the prefix on every volume), the `/opt/veo-audit` deploy
> dir, the `veo-audit-archive` R2 bucket, and the `veo_audit` database.
> See [docs/reference/MIGRATION.md](docs/reference/MIGRATION.md#post-rename-operator-checklist).

The original purpose was tracking compliance with Denver RFP §3.0 (30%
of fleet in Equity Areas) — see `docs/deferred/VEO_AUDIT.md` for that history. The
v3.3 architecture (this README) generalizes the pipeline so any future
frontend (scooter.fyi, weseeyouveo.com, keepdenverfair.com, …) can XHR-poll the public REST
API for live state. For full request/response shapes, error codes, and
auth details behind the endpoint tables below, see [docs/reference/API.md](docs/reference/API.md).

## Architecture

```
Veo GBFS feed                                 Browser
     ▲                                           │ https://data.scooter.fyi
     │ */2 min                                   ▼
┌──────────────────────────┐              Cloudflare edge (TLS)
│  scheduler               │                     │ hostname managed in
│  run-scheduler.sh →      │                     │ z280/cloudflare-management
│  supercronic + crontab   │                     ▼
│  1.0 GiB cap             │              `ovh3-ingress` docker network
│  TZ=America/Denver       │                     │ pipeline_worker:8080
└──────────────────────────┘                     ▼
     │ runs every job itself:            ┌──────────────────────────┐
     │ `python -m src.cli <command>`     │  pipeline_worker         │
     │ (ingest_cycle, rollups,           │  1.0 GiB RAM cap         │
     │  archive, retention, …)           │  FastAPI: public API,    │
     │                                   │  rider API, admin panel  │
     │                                   │  HTTP only; applies      │
     │                                   │  migrations at boot      │
     │                                   └──────────────────────────┘
     │ writes                                    │ reads / writes
     ▼                                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  denver_spatial_db   vanilla Postgres 15 (no PostGIS)           │
│  2.5 GiB RAM cap     source of truth for all persistent state   │
│  shared_buffers=2GB                                             │
└─────────────────────────────────────────────────────────────────┘
     │ archive_if_due: checked daily 02:00, acts once 24 h have
     │ passed since last_archive_ts
     ▼
Cloudflare R2 (Parquet, ZSTD)   raw_telemetry_points archive
```

The `scheduler` and `pipeline_worker` containers share the same image.
The scheduler's entrypoint is `/usr/local/bin/run-scheduler.sh`
(`scripts/run-scheduler.sh`): on first boot it seeds `/app/state/crontab`
from the baked-in `/app/crontab`, then runs supercronic against that copy
and re-execs it within ~15 s whenever the file changes. Every scheduled job,
including the ingest cycle, runs as `python -m src.cli <command>` inside the
scheduler container, straight against Postgres; `pipeline_worker` serves
HTTP only (`src/main.py`). The split means the HTTP API can crash and
restart without disturbing the schedule, and the scheduler can crash
without taking the API down.

Two more sidecars sit beside them on the internal network: `valhalla`
(routing, fed by the one-shot `valhalla_map_fetch`) and `photon`
(geocoding, fed by the one-shot `photon_index_fetch`). Neither is reachable
from outside; riders reach them through `/api/v1/route` and
`/api/v1/geocode/search`.

**Ingress.** There is no tunnel sidecar in this repo any more (the
`cloudflared` service was removed in `ec6bc7d`, 2026-07-29). Public traffic
arrives over the external `ovh3-ingress` network, which is owned by
z280/cloudflare-management, and `data.scooter.fyi` resolves
`pipeline_worker:8080` on it. Hostnames are managed in that repo, not here.
`pipeline_worker` must declare `ovh3-ingress` in `docker-compose.yml`: a
deploy recreates the container, and a network attached only at runtime is
dropped on recreate (that is how the site went down on 2026-07-29).

### Postgres vs DuckDB — which does what

| | **Postgres** | **DuckDB** |
|---|---|---|
| Lives | always-on container | ~1 sec per cycle, in-process |
| Holds | every persistent table | nothing, between cycles |
| Used for | storage, admin queries, public API | spatial join (`ST_Within` against GeoJSON boundaries) |

Postgres is the system of record. DuckDB is a worker tool that loads
GeoJSON boundaries with its spatial extension, joins against the
just-tagged points, dumps aggregates into Postgres, and closes. This
keeps steady-state RAM near zero, which matters because the host (ovh3,
22 GiB) also runs other stacks that this repo does not cap.

## Repo layout

```
.
├── config.json                 non-secret runtime config
├── .env.example                env template (secrets ONLY)
├── docker-compose.yml          seven services, hard memory caps (Postgres, worker,
│                               scheduler, the Valhalla routing pair, the Photon
│                               geocoding pair); three EXTERNAL networks
├── docker-compose.override.yml.example  local-dev host ports (worker :8080, photon :2322)
├── Dockerfile                  python:3.11-slim + FastAPI + DuckDB + supercronic + tini
├── crontab                     seed schedule for the scheduler container (see Schedule)
├── data/                       baked into the image (not mounted)
│   ├── v1.json                 Disadvantaged Areas v1 — 34 polygons (legacy hand-drawn)
│   ├── v2.json                 Disadvantaged Areas v2 — 65 census block groups
│   ├── er1.json … er6.json     DOTI Equity Index, one file per exact rank tier
│   │                           (34 / 58 / 157 / 93 / 114 / 116 block groups)
│   ├── equity.geojson          official Equity Areas — 30 polygons (EQ_001…EQ_030),
│   │                           the map the contract binds (sql/079)
│   ├── NB.geojson              78 neighborhoods
│   ├── CD.geojson              council districts (11 numbered + 2 at-large)
│   ├── CN.geojson              13 community networks
│   ├── range_soc_lut.json      range → state-of-charge lookup (quality.py, battery_model.py)
│   └── (7 more)                Atlanta, Bird and Lyft reference files (cited by
│                               docs/ATLANTA_PLAN.md / docs/MULTI_TENANCY_PLAN.md);
│                               nothing in src/ loads them
├── sql/001_init.sql … 095_receipt_claims_drop_plan_evidence.sql
│                              97 files (061 and 069 each have two), applied in
│                              filename order by pipeline_worker at boot
├── scripts/
│   ├── run-scheduler.sh        scheduler entrypoint: seeds + live-reloads the crontab
│   ├── check_migration_numbers.py  CI lint: no two sql/NNN_ files share a number
│   ├── migrate-state.sh        one-off host move of volumes/state (docs/reference/MIGRATION.md)
│   ├── analyze_range_signal.py range-signal analysis against the R2 archive
│   └── gen_ruling_palette.py   provenance for sql/044's 128-colour palette
├── docker/photon/Dockerfile    the Photon geocoding sidecar (pinned + sha256-verified
│                               official jar; the index itself ships from R2)
├── docs/                       plans (active at top level, plus implemented/ and
│                               deferred/) and reference/ (API.md, MIGRATION.md,
│                               build_photon_index.md); index in docs/README.md
├── src/
│   │  — app + plumbing —
│   ├── main.py                 FastAPI app, lifespan, migrations, router mounts
│   ├── cli.py                  every `python -m src.cli` command (see CLI reference)
│   ├── config.py               loads config.json (non-secret) + env (secrets)
│   ├── pg.py                   psycopg pool + migration runner
│   ├── job_runs.py             job-run ledger — every scheduled command's
│   │                           last run, status and summary (/admin/scheduler)
│   ├── duck.py                 ephemeral DuckDB session factory
│   ├── ratelimit.py            Postgres-advisory-lock rate limiting (no Redis)
│   ├── client_ip.py            client IP extraction behind Cloudflare
│   ├── request_metrics.py      per-request metrics middleware (route template, status,
│   │                           latency, coarse device; no IP), flushed in batches
│   ├── sentry.py               Sentry SDK init (no-op without DSN)
│   │  — ingest cycle —
│   ├── ingest.py               GBFS fetch + freshness + envelope tagging
│   ├── identity.py             HMAC plate → vehicle_identifier (the privacy boundary),
│   │                           + plate_display_code (cosmetic-only, NOT a privacy control)
│   ├── vehicle_identity.py     derived memorable vehicle names ("Lunar 🐸"), not stored
│   ├── compute.py              DuckDB CTEs → core + narrow rows
│   ├── device_state.py         per-vehicle NEW/MOVED/FAILED_START/STATIONARY state machine,
│   │                           rental counters, absent-vehicle stop closing
│   ├── ride_watch.py           rider-declared ride watch: detect a device leaving/
│   │                           rejoining the feed (tracked_rides); called from cycle.py
│   │                           right after device_state, same isolation contract
│   ├── cycle.py                observation_cycles lifecycle state machine
│   ├── transmit.py             fanout to downstream endpoints
│   ├── analytics_rollups.py    fleet-analytics rollups (sql/094), refreshed last in each cycle
│   ├── archive.py              24-hour Parquet → R2 → TRUNCATE
│   ├── ghost_stops.py          one-off close of pre-sql/083 ghost stops (manual CLI)
│   │  — geography + equity —
│   ├── boundaries.py           cached GeoJSON boundary loader
│   ├── geo.py                  pure-Python point-in-polygon (report-time region lookup;
│   │                           the per-cycle device join stays in DuckDB — see compute.py)
│   │                           + distance_meters (shared flat-earth distance helper)
│   │                           + path_length_meters (tracked-ride distance from waypoints)
│   ├── equity_groups.py        registry of tracked equity groups (v1, v2, er1..er6, equity),
│   │                           the compliance groups (v1, v2, equity), OFFICIAL_GROUP
│   │                           (equity), and split dimensions (bicycle/scooter,
│   │                           sitting/standing) — single source of truth for
│   │                           compute.py + daily_sla.py
│   ├── daily_sla.py            9:02am daily SLA compliance rollup
│   ├── equity_backfill.py      9:40am reprocessing of PRIOR days' Equity Area compliance
│   │                           against the city's clarified map — rebuilds each past
│   │                           cycle's fleet from device_history's stop intervals, gated
│   │                           on agreeing with the fleet count that cycle recorded
│   ├── fleet_outcomes.py       share of rentals that never moved, fleet + by model
│   ├── fleet_equity.py         the same, inside vs outside the official Equity Areas
│   ├── parking_response.py     how long Veo takes to move a reported badly-parked vehicle
│   │  — fleet stats —
│   ├── quality.py              reliability tier + battery-percent conversion
│   ├── ranking.py              range/popularity ranking helpers
│   ├── dwell_stats.py          dwell-time outlier detection
│   ├── daily_trips.py          9am daily trip/popularity rollup (trip_events → ranked stats)
│   ├── battery_model.py        empirical battery-burn regression: nightly observation-gap
│   │                           mining + weekly refit, plus ingest_donated_observation
│   │                           (the donated-track battery feedback loop, sql/051)
│   ├── weather.py              Open-Meteo temperature backfill for the battery model
│   ├── device_features.py      crowdsourced feature consensus + the :08 report processor
│   │  — accounts, auth, comms —
│   ├── accounts.py             account/session core: bearer tokens, require_session/
│   │                           require_admin, public-username generation/
│   │                           choice, phone number validation
│   ├── google_auth.py          local Google ID-token (JWKS) verification, no per-request call
│   ├── postmark.py             transactional email (magic link, sign-in code)
│   ├── comms.py                z280-comms outbound SMS client
│   ├── comms_replies.py        polled inbound SMS replies + STOP/UNSTOP mirror
│   ├── auth.py                 GitHub OAuth + org allowlist (admin panel only — a
│   │                           separate mechanism from accounts.py's rider auth)
│   │  — rides, points, uploads —
│   ├── points.py               points ledger primitives (credit_points + per-action wrappers,
│   │                           incl. the reshaped ride-mode awards battery_contribution/
│   │                           nav_distance_bonus; credit_points is where the
│   │                           100-points-per-ride cap and the even-points assert live)
│   ├── track_verify.py         pure server-side verifier for the donated waypoint chain:
│   │                           signature → chain integrity → monotonic/bounds → speed →
│   │                           GBFS start/end correlation → volume minimums
│   ├── ride_limits.py          the operator's three hard ride invariants — 100 points/ride,
│   │                           3 km between consecutive path points, 80 km/ride — plus the
│   │                           shared path measurement both ride modules close out with
│   ├── ride_totals.py          a rider's lifetime ride count + distance
│   ├── badges.py               server-computed profile badges (recomputed on every read;
│   │                           mileage/streak badges union tracked_rides.distance_meters
│   │                           with off-feed rides.distance_m — ended rides only)
│   ├── area_leaders.py         weekly H3 r8 cell-universe refresh + the leaderboard's
│   │                           window/ranking rules (the board itself is read-time)
│   ├── polyline.py             Google polyline encode/decode (ride paths)
│   ├── image_processing.py     shared Pillow re-encode pipeline (EXIF strip, resize, JPEG) —
│   │                           used by receipts, device photos, ride screenshots
│   ├── receipts.py             discount-report receipt upload → private R2 bucket
│   ├── receipt_claims.py       equity receipt claims, Phase 1: the gate + recorded arithmetic
│   ├── device_photos.py        device photo upload → PUBLIC R2 bucket
│   ├── ride_screenshots.py     ride transaction screenshot upload → PRIVATE R2 bucket
│   ├── qr.py                   QR payload plate extraction + vehicle_identifier validation
│   │  — routing + geocoding —
│   ├── valhalla.py             Valhalla HTTP client + trip shape/summary/maneuver
│   │                           extraction (per-leg shape indices re-offset in one pass)
│   ├── r2_map.py               SigV4 sync of the private R2 sidecar artifacts: the
│   │                           routing .pbf + canopy sidecar, and the Photon index
│   ├── addresses.py            Denver address-point index (sql/074) merged into geocoding
│   │  — analytics —
│   ├── analytics.py            daily telemetry/request/campaign rollups + retention sweeps
│   ├── campaigns.py            campaign registry + utm_campaign attribution (sql/075)
│   │  — HTTP routers —
│   ├── api_public.py           14 public read-only routes: health, snapshots, boundaries,
│   │                           devices/current, equity estimate, fleet outcomes, compliance
│   ├── api_analytics.py        public fleet analytics (/api/v1/analytics/*)
│   ├── api_device_history.py   public hourly fleet size (/api/v1/devices/history/hourly)
│   ├── api_h3.py               public H3 aggregate endpoint
│   ├── api_leaderboard.py      public territory leaderboards, computed at read time
│   ├── api_meta.py             public metadata endpoints — privacy retention policy
│   │                           and the Ride Mode sales-tax rate (`/meta/pricing`)
│   ├── api_telemetry.py        first-party telemetry ingest (POST /api/v1/telemetry/events)
│   ├── api_dibs.py             dibs claims, certificates and the public verification page
│   ├── api_user.py             signed-in device map feed
│   ├── api_profile.py          rider profile GET/PUT + public-username endpoints
│   ├── api_lexicon.py          emoji-noun / adjective list + search endpoints
│   ├── api_preferences.py      rider-owned opaque preference blobs: saved map settings,
│   │                           ride-mode "Usuals", ride specs
│   ├── api_favorites.py        My Scooters — retired 2026-10-06; only the list-what-you-kept
│   │                           and delete routes stay mounted
│   ├── api_route.py            GET /api/v1/route (+ /options, /walk, /profiles) — Valhalla
│   │                           proxy, shade re-ranking, battery estimate, turn-by-turn
│   │                           maneuvers, outside-city flags
│   ├── api_geocode.py          GET /api/v1/geocode/search — Denver address index first,
│   │                           then the self-hosted Photon sidecar (Denver bbox filter,
│   │                           in_coverage vs the routing graph, 24h LRU cache)
│   ├── api_auth.py             sign-in doors + session lifecycle
│   ├── api_tracked_rides.py    GBFS-detected ride tracking: start/list/active/detail/
│   │                           end-report/track-donation/waypoints(deprecated)/delete
│   ├── api_rides.py            OFF-FEED rides — vehicles not in the GBFS feed (a personal
│   │                           scooter, a competitor's rental). Same lifecycle as tracked
│   │                           rides (start/waypoints/end) plus a one-shot log of a
│   │                           finished ride, owner-only list/export, hard delete. No
│   │                           points; client-asserted distances are plausibility-checked
│   ├── api_points.py           GET /api/v1/points — ledger + running total; GET
│   │                           /api/v1/points/schedule — public action → award map,
│   │                           generated from src/points.py so UI copy cannot drift
│   ├── api_device_recommendations.py  POST .../recommend
│   ├── api_device_photos.py    device photo upload/list/report + GET /api/v1/photos/mine
│   ├── api_device_features.py  POST /api/v1/reports/device-features + GET .../features
│   ├── api_qr.py               POST /api/v1/devices/qr-scan (RETIRED 2026-10-06, not mounted)
│   ├── api_ride_screenshots.py ride transaction screenshot upload/list
│   ├── api_ride_surveys.py     POST /api/v1/tracked-rides/{id}/survey — Screen 9's
│   │                           end-of-ride survey + its three point awards (sql/052)
│   ├── api_ride_routes.py      POST /api/v1/ride-routes — Screen 4's chosen route,
│   │                           persisted only when nav_improvement consent is on (sql/052)
│   ├── api_route_feedback.py   POST /api/v1/route-feedback — navigation feedback for a
│   │                           private ride (no tracked ride, no points)
│   ├── api_reports.py          map-pin negative reports + quality feedback
│   ├── api_frontend_reports.py account-aware device/discount/model report flow,
│   │                           incl. equity receipt claims
│   ├── api_private.py          bearer-token admin JSON API (distinct from api_admin.py)
│   ├── api_admin.py            GitHub-OAuth-protected admin HTML views
│   ├── api_legal.py            static legal pages
│   └── templates/              Jinja templates for /admin (+ legal/)
├── tests/                      pytest: 162 test_*.py files, 2,200+ tests; 28 *_pg.py
│                               files (~276 tests) need a real Postgres
└── .github/workflows/deploy.yml  test (PRs + main) → build → GHCR → deploy over
                                Tailscale on push to main
```

## Data model

Core tables in Postgres, all narrow (no 270-column wide schemas). This
is the original ingest-pipeline core plus trip tracking only; accounts,
points, rides, reports, telemetry, campaigns, dibs and the analytics
rollups each have their own migrations in `sql/` — read those for the
full current set.

| Table | Purpose |
|---|---|
| `observation_cycles` | Per-cycle UUID lifecycle: start_ts, phase timestamps, job_status, errors, JSONB blob |
| `api_failures` | Upstream / archive failures with cycle_id FK |
| `raw_telemetry_points` | Per-device rolling buffer; flushed to R2 every 24h |
| `snapshot_metadata_core` | The 22 RFP-relevant metrics, plus the same total/percent fields per tracked equity group **and** per split dimension (bicycle/scooter, sitting/standing — see `src/equity_groups.py`), one row per cycle |
| `regional_metrics_narrow` | Per-region counts, one row per (cycle, region). Indexed by region_category + region_type + snapshot_time |
| `transmission_attempts` | One row per downstream POST, with http_status_code |
| `system_state` | Tiny KV (e.g. `last_archive_ts`, `address_points_refreshed_at`) |
| `trip_events` | One row per detected "successful trip" (a MOVED transition in `device_state.py`) — vehicle, from/to position, distance |
| `daily_trip_summary` | One row per Denver-local calendar day: total trips, distinct vehicles tripped |
| `daily_vehicle_trip_counts` | One row per (day, vehicle) with a trip that day: trip count + popularity rank |

### Boundary taxonomy

| `region_category` | `region_type` | rows |
|---|---|---|
| `disadvantaged_areas` | `v1` | 34 polygons (legacy hand-drawn boundary). Was the compliance metric until the city clarified the map in August 2026; it keeps its own compliance flag so the old series stays readable (see the §1.1a note under "Status" in docs/API_REQUIREMENTS.md) |
| `disadvantaged_areas` | `v2` | 65 census block groups |
| `disadvantaged_areas` | `er1`..`er6` | 34 / 58 / 157 / 93 / 114 / 116 census block groups — DOTI Equity Index, one layer per exact rank tier (er1 = highest need). Partition the scored area; tracked individually (not pre-combined) so a future compliance cutoff can be reconstructed from history. Full metric parity with v1/v2 in both `snapshot_metadata_core` and `daily_sla_compliance`, but no compliance flag — see `src/equity_groups.py`. |
| `equity_areas` | `equity` | 30 polygons (EQ_001…EQ_030) — the city's official Equity Areas, **the contractual compliance metric** (Exhibit B's 30%; `OFFICIAL_GROUP` in `src/equity_groups.py`, `sql/079`). Days before the map deployed are rebuilt by `reprocess_equity_compliance`; `null` there means not reprocessed (or unmeasurable), not zero |
| `council_districts` | `council_district` | 11 (CD_1…CD_11; At-Large overlays filtered) |
| `community_networks` | `community_network` | 13 (CN_Central, CN_Southwest, …) |
| `neighborhoods` | `neighborhood` | 78 (NB_AthmarPark, …) |

### The 22 core metrics

Stored in `snapshot_metadata_core` and exposed verbatim via
`/api/v1/snapshots/latest`. Counts: `total_devices_(denver|v1|v2)`,
`total_(bike|scooter)_(denver|v1|v2)`, `total_not_in_denver`. Percentages:
all the natural ratios — bikes_denver, scooters_v1, all_devices_v2, etc.
The same total/percent fields exist for every other tracked group
(`er1`…`er6`, `equity`), e.g. `total_devices_equity`,
`percent_all_devices_equity`.

## Configuration

**Non-secret** values live in `config.json` (committed, and bind-mounted
read-only into the containers so prod can change it without a rebuild):

- `gbfs.url`, `gbfs.vehicle_types_url`, `gbfs.timeout_seconds`, `gbfs.user_agent`
- `schedule.cycle_minutes` (2), `schedule.archive_hours` (24)
- `envelope.denver_core`, `envelope.china_glitch` bounding boxes
- `boundaries[]` — one entry per layer (12), with file path + naming rule
- `transmission.endpoints[]` — `{name, url, method, path, auth_env}` (empty today)
- `cors.allowed_origins` + `cors.allowed_origin_patterns` — strictly enforced
- `r2.bucket_name` (`veo-audit-archive`), `r2.endpoint_template`
- `auth.allowed_github_orgs` (default; env overrides), `auth.callback_url`
- `accounts.magic_link_url_template` (env `MAGIC_LINK_URL_TEMPLATE` overrides)
- `valhalla.*` — sidecar URL, timeout, `graph_bbox`, R2 object keys for the
  routing assets, `default_profile`, and the selectable routing profiles
- `geocode.upstream` / `geocode.enabled` — the Photon sidecar behind
  `/api/v1/geocode/search`; `enabled: false` is a real kill switch (the
  endpoint 503s exactly as it does when the sidecar is down)
- `pricing.tax_rate` / `currency` / `as_of` — served by `/api/v1/meta/pricing`.
  `tax_rate` is a **fraction** (0.0915, not 9.15); a value outside `[0, 1)` is
  refused and the built-in default served instead
- `device_tracking.stationary_threshold_meters`, `spatial.denver_core_buffer_meters`,
  `logging.level`

**Secrets** come from environment variables only (see `.env.example`;
in production `deploy.yml` renders them from GitHub Secrets). A variable
only reaches a container if `docker-compose.yml` lists it under that
service's `environment:`.

| Variable | Notes |
|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` (+ `POSTGRES_HOST`/`PORT`, set by compose) | database |
| `VEHICLE_IDENTIFIER_SALT` | **required** (worker and scheduler): HMAC key for `vehicle_identifier`; a cycle aborts without it. Losing it orphans every identifier ever emitted — back it up out of repo |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` | archive bucket credentials |
| `R2_RECEIPTS_BUCKET` | private bucket for receipt images (same credentials) |
| `R2_MAP_BUCKET`, `R2_MAP_ACCESS_KEY_ID`, `R2_MAP_SECRET_ACCESS_KEY` | private routing/geocoding assets bucket, with its own read token |
| `SENTRY_DSN` | optional; blank disables Sentry |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `AUTH_ALLOWED_GITHUB_ORGS`, `SESSION_SECRET` | admin panel (GitHub OAuth) |
| `GOOGLE_OAUTH_CLIENT_ID` | Google sign-in door; blank = off |
| `GOOGLE_AUTH_ENABLED` | read by `src/api_auth.py` (unset = on; falsy = force Google off). Not listed in `docker-compose.yml`, so today the containers never see it |
| `POSTMARK_TOKEN`, `POSTMARK_FROM`, `MAGIC_LINK_URL_TEMPLATE` | email sign-in doors; blank token = 503 |
| `COMMS_TOKEN`, `COMMS_BASE_URL` | z280-comms SMS door + reply poller; blank token = door off (supported) |
| `SESSION_HTTPS_ONLY` | admin session cookie `https_only` (default true). Read by `src/config.py`, but not listed in `docker-compose.yml`, so a value in `.env` does not reach the container |
| `OPENROUTER_KEY` | wired into both containers for equity receipt reading (docs/PLAN_EQUITY_RECEIPTS.md Phase 3); nothing in `src/` reads it yet |
| `ADMIN_EMAILS` | deprecated and ignored; the admin allowlist is the `admin_allowlist` table |
| `CLOUDFLARE_TUNNEL_TOKEN` | vestigial: `deploy.yml` still renders it into `.env`, but nothing consumes it since the tunnel sidecar was removed |

## Public API

No authentication required. CORS-locked for browser callers to
`scooter.fyi`, `denver.scooter.fyi`, `weseeyouveo.com` and
`keepdenverfair.com` (each with its `www.` variant where one exists),
`denver-scooter-fyi.pages.dev`, and its preview deployments
(`^https://[a-z0-9-]+\.denver-scooter-fyi\.pages\.dev$`); anything
else (curl, server-to-server) is unaffected by CORS.

### Spatial snapshots & analytics

| Endpoint | Returns |
|---|---|
| `GET /health` | `{last_data_ingest_ts, last_data_upload_ts, last_cycle_id, last_retrieval_ts}` |
| `GET /api/v1/snapshots/latest` | Latest row of `snapshot_metadata_core` |
| `GET /api/v1/spatial-snapshot?layer=…&time=…` | `{snapshot_time, layer, regions: {region_name: {total, bikes, scooters}}}` for a layer, optionally at a past time |
| `GET /api/v1/analytics/trend?layer=…&name=…&range=7d` | Time-series of counts for one region |
| `GET /api/v1/boundaries` | List of boundary layers with feature count, bbox, URL |
| `GET /api/v1/boundaries/{layer}` | Full GeoJSON FeatureCollection for one boundary layer |
| `GET /api/v1/devices/current` | GeoJSON FeatureCollection of every device's current position/quality (no plate) |
| `GET /api/v1/devices/history/hourly?days=1..14` | Fleet size per hour (last cycle in each hour), with per-model status counts where recorded |
| `GET /api/v1/devices/{vehicle_identifier}/features` | Crowdsourced feature consensus for one vehicle |
| `GET /api/v1/devices/{vehicle_identifier}/conditions` | Signed in: the standing negative-rideability reports a rider at the scooter is asked to confirm |
| `POST /api/v1/devices/{vehicle_identifier}/condition-checks` | Signed in: a condition check — resolves / reconfirms those reports after a test ride; 10 points, +40 when the feed confirms the ride |
| `GET /api/v1/vehicles/resolve?plate=…` | Plate → `{device_id, vehicle_identifier}` in the current snapshot (never the plate); 404 if none; 30/min per IP |
| `GET /api/v1/equity-estimate` | Device share inside selected equity-rank tiers from the latest snapshot |
| `GET /api/v1/h3/aggregates` | Per-H3-cell aggregates (device_count, trips_started_24h, battery, risk_share, dwell) at res 8/9/10 |

### Compliance

| Endpoint | Returns |
|---|---|
| `GET /api/v1/compliance/daily/latest` | Most recent computed daily SLA compliance window |
| `GET /api/v1/compliance/daily?date=…` | Daily SLA window for one Denver-local date |
| `GET /api/v1/compliance/daily/range` | Range of daily SLA rows, ascending |
| `GET /api/v1/compliance/calendar?month=&count=&group=equity` | Per-day `pass` / `fail` / `pending` / `unmeasurable` / `no_data` for whole months (default group `equity`) |

The daily verdict is computed for the compliance groups `v1`, `v2` and
`equity`; `equity` (the official map) is the one the contract binds.

### Fleet analytics

Public, read-only, cached (`src/api_analytics.py`,
docs/implemented/PLAN_FLEET_ANALYTICS.md). Rides, failed starts and dwell
read from the `sql/094` rollups, which the ingest cycle refreshes as its
last step; `analytics_backfill` filled the history once.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/analytics/rides` | Rides by time bucket × model, for a region |
| `GET /api/v1/analytics/failed-starts` | Failed starts by bucket × model (+ `undercount_since`) |
| `GET /api/v1/analytics/devices-by-region` | Vehicles on the map per region |
| `GET /api/v1/analytics/equity-compliance` | % of the fleet in the official Equity Areas by bucket, against the 30% line |
| `GET /api/v1/analytics/dwell` | Average dwell per region × model, with sample size |
| `GET /api/v1/analytics/fleet-status` | Available / in use / out of service / off-map |
| `GET /api/v1/analytics/fleet-counts` | Devices visible now; ever seen, by model |
| `GET /api/v1/fleet/outcomes` | Share of rentals that ended where they began, fleet + by model, since the sql/089 counter reset; plus `stayed` / `stayed_rate` (never left the spot: never more than 50 m from the unlock point, released there) since sql/099, with its own sample (`stayed_rentals`) and start (`stayed_counted_since`) |
| `GET /api/v1/fleet/outcomes/equity?window=7d\|28d` | The same, for rentals unlocked inside vs outside the official Equity Areas (incl. `stayed` / `stayed_known` / `stayed_rate` per side) |

#### Counting eras

`trip_events`, `device_history` stops and failed starts were counted three
different ways. The history cannot be corrected after the fact, so it is
labelled instead: rides, failed-starts and dwell responses carry
`counting_changes` and `comparable_since`. **Never compare figures across
these dates**:

| Period | How it was counted |
|---|---|
| until 2026-08-10 04:15 UTC | every 2-minute sample of a moving rented vehicle was a trip (about 6× over-count), and stops were split into 2-minute pieces (`8a51d4d` fixed it) |
| 2026-08-10 04:15 → 2026-10-06 01:36 UTC | one trip per rental, but GPS drift over 16 m was still a trip (about 2 in 3 trips were drift), drift restarted dwell, and failed starts were under-counted (`dc292b6` fixed it) |
| since 2026-10-06 01:36 UTC | current method; the only comparable era (`comparable_since`) |

### Routing & geocoding

Both upstreams are self-hosted sidecars in this repo's compose file — a
Denver-clipped Valhalla graph (`valhalla`) and a Colorado-scoped Photon
index (`photon`, built from `docker/photon/`, seeded from R2; see
`docs/reference/build_photon_index.md`). No third-party routing or geocoding API,
no API key, and no rider query leaves the box. Both are rate limited per IP
because a sidecar round trip is expensive.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/route?from=&to=&profile=&maneuvers=` | GeoJSON `Feature`: route geometry, distance/duration/elevation, battery estimate, and — with `maneuvers=true` — turn-by-turn cues whose shape indices are re-offset onto the returned LineString. Directions are **beta**: every response carries a `beta_warning` string clients must show to riders. 30/min per IP |
| `GET /api/v1/route/options` | Every genuinely different route to a destination, once each. Same per-IP limit as `/route` |
| `GET /api/v1/route/walk` | Walking route from the rider to the vehicle they picked. Same per-IP limit as `/route` |
| `GET /api/v1/route/profiles` | The selectable routing profiles + `graph_bbox` (config-driven; treat as the live list). 60/min per IP |
| `GET /api/v1/geocode/search?q=…&lat=…&lon=…&limit=…` | Up to 8 Denver-scoped hits as `{label, lat, lon, kind, in_coverage}`; `in_coverage` is routing-graph membership so clients can grey out un-routable picks. 20/min per IP; 503 `geocoder_unavailable` when the sidecar is down or disabled |
| `GET /api/v1/geocode/reverse?lat=…&lng=…` | What is at a point (saved places, parking reports): `{address, name, housenumber, street, locality, city, postcode}`, nulls for missing fields. Photon `/reverse` plus Denver's address points for the house number. Colorado only (400 `outside_coverage`), 404 `not_found`, 503 `geocoder_unavailable`. 60/min per IP, `Cache-Control: no-store`, coordinates never logged |

Route responses also carry `outside_city: {from, to}` and an
`outside_city_warning` string (null when both ends are inside Denver).
Geocoding answers house-number queries from Denver's own address points
(`src/addresses.py`, `sql/074`, refreshed weekly by
`refresh_address_points`) and asks Photon for everything else
(businesses, parks, landmarks), because OSM lacks most Denver addresses.

### Leaderboard

FEATURE_PLAN §11 (docs/implemented/FEATURE_PLAN_2026-07.md): territory
control over H3 r8 cells for the trailing 28 days. Since `sql/061` both
boards are **computed live from `user_points` on every request**
(`src/api_leaderboard.py`); the only precomputed part is the all-time cell
universe, refreshed weekly (Monday 09:15, `refresh_area_universe`) and
unioned with whatever has points in the window. Privacy is applied at read
time: an account's `show_in_leaderboards`/`show_public_username` choice
(or a never-backfilled `display_name`) takes effect on the very next
request, and a skipped rank falls through to the next eligible earner.
Both send a content-only weak ETag (`W/"arealb:<hash>"`) and
`Cache-Control: public, max-age=30`.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/leaderboard/map` | `{computed_at, window_start, window_end, cells: {"<h3 string>": {total_points, distinct_earners, leader, runners_up}}}` — eligible top 3 per cell in one fetch. `total_points`/`distinct_earners` count every earner and are not privacy-filtered |
| `GET /api/v1/leaderboard/regional` | The same window ranked across the whole database: top 25 eligible accounts |

### Sign-in

The first half of each door in `src/api_auth.py` — public because you use
them before you have a session. The session-management half
(`refresh`/`session`/`signout`) is bearer-gated; see Rider API below.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/auth/config` | Public sign-in capability flags + Google client id |
| `POST /api/v1/auth/google` | Google ID token → session |
| `POST /api/v1/auth/magic-link` | Email → Postmark magic link (always 202) |
| `POST /api/v1/auth/redeem` | Magic-link token → session |
| `POST /api/v1/auth/code` | Email → Postmark `AA000AA` sign-in code (always 202) |
| `POST /api/v1/auth/code/verify` | Email + code → session |
| `POST /api/v1/auth/sms/code` | US phone → z280-comms `AA000AA` sign-in code |
| `POST /api/v1/auth/sms/code/verify` | Phone + code → session (and marks the number verified) |

#### SMS, via z280-comms

Texts go through [z280-comms](https://github.com/z280/comms) rather than
straight to a handset. Set `COMMS_TOKEN` (and optionally `COMMS_BASE_URL`)
to switch the door on; leave it blank and `/auth/config` reports
`sms_enabled: false`, the SMS endpoints `503`, and the reply poller no-ops.
That is a supported configuration, not a broken one.

Four things about it will surprise you if nobody says them:

* **One phone number serves several applications.** Comms adds the
  `scooter.fyi: ` prefix server-side, which is how a recipient tells our
  text apart from another application's. Never put the site name in a
  message body — it would be said twice.
* **Our STOP/UNSTOP reading is a mirror, not a judgement.**
  `comms_replies.classify` reproduces comms' rule exactly — a STOP prefix
  blocks, exactly UNSTOP clears — and the table in
  `tests/test_comms_replies.py` fails if the two drift. Widening it locally
  is the tempting mistake: we'd mark someone opted out while comms kept
  accepting sends, and every *other* application on that number would go on
  texting a rider who used a carrier-standard keyword.
* **Consent is global and enforced upstream.** Someone who texted STOP to
  *any* application on that number cannot be messaged by us: the send comes
  back `409`. You will see it for people who have never had an account
  here. The `409` body is written to be shown to a human and names the
  exact keyword and number that unblock — pass it through verbatim.
* **Replies are polled, and polling claims.** `python -m src.cli
  poll_comms_replies` (cron, every 5 min) is the only thing that will ever
  see a rider's STOP; nothing is redelivered. A row in `comms_replies` with
  `handled_at IS NULL` means we collected a message and failed to finish
  with it — that's the query a human should watch.
* **Nothing is guaranteed delivered.** A `202` means accepted, and
  `fell_back: true` means the message went out on the handset with no
  delivery confirmation ever to follow. Sign-in codes are safe under this
  (the rider just asks for another); anything that *must* not fail silently
  needs a non-SMS path.

Phone numbers written through `PUT /api/v1/profile` are **unverified** —
contact details, not proof. Only typing back a texted code sets
`phone_verified`, and only a verified number can sign in. See
`sql/045_sms_login_codes.sql` for why that distinction is load-bearing.

### Reports (public submission & aggregates)

| Endpoint | Returns |
|---|---|
| `POST /api/v1/reports` | Submit a citizen negative report (map pin) |
| `POST /api/v1/quality-feedback` | Public positive/negative feedback on a shown quality designation |
| `POST /api/v1/reports/device` | Rider device-failure report (anonymous allowed, tightly rate-limited) |
| `POST /api/v1/reports/device-features` | Confirm what's fitted to a vehicle (basket, bell, …); anonymous allowed, points only for a signed-in reporter. Graded by `process_device_feature_reports` |
| `POST /api/v1/reports/model` | "This model is unrecognised — here's what it is"; feeds a review queue (sql/038). Anonymous allowed |
| `POST /api/v1/route-feedback` | Navigation feedback for a private ride (no tracked ride; sql/068). Never awards points |
| `GET /api/v1/reports/summary` | Per-region report aggregate (~10 min cache) |
| `GET /api/v1/reports/export/monthly.csv` | Monthly CSV export for DOTI/journalists |

### Dibs

A timestamped claim on a scooter, with a certificate another person can
verify on their own phone (`src/api_dibs.py`, `sql/076`). No account
needed; claims last at most 25 minutes; registering is rate limited.

| Endpoint | Returns |
|---|---|
| `POST /api/v1/dibs` | Register a claim → certificate links |
| `POST /api/v1/dibs/{dibs_id}/release` | Give a claim back early (expires the row; history is kept) |
| `GET /api/v1/dibs/live` | Every live claim in the city, keyed by vehicle |
| `GET /api/v1/dibs/vehicle/{vehicle_identifier}` | Live claim on one vehicle, if any |
| `GET /api/v1/dibs/{dibs_id}` / `.../qr.svg` | The claim as data / its certificate QR |
| `GET /dibs/{dibs_id}` | HTML verification page behind the QR (also the app's referral front door) |
| `POST /dibs/{dibs_id}/stand-down`, `POST /dibs/{dibs_id}/refer` | The verification page's two forms |

### Telemetry

`POST /api/v1/telemetry/events` (always `204`, not in the OpenAPI schema)
takes small batches of allowlisted frontend events. Unknown event names are
dropped, props are truncated, and the only identity is a `visitor_hash`
over a per-day salt; no IP or user-agent is stored. Request metrics are
captured separately by middleware (`src/request_metrics.py`: route
template, status, latency, coarse device, no IP). Campaign codes
(`utm_campaign`) are resolved against the `/admin/campaigns` registry, so
the stored value is always a known code, `other` or `none`. Retention:
raw `telemetry_events` 90 days, raw `request_metrics` 30 days,
`telemetry_salt` 2 days (destroying the salt is what makes
`visitor_hash` irreversible); the daily rollups are aggregate and kept.

### Other

| Endpoint | Returns |
|---|---|
| `GET /` | JSON banner listing many of the mounted endpoints (discovery only; not exhaustive) |
| `GET /api/v1/meta/privacy` | Machine-readable data retention policy |
| `GET /api/v1/meta/pricing` | Sales-tax rate for the Ride Mode cost breakdown (config-driven, `"pricing"` block) |
| `GET /legal/terms-of-service` | Static Terms of Service page |
| `GET /legal/privacy-policy` | Static Privacy Policy page |

## Rider API

Requires `Authorization: Bearer <token>` from one of the sign-in doors
above. Every endpoint below is open to any signed-in rider. There is no
paid tier and no purchasable status — signed-in and admin are the only
two gates in this system (`sql/036_decommercialize.sql`).

### Session & profile

| Endpoint | Returns |
|---|---|
| `POST /api/v1/auth/refresh` | Rotate the presented bearer token |
| `GET /api/v1/auth/session` | Session introspection for UI state |
| `POST /api/v1/auth/signout` | Revoke the presented token |
| `GET /api/v1/profile` | Full rider profile incl. server-computed badges/public username/`display_name` |
| `PUT /api/v1/profile` | Partial update of `rate_plan`/`theme`/`favorites`/`email`/`phone_number`/`show_public_username`/`show_in_leaderboards`/`home_lat`/`home_lng`/`work_lat`/`work_lng`/`royalty_title`/`ruling_color`/`ruling_border_color` |
| `POST /api/v1/profile/username/regenerate` | Re-roll your public username to a new random adjective+emoji pair |
| `PUT /api/v1/profile/username` | Choose a specific adjective and/or emoji (partial update) |
| `POST /api/v1/profile/phone/code` | Text a code to prove you answer your listed number |
| `POST /api/v1/profile/phone/verify` | Type it back → `phone_verified` on **this** account |
| `GET /api/v1/profile/map-settings` | Every saved map setting for the caller |
| `GET /api/v1/profile/map-settings/{name}` | One saved map setting |
| `PUT /api/v1/profile/map-settings/{name}` | Create or replace a named map setting (opaque JSON blob) |
| `DELETE /api/v1/profile/map-settings/{name}` | Delete a named map setting |
| `GET /api/v1/profile/ride-usuals` | Every saved ride-mode "Usual" (options preset) for the caller |
| `GET /api/v1/profile/ride-usuals/{name}` | One saved Usual |
| `PUT /api/v1/profile/ride-usuals/{name}` | Create or replace a named Usual (opaque JSON blob: `ride_options` + `label`); 10 per account |
| `DELETE /api/v1/profile/ride-usuals/{name}` | Delete a named Usual |
| `GET /api/v1/profile/ride-specs` | Every saved ride spec ("ideal scooter") for the caller |
| `GET /api/v1/profile/ride-specs/{name}` | One saved spec |
| `PUT /api/v1/profile/ride-specs/{name}` | Create or replace a named spec (opaque JSON blob: requirements + which are `must`); 5 per account |
| `DELETE /api/v1/profile/ride-specs/{name}` | Delete a named spec |
| `GET /api/v1/profile/favorite-devices/retired` | **My Scooters was retired 2026-10-06.** Lists what the caller kept before then: identifiers, nicknames and dates only, so it can be deleted |
| `DELETE /api/v1/profile/favorite-devices/{vehicle_identifier}` | Delete one kept vehicle (the privacy policy's promise outlives the feature) |
| ~~`GET/POST/PATCH /api/v1/profile/favorite-devices…`~~ | Retired 2026-10-06, not mounted (`api_favorites._retired`) |
| `GET /api/v1/emoji-nouns` | Full emoji → noun-word list, for building a username picker |
| `GET /api/v1/emoji-nouns/search?q=…` | Partial word match on the emoji-noun list |
| `GET /api/v1/adjectives` | Full curated adjective list |
| `GET /api/v1/adjectives/search?q=…` | Partial word match on the adjective list |
| `GET /api/v1/royalty-titles` | Curated titles that can prefix a public username |
| `GET /api/v1/royalty-titles/search?q=…` | Partial match on the title list |
| `GET /api/v1/ruling-colors` | The 128-colour leaderboard palette + already-claimed (fill, border) pairs |
| `GET /api/v1/user/devices/current` | Signed-in device map feed; adds plate/admin fields for admin-allowlisted sessions |
| `GET /api/v1/vehicles/plates?device_ids=…` | `{plates: {device_id: plate}, as_of}` for ≤50 ids in the current snapshot (replaces the browser's direct Veo GBFS fetch) |
| `POST /api/v1/reports/discount` | Missed-discount evidence, optional receipt upload. Multipart carrying any receipt-claim field is an **equity receipt claim** (below) |

### Equity receipt claims (Phase 1: capture)

docs/PLAN_EQUITY_RECEIPTS.md is the spec. A multipart
`POST /api/v1/reports/discount` that carries any of `vehicle_plate`,
`trip_minutes`, `subtotal_cents`, `total_cents` or `charge_date` is routed
to `src/receipt_claims.py` (`sql/093`, `sql/095`):

- **The gate.** Plate, trip minutes, a cost (pre-tax or with-tax) and the
  charge date are all required. Anything less is declined and **nothing is
  kept**: no row, no image (the gate runs before any upload).
- **No location or time of day is required**, because a Veo receipt has
  neither. Pins and an approximate start time are optional tie-breakers.
  The declared rate plan is trusted; the plan screenshot `sql/093` required
  was dropped by `sql/095`.
- **Arithmetic is recorded, not judged.** The expected equity price, rate
  error, rate signature and tax check are stored at submission. Phase 1
  awards no points and tells nobody they were overcharged; ride matching
  (Phase 2) and automated review (Phase 3) are later phases.
- Receipt images go to the private receipts bucket and are purged after
  18 months (`cleanup_receipts`). The public monthly CSV carries
  `vehicle_identifier`, never the plate, and rounds pins to 3 decimals.

### Off-feed rides (vehicles we don't track)

Rides on a vehicle with no `vehicle_identifier` — a personal scooter, a
competitor's rental, a friend's e-bike. Repurposed from the old
supporter-only ride log (`sql/035_off_feed_rides.sql`); no longer gated,
and now a full lifecycle rather than a single POST. No points are awarded
here — the data is rider-asserted about a vehicle we can't corroborate.

| Endpoint | Returns |
|---|---|
| `POST /api/v1/rides/start` | Begin a ride (409 if one is already active) |
| `GET /api/v1/rides/active` | The caller's one active off-feed ride, or null |
| `POST /api/v1/rides/{ride_id}/waypoints` | Append a GPS fix; rebuilds polyline + distance |
| `GET /api/v1/rides/{ride_id}/waypoints` | Owner-only paginated waypoint list |
| `PATCH /api/v1/rides/{ride_id}/end` | Report the end (single-shot) |
| `POST /api/v1/rides` | One-shot log of an already-finished ride |
| `GET /api/v1/rides` | Owner-only paginated list, newest first |
| `GET /api/v1/rides/export?format=geojson\|csv` | Owner-only export |
| `DELETE /api/v1/rides/{ride_id}` | Hard-delete one ride (cascades to waypoints) |
| `DELETE /api/v1/rides` | Hard-delete every off-feed ride the account owns |

An active ride expires 24 hours after it was created
(`sql/040_off_feed_ride_expiry.sql`, swept by `expire_stale_off_feed_rides`
every 15 minutes). Without it, a rider who never reports an end holds the
one-active-ride slot forever and can never start another ride — the
partial unique index has no other way to let go. An expired ride keeps its
waypoints and its measured distance, never gains an invented end, and
earns no badge mileage.

`src/badges.py` computes the mileage/streak badges from **both** this
table and `tracked_rides` — a rider's mileage is the miles they rode,
whichever mechanism recorded them. Only rides someone *ended* count, from
both tables. Because the one-shot `POST /api/v1/rides` lets the client
assert its own distance, that number is checked for plausibility
(ride-average speed ≤ 20 m/s, and consistent with the submitted polyline)
before it is stored, so counting off-feed mileage doesn't mean believing
arbitrary mileage.

### Tracked rides (GBFS-detected, all riders)

Server-detected ride tracking: you declare a ride start, a watch list
compares the device against every GBFS ingest cycle for up to 3 hours to
detect it leaving/rejoining the feed, and you separately report your own
end. See `sql/027_tracked_rides.sql` / `src/ride_watch.py`. Ride-mode
track donation, verification and validation-finishing live in
`sql/051_track_donations.sql` / `src/track_verify.py` / `src/battery_model.py`.

| Endpoint | Returns |
|---|---|
| `POST /api/v1/tracked-rides` | Start a ride + watch; optional `ride_options` (≤4 KB) and `reported_start_battery_percent`. Returns `track_signing` (per-ride HMAC key, owner-only) + `validation` (404 unknown device, 409 if one's already active, 413 options too large, 422 bad options) |
| `GET /api/v1/tracked-rides?limit=&before=&status=` | Owner-only paginated list |
| `GET /api/v1/tracked-rides/active` | The caller's one active ride, or `{"active": null}` |
| `GET /api/v1/tracked-rides/{ride_id}` | Full detail incl. decoded `path_geojson`; GBFS fields hidden until you report your own end |
| `PATCH /api/v1/tracked-rides/{ride_id}/end` | Report your end location/battery/cost/metadata plus `reported_minutes` (0–1440) and `reported_plan` (`resident\|visitor\|equity`); sets a provisional `validation.status` (single-shot). No longer credits points (superseded — see `POST .../track` below) |
| `POST /api/v1/tracked-rides/{ride_id}/track` | Bulk track donation: verifies the signed waypoint chain (`src/track_verify.py`), stores it, awards `battery_contribution`/`nav_distance_bonus`, and feeds the battery model. Owner-only, 6/hour, ≤2 MB / 600 batches. 404 not yours, 409 not ended / already donated, 422 not opted in / chain invalid |
| `POST /api/v1/tracked-rides/{ride_id}/waypoints` | **Deprecated** — append a GPS waypoint while the ride is active. Superseded by `POST .../track`; earns no points |
| `GET /api/v1/tracked-rides/{ride_id}/waypoints?limit=&before=` | Paginated waypoint list |
| `DELETE /api/v1/tracked-rides/{ride_id}` / (bare) | Hard-delete one ride / every ride you own, including its screenshot images in R2 |
| `POST /api/v1/tracked-rides/{ride_id}/screenshots?screenshot_type=overview\|receipt` | Upload a transaction screenshot (overwrites the same slot) |
| `GET /api/v1/tracked-rides/{ride_id}/screenshots` | List your screenshots for a ride |
| `POST /api/v1/tracked-rides/{ride_id}/survey` | Screen 9's end-of-ride survey — scooter-feedback + navigation-feedback panes, single-shot. Awards `ride_survey`/`nav_route_feedback`/`nav_qualitative_feedback`. 404 not yours, 409 not ended / already submitted, 422 bad issue / bad model_bonus / bad ride_route_id. See `src/api_ride_surveys.py` |

### Ride routes

Screen 4's chosen route, stored (only when `ride_options.nav_improvement`
is on) so the end-of-ride survey above can rate it and `nav_distance_bonus`
can confirm a route exists. See `sql/052_ride_surveys_routes.sql` /
`src/api_ride_routes.py`.

| Endpoint | Returns |
|---|---|
| `POST /api/v1/ride-routes` | Persist a chosen route; `tracked_ride_id` null in the normal flow, or a ride you own (else 404). 400 unknown profile / bad polyline (<2 decoded points) / out of routing-graph coverage; 422 out-of-bound `distance_meters`/`duration_seconds`/`battery_percent_estimate`. No uniqueness on `tracked_ride_id` — multiple routes per ride is intended. 30/hour per account. → `{ ride_route_id }` |

### Points & device engagement

| Endpoint | Returns |
|---|---|
| `GET /api/v1/points?limit=&before=` | Your points ledger + running total |
| `GET /api/v1/points/schedule` | **Public** — authoritative action → points map incl. formulas; UI copy is generated from it |
| `POST /api/v1/devices/{vehicle_identifier}/recommend` | Yes/no — only accepted with a completed ride on that device in the last 24h |
| ~~`POST /api/v1/devices/qr-scan`~~ | Retired 2026-10-06, not mounted: its 100-point bonus was farmable because plates are public. QR scans still work for feature reports (`qr_raw_value`) via `src/qr.py`. |

### Device photos

Public content — capped at 3 photos per device, attributed to the
uploader's public username. See `sql/031_device_photos.sql`.

| Endpoint | Returns |
|---|---|
| `POST /api/v1/devices/{vehicle_identifier}/photos` | Upload (multipart `photo`); 409 at the 3-photo cap, 503 if storage isn't configured |
| `GET /api/v1/devices/{vehicle_identifier}/photos` | List a device's photos |
| `POST /api/v1/photos/{photo_id}/reports` | Report a problem with a photo (distinct from reporting the device itself) |
| `GET /api/v1/photos/mine` | Everything you've uploaded — device photos and ride transaction screenshots together |

## Private API

Bearer-token JSON endpoints gated on `require_admin` (session email must be
on the `admin_allowlist` table, reachable via either sign-in door) —
**distinct from the Admin panel below**, which is a separate GitHub-OAuth
HTML portal with its own login flow.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/private/devices/lookup` | Resolve plate ↔ identifier + current state row |
| `GET /api/v1/private/devices/lookup-batch` | Batch plate → max observed range lookup |
| `GET /api/v1/private/devices/{vehicle_identifier}/history` | Time-ordered position-stop history for one scooter |
| `GET /api/v1/private/devices/max-ranges` | Devices sorted by highest-ever observed range |
| `GET /api/v1/private/trips/daily` | Daily trip/popularity rollup for one Denver-local date |
| `GET /api/v1/private/area-leaders` | Full, unfiltered §11 leaderboard, computed live: every earner per cell with real account ids/points/tie-break provenance — no privacy filtering |
| `GET /api/v1/private/regional-leaders` | The whole-database board, unfiltered |
| `GET`/`POST`/`DELETE /api/v1/private/admins` | List / add / remove admin-allowlist emails |
| `GET /api/v1/private/analytics/daily` | `telemetry_daily` rollup rows |
| `GET /api/v1/private/analytics/events?name=` | Daily counts for one telemetry event |
| `GET /api/v1/private/analytics/requests/daily` | `request_metrics_daily` rows |
| `GET /api/v1/private/reports` | Admin listing of all negative reports |
| `GET /api/v1/private/quality-feedback` | Admin listing of all quality feedback |

## Admin panel

At `https://data.scooter.fyi/admin`, behind GitHub OAuth — deliberately a
separate door from rider auth, and **staying that way** (the proposal to
retire it in favour of the bearer/`admin_allowlist` path was withdrawn
2026-07-28). Reached through the `ovh3-ingress` network like the rest of
`data.scooter.fyi` — the host does not expose port 80 or 443 to the
internet. Users must be members of an org in
`AUTH_ALLOWED_GITHUB_ORGS`. HTML views (GET unless noted):

- `/admin/login` — start GitHub OAuth login
- `/admin/auth/callback` — GitHub OAuth callback
- `/admin/logout` — clear the admin session
- `/admin` — index of every admin page (signed in), or a redirect to `/admin/login`
- `/admin/cycles` — paginated cycle log with status colors
- `/admin/cycles/{cycle_id}` — every phase timestamp, JSONB blob,
  transmission attempts, related failures
- `/admin/failures` — recent `api_failures` rows
- `/admin/scheduler` — every scheduled command with its schedule from the
  active crontab and its last run, status and summary from the `job_runs`
  ledger (`sql/062`); the ingest cycle's own record is `/admin/cycles`
- `/admin/scheduler/edit` — crontab textarea editor form
- `/admin/scheduler/edit` (POST) — validate (via `supercronic -test`) and save/reset the crontab
- `/admin/regions?layer=…` — current snapshot's per-region counts
- `/admin/admins` — admin allowlist management view
- `/admin/admins/add` (POST) — add an email to the admin allowlist
- `/admin/admins/remove` (POST) — remove an email from the admin allowlist
- `/admin/analytics?days=` — telemetry and request-metrics dashboard
- `/admin/campaigns` — campaign registry + attribution;
  `/admin/campaigns/{code}/qr.png` and `.../qr.svg` — the campaign's QR;
  `/admin/campaigns/add` and `/admin/campaigns/archive` (POST)

Fleet reports admin centre (docs/FLEET_REPORTS_PLAN.md Phase 2). Writes from
these pages are attributed to the GitHub login; every POST is CSRF-checked:

- `/admin/fleet` — the fleet-reports index
- `/admin/fleet/reports?report_type=&reason=&region=&standing=&status=&page=` —
  reports queue: type, reason, observed/reported times, reporter (account id +
  public username), charge at report and whether it moved since, standing /
  suppressing status, near-duplicates
- `/admin/fleet/reports/{id}/resolve` (POST, `resolution`) — audited void /
  resolve; `/admin/fleet/reports/{id}/reinstate` (POST, `reason`) — undo a
  rider condition check's resolution (an admin's is final)
- `/admin/fleet/devices/{vehicle_identifier}?days=14` — per-scooter dossier:
  reports, condition checks, "repeatedly hidden at the same spot", feature
  consensus and broken parts, census ack and note, moves and idle time,
  hourly battery, SMS watches
- `/admin/fleet/reporters?days=30&account_id=` — per-account report volume
  and spread (types, vehicles, cells, days, hours) with rider resolutions
- `/admin/fleet/watches` — SMS watch on one vehicle (an allowlisted admin
  account's verified phone, consent tick, STOP, ≤ 20 texts, ≤ 7 days);
  POST to subscribe, `/admin/fleet/watches/{id}/unsubscribe` (POST) to stop
- `/admin/fleet/census?list=arrivals|missing|gone&hours=72` — the census, with
  `/admin/fleet/census/{vid}/ack`, `/unack` and `/note` (POST)
- `/admin/fleet/export?window_days=30&unmoved_days=7` — the advocacy export;
  `/admin/fleet/export.csv?table=summary|inaccessible` downloads it

## Run locally

```bash
cp .env.example .env
# Minimum: POSTGRES_USER/PASSWORD/DB and VEHICLE_IDENTIFIER_SALT (any fixed
# string locally; the ingest cycle aborts without it). Leave R2 / Sentry /
# OIDC / Postmark / comms blank.
cp docker-compose.override.yml.example docker-compose.override.yml
# ^ publishes pipeline_worker on localhost:8080 (and photon on :2322).

# All three networks in docker-compose.yml are EXTERNAL (owned by other
# stacks in production), so compose will not create them. Locally:
docker network create scooter-internal
docker network create comms-api
docker network create ovh3-ingress

docker compose up --build denver_spatial_db pipeline_worker scheduler

curl localhost:8080/health   # 4-key JSON
curl localhost:8080/api/v1/snapshots/latest   # 503 until the first cycle lands
```

The ingest runs in the `scheduler` container, not the worker; the first
cycle lands on the next `*/2` tick (`docker compose logs -f scheduler`).
Routing and geocoding need the `valhalla`/`photon` pairs, whose fetch
sidecars need the private R2 map bucket. `SESSION_HTTPS_ONLY=false` (for
the admin OAuth flow over plain HTTP) has to be added to
`pipeline_worker`'s `environment:` in your override file; compose does not
pass it through from `.env`.

## Run tests

```bash
python3.11 -m pip install -r requirements.txt pytest
python3.11 -m pytest -q
# 2,200+ tests across 162 test_*.py files. Most run with no real Postgres (a
# fake cursor/connection is monkeypatched in; tests/conftest.py sets a fixed
# salt and VEO_CONFIG) — test_compute_sql exercises the real DuckDB spatial
# join, and the 28 files ending _pg.py additionally skip unless
# VEO_TEST_PG_DSN points at a real, migratable Postgres instance.
```

### Running the `_pg.py` tests

The ~276 `_pg.py` tests are the only coverage of the `sql/` files as Postgres
actually executes them — the guarded `DO $$` constraint blocks, the partial
unique indexes, and `test_migration_replay_pg.py`'s replay-over-live-data
check are all invisible to a fake cursor. **Skipped is not passed**: run them
before shipping a migration. (CI runs them on every PR — see Deploy.)

Any reachable, migratable Postgres works. Without a Docker daemon, `pgserver`
ships a server as a wheel:

```bash
python3.11 -m pip install pgserver   # dev-only; deliberately NOT in
                                     # requirements.txt (it bundles Postgres
                                     # binaries the app image must not carry)
python3.11 - <<'PY'
import pgserver
db = pgserver.get_server('/tmp/veopg', cleanup_mode=None)  # None = outlive this process
db.psql('CREATE DATABASE veotest;')
print(db.get_uri(database='veotest'))
PY

VEO_TEST_PG_DSN='postgresql://postgres:@/veotest?host=/tmp/veopg' \
  python3.11 -m pytest -q          # expect 0 skipped
```

`cleanup_mode=None` is load-bearing: the default reference-counts the server
and shuts it down when the starting process exits, so the DSN goes dead and
every `_pg.py` test silently skips again.

## Deploy

`.github/workflows/deploy.yml` runs on every pull request to `main` and on
every push to `main`. A PR gets only the `test` job; a push runs all three:

1. **test** (PRs and `main`) — against a fresh `postgres:15` service:
   `scripts/check_migration_numbers.py` (fails on a duplicate `sql/NNN_`
   number, including against other open PRs), `python -m src.cli migrate`
   on the clean database (the only place migrations run end to end before
   production), then `pytest tests/` with `VEO_TEST_PG_DSN` set, so the
   `_pg.py` suites run rather than skip
2. **build-push** (`main` only) — builds the image and pushes
   `ghcr.io/z280/scooter-fyi-api:latest` plus a `sha-<commit>` tag
3. **deploy** (`main` only) —
   - joins the runner to the tailnet (Tailscale OAuth client, `tag:ci`):
     the box's public :22 is closed, so SCP/SSH go over Tailscale
   - SCPs `docker-compose.yml`, `config.json`, `sql/`, `docker/` to
     `/opt/veo-audit/` — `docker/` because the `photon` sidecar is built on
     the box from `docker/photon/`, not pulled from GHCR
   - SSHes in, renders `.env.new` from GitHub Secrets, `docker compose pull`s
     with it, and only then `mv`s it over `.env` (a failed pull leaves the
     live `.env` and the running stack untouched); builds `photon` (a
     layer-cache no-op unless its Dockerfile moved); `docker compose up -d
     --remove-orphans`
   - runs one `ingest_cycle` synchronously in the `scheduler` container
     (failure tolerated) so `/health` has fresh data
   - health check: `docker compose exec -T pipeline_worker curl -fsS
     http://localhost:8080/health` — fails the workflow if not green

There is no separate migration step: `pipeline_worker` applies any new
`sql/` files at boot.

Renaming the repo? See the
[post-rename operator checklist](docs/reference/MIGRATION.md#post-rename-operator-checklist).

Required GitHub Secrets:

| Secret | Notes |
|---|---|
| `VPS_HOST` | ovh3's **tailnet** address (MagicDNS name or 100.x IP), not its public IP |
| `VPS_USER`, `VPS_SSH_KEY` | dedicated passwordless ed25519 keypair |
| `TSC_OAUTH_CLIENT_ID`, `TSC_OAUTH_SECRET` | Tailscale OAuth client (scope `auth_keys`); the ACL must let `tag:ci` reach the box on :22 |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Postgres credentials |
| `VEHICLE_IDENTIFIER_SALT` | required; see Configuration |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` | Cloudflare R2 archive token |
| `R2_RECEIPTS_BUCKET` | private receipts bucket |
| `R2_MAP_BUCKET`, `R2_MAP_ACCESS_KEY_ID`, `R2_MAP_SECRET_ACCESS_KEY` | routing/geocoding assets bucket + its read token |
| `SENTRY_DSN` | optional; blank disables Sentry |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | GitHub OAuth App; callback `https://data.scooter.fyi/admin/auth/callback` |
| `AUTH_ALLOWED_GITHUB_ORGS` | comma-separated, e.g. `z280` |
| `SESSION_SECRET` | `openssl rand -hex 32` |
| `GOOGLE_OAUTH_CLIENT_ID` | Google sign-in door |
| `POSTMARK_TOKEN`, `POSTMARK_FROM`, `MAGIC_LINK_URL_TEMPLATE` | email sign-in doors |
| `COMMS_TOKEN`, `COMMS_BASE_URL` | z280-comms |
| `OPENROUTER_KEY` | owner's OpenRouter key (receipt reading, later phases) |
| `ADMIN_EMAILS`, `CLOUDFLARE_TUNNEL_TOKEN` | still referenced by the workflow, consumed by nothing (see Configuration) |

The box needs Docker, the three external networks (`scooter-internal`,
`comms-api` from z280/comms, `ovh3-ingress` from
z280/cloudflare-management), a deploy user that can run `docker`, and the
SSH public key in `~/.ssh/authorized_keys`. Everything else (image,
config, schema) is pushed by the workflow. The public hostname
(`data.scooter.fyi` → `pipeline_worker:8080`) lives in
z280/cloudflare-management; adding or changing one is done there, not here.

## Schedule

`crontab` is the **seed** schedule. The live one is the admin-editable copy
at `/app/state/crontab` on the `scheduler_state` volume, written once from
`crontab` on first boot and edited at `/admin/scheduler/edit`; changing
`crontab` in the repo does not change production. Times are
America/Denver. Summary (read `crontab` for each job's reasoning):

| When | Command |
|---|---|
| every 2 min (+ 06:05) | `ingest_cycle` |
| every 5 min | `poll_comms_replies` |
| :08/:18/:28/:38/:48/:58 | `process_device_feature_reports` |
| every 15 min | `expire_stale_watches`, `expire_stale_off_feed_rides` |
| hourly at :15 | `deidentify_donations` |
| 01:30 | `extract_battery_trips` (must precede the archive) |
| 02:00 | `archive_if_due` (acts once 24 h have passed) |
| 03:30 | `cleanup_receipts`, `cleanup_ride_screenshots`, `cleanup_model_report_photos`, `cleanup_job_runs` |
| 03:45 | `cleanup_telemetry` |
| 04:30 | `refresh_routing_graph` |
| 05:00 | `refresh_photon_index` |
| 09:00 / 09:02 | `daily_trips` / `daily_sla` |
| 09:20 | `rollup_analytics` |
| 09:40 | `reprocess_equity_compliance` |
| Sun 03:50 | `sweep_orphan_images --apply` |
| Sun 04:15 | `refresh_address_points` |
| Mon 05:45 | `train_battery_model` |
| Mon 09:15 | `refresh_area_universe` |

## CLI reference

`python -m src.cli <command>` (in production: `docker compose exec
scheduler python -m src.cli <command>`). Commands in the table above run on
schedule; the rest are by hand. Defined in `src/cli.py`.

| Purpose | Commands |
|---|---|
| Ingest + archive | `ingest_cycle`, `archive_if_due` |
| Daily rollups | `daily_sla`, `daily_trips`, `rollup_analytics` |
| Equity compliance | `reprocess_equity_compliance` (scheduled, 14-day lookback); `equity_backfill <start> [end] [--full-day] [--dry-run]` (explicit range) |
| Fleet analytics | `analytics_backfill` (one-time, resumable; the ingest keeps the rollups current) |
| Rides + points | `expire_stale_watches`, `expire_stale_off_feed_rides`, `deidentify_donations`, `refresh_area_universe`, `process_device_feature_reports`, `backfill_ride_distances_from_donations` |
| Battery model | `extract_battery_trips`, `train_battery_model`, `backfill_battery_trips` (manual; needs a raised memory limit) |
| Routing + geocoding assets | `fetch_map_pbf`, `refresh_routing_graph`, `fetch_photon_index`, `refresh_photon_index`, `refresh_address_points` |
| Retention | `cleanup_receipts`, `cleanup_ride_screenshots`, `cleanup_model_report_photos`, `cleanup_job_runs`, `cleanup_telemetry`, `sweep_orphan_images [--apply] [--force]` (unreferenced user images, 7-day grace; weekly) |
| Messaging | `poll_comms_replies` |
| Accounts + admin | `admin list` / `admin add <email>` / `admin remove <email>`, `backfill_public_usernames`, `delete_account --account-id N [--apply]` ([runbook](docs/reference/account_deletion.md)) |
| Schema + repair | `migrate`, `close_ghost_stops [--dry-run] [YYYY-MM-DD ...]` (one-off) |

## Operating tips

- **First cycle**: there is no boot job. The first cycle runs on the
  scheduler's next `*/2` tick (a deploy also fires one synchronously).
  Watch `docker compose logs -f scheduler`; per-cycle detail is at
  `/admin/cycles`, every other job at `/admin/scheduler`.
- **Stale upstream**: if Veo's `last_updated` hasn't changed since the
  previous cycle, the cycle aborts with `job_status='stale_aborted'`
  and a row in `api_failures`. In practice this never fires on Veo's
  feed, which is generated per request (`last_updated` is stamped at fetch
  time, `ttl=0`).
- **Failures**: Sentry gets every uncaught exception (tagged with
  `cycle_id`). `api_failures` is the authoritative audit log.
- **Archive**: the 24-hour job is idempotent — it only truncates after
  R2 returns HTTP 200. If R2 is unreachable, `raw_telemetry_points`
  just keeps growing until the next attempt.
- **Retention**: receipts, ride screenshots and model-report photos 18
  months; `job_runs` 30 days; raw telemetry 90 days, request metrics 30
  days, the telemetry salt 2 days; donated ride tracks lose account linkage
  4 h after their points settle, and never later than 28 h after donation.
  The machine-readable policy is `GET /api/v1/meta/privacy`.
- **Backups**: this repo runs no Postgres backup — nothing in `crontab`
  or `deploy.yml` dumps the database (`pg_dump` appears only in the
  one-off host-move tooling, `scripts/migrate-state.sh` and
  docs/reference/MIGRATION.md). The R2 archive holds raw points only, not
  the database. Whatever protects `veo-audit_pgdata` lives outside this
  repo. Back up `VEHICLE_IDENTIFIER_SALT` out of repo as well: without it,
  every stored `vehicle_identifier` is orphaned.
- **Schema changes**: drop a new `sql/NNN_*.sql` file. `src/pg.py`
  applies anything not in `schema_migrations` at boot. All migrations
  use `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` for
  belt-and-suspenders re-runnability.
- **Picking a migration number**: the highest number in `sql/` plus one,
  three digits, zero-padded — and check open PRs first, because two
  branches written in parallel will both pick the same "next" number
  (that is how `061` and `069` each ended up with two files). When
  several branches are in flight at once, reserve a number for each up
  front. `tests/test_migration_numbering.py` fails on a new duplicate. If
  one slips through, renumber the file that has **not** merged; never
  rename one that has shipped — `schema_migrations` is keyed on filename,
  so a renamed file runs again on every database that already applied it.

## Resource ceilings

Enforced via Docker Compose `mem_limit`:

| Container | RAM | CPU notes |
|---|---|---|
| `pipeline_worker` | 1.0 GiB | bursts during DuckDB compute (~1 s/cycle) |
| `denver_spatial_db` | 2.5 GiB | `shared_buffers=2GB`, `max_connections=20` |
| `scheduler` | 1.0 GiB | supercronic + each job's transient Python process; sized for the 02:00 archive's DuckDB → Parquet burst |
| `valhalla` | 3.0 GiB | serving a Denver-sized graph needs ~1 GiB; the headroom is for the transient tile build |
| `photon` | 2.0 GiB | JVM heap capped at 1536m (`JAVA_OPTS`); Photon embeds OpenSearch, so the heap **is** the index budget — this is why the index is Colorado-scoped and not US-wide |
| `valhalla_map_fetch`, `photon_index_fetch` | 256 MiB each | one-shot sidecars; they exit before the services they feed start serving |
| Other stacks on ovh3 | — | the host runs other projects' containers and agents; **not** enforced by this repo |

Total of the long-running ceilings: ~9.5 GiB, on a 22 GiB host (ovh3) that
it shares with those other stacks. These are **limits, not steady-state
usage** — Valhalla's headroom is only touched during a tile rebuild and the
two fetch sidecars have exited by then — but check what else the host is
running before adding another always-on service here.
