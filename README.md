# scooter-fyi-api

Denver micromobility spatial analytics pipeline. Polls the Veo GBFS feed
every 2 minutes, geo-tags each device against five boundary layers
(Disadvantaged Areas v1/v2, Neighborhoods, Council Districts, Community
Networks), and stores cycle-by-cycle metadata + per-region counts in
Postgres. Cold storage of raw points goes to Cloudflare R2 as Parquet
every 48 hours.

> Formerly the `veo-audit` repo. **"Veo Audit" remains the public
> dataset/report brand** — only the repo/image names moved. Names that
> identify live resources deliberately did *not*: the Compose project
> (`veo-audit`, the prefix on every volume), the `/opt/veo-audit` deploy
> dir, the `veo-audit` Cloudflare tunnel, and the `veo_audit` database.
> See [MIGRATION.md](MIGRATION.md#post-rename-operator-checklist).

The original purpose was tracking compliance with Denver RFP §3.0 (30%
of fleet in Equity Areas) — see `VEO_AUDIT.md` for that history. The
v3.3 architecture (this README) generalizes the pipeline so any future
frontend (scooter.fyi, weseeyouveo.com, keepdenverfair.com, …) can XHR-poll the public REST
API for live state. For full request/response shapes, error codes, and
auth details behind the endpoint tables below, see [API.md](API.md).

## Architecture

```
Veo GBFS feed                                Browser
     ▲                                             │ https://data.scooter.fyi
     │ */2 min                                     ▼
┌──────────────────────────┐               ┌────────────────┐
│  scheduler               │               │   cloudflared  │   Cloudflare Tunnel
│  supercronic + crontab   │               │   128 MiB cap  │   (TLS at CF edge)
│  1.0 GiB cap             │               └────────────────┘
│  TZ=America/Denver       │                       │ outbound 443
└──────────────────────────┘                       ▼
     │ shells `python -m src.cli ...`        Cloudflare edge
     ▼                                             ▲
┌──────────────────────────┐                       │
│  pipeline_worker         │ ◄─────────────────────┘
│  1.0 GiB RAM cap         │   :8080 (internal only)
│                          │   FastAPI public API + admin panel
└──────────────────────────┘
     │ writes
     ▼
┌──────────────────────────┐
│  denver_spatial_db       │   vanilla Postgres 15 (no PostGIS)
│  2.5 GiB RAM cap         │     - source of truth for all persistent state
│  shared_buffers=2GB      │     - read by public API + admin panel
└──────────────────────────┘
     │ every 48 hr (gated by last_archive_ts)
     ▼
Cloudflare R2 (Parquet, ZSTD)   raw_telemetry_points archive
```

The `scheduler` and `pipeline_worker` containers share the same image —
scheduling is just a different entrypoint (`supercronic /app/crontab`)
that shells out to `python -m src.cli <command>`. The split means the
HTTP API can crash and restart without disturbing the schedule, and the
scheduler can crash without taking the API down.

### Postgres vs DuckDB — which does what

| | **Postgres** | **DuckDB** |
|---|---|---|
| Lives | always-on container | ~1 sec per cycle, in-process |
| Holds | every persistent table | nothing, between cycles |
| Used for | storage, admin queries, public API | spatial join (`ST_Within` against GeoJSON boundaries) |

Postgres is the system of record. DuckDB is a worker tool that loads
GeoJSON boundaries with its spatial extension, joins against the
just-tagged points, dumps aggregates into Postgres, and closes. This
keeps steady-state RAM near zero, which matters because a Hermes agent
runs natively on the same 12 GiB VPS (~256-512 MiB for an API-based
agent process).

## Repo layout

```
.
├── config.json                 non-secret runtime config
├── .env.example                env template (secrets ONLY)
├── docker-compose.yml          seven services, hard memory caps (Postgres, worker,
│                               scheduler, the Valhalla routing pair, the Photon
│                               geocoding pair)
├── Dockerfile                  python:3.11-slim + FastAPI + DuckDB + supercronic + tini
├── crontab                     supercronic schedule for the scheduler container
├── data/                       baked-in boundary files (11)
│   ├── v1.json                 Disadvantaged Areas v1 — 34 polygons (legacy, being retired)
│   ├── v2.json                 Disadvantaged Areas v2 — 65 census block groups
│   ├── er1.json … er6.json     DOTI Equity Index, one file per exact rank tier
│   │                           (34 / 58 / 157 / 93 / 114 / 116 block groups)
│   ├── NB.geojson              78 neighborhoods
│   ├── CD.geojson              council districts (11 numbered + 2 at-large)
│   └── CN.geojson              13 community networks
├── sql/001_init.sql … 060_username_presentation.sql
│                              60 migrations, applied idempotently at boot
├── docker/photon/Dockerfile    the Photon geocoding sidecar (pinned + sha256-verified
│                               official jar; the index itself ships from R2)
├── scripts/build_photon_index.md  manual runbook for building/refreshing that index
├── src/
│   ├── main.py                 FastAPI app, lifespan, migrations, router mounts
│   ├── cli.py                  subcommands run by the scheduler container
│   ├── config.py               loads config.json (non-secret) + env (secrets)
│   ├── pg.py                   psycopg pool + migration runner
│   ├── job_runs.py             job-run ledger — every scheduled command's
│   │                           last run, status and summary (/admin/scheduler)
│   ├── duck.py                 ephemeral DuckDB session factory
│   ├── ingest.py               GBFS fetch + freshness + envelope tagging
│   ├── identity.py              HMAC plate → vehicle_identifier (the privacy boundary),
│   │                            + plate_display_code (cosmetic-only, NOT a privacy control)
│   ├── compute.py               DuckDB CTEs → core + narrow rows
│   ├── device_state.py          per-vehicle NEW/MOVED/FAILED_START/STATIONARY state machine
│   ├── ride_watch.py            rider-declared ride watch: detect a device leaving/
│   │                            rejoining the feed (tracked_rides); called from cycle.py
│   │                            right after device_state, same isolation contract
│   ├── cycle.py                 observation_cycles lifecycle state machine
│   ├── transmit.py              fanout to downstream endpoints
│   ├── archive.py               48-hour Parquet → R2 → TRUNCATE
│   ├── boundaries.py            cached GeoJSON boundary loader
│   ├── geo.py                   pure-Python point-in-polygon (report-time region lookup;
│   │                            the per-cycle device join stays in DuckDB — see compute.py)
│   │                            + distance_meters (shared flat-earth distance helper)
│   │                            + path_length_meters (tracked-ride distance from waypoints)
│   ├── equity_groups.py         registry of tracked equity groups (v1, v2, er1..er6) and
│   │                            split dimensions (bicycle/scooter, sitting/standing) —
│   │                            single source of truth for compute.py + daily_sla.py
│   ├── quality.py               reliability tier + battery-percent conversion
│   ├── ranking.py               range/popularity ranking helpers
│   ├── dwell_stats.py           dwell-time outlier detection
│   ├── daily_sla.py             9am daily SLA compliance rollup
│   ├── daily_trips.py           9am daily trip/popularity rollup (trip_events → ranked stats)
│   ├── equity_backfill.py       9:40am reprocessing of PRIOR days' Equity Area compliance
│   │                            against the city's clarified map — rebuilds each past
│   │                            cycle's fleet from device_history's stop intervals, gated
│   │                            on agreeing with the fleet count that cycle recorded
│   ├── accounts.py              account/session core: bearer tokens, require_session/
│   │                            require_admin, public-username generation/
│   │                            choice, phone number validation
│   ├── google_auth.py           local Google ID-token (JWKS) verification, no per-request call
│   ├── postmark.py              transactional email (magic link, sign-in code)
│   ├── ratelimit.py             Postgres-advisory-lock rate limiting (no Redis)
│   ├── image_processing.py      shared Pillow re-encode pipeline (EXIF strip, resize, JPEG) —
│   │                            used by receipts, device photos, ride screenshots
│   ├── receipts.py              discount-report receipt upload → R2 (thin wrapper over
│   │                            image_processing.py)
│   ├── device_photos.py         device photo upload → PUBLIC R2 bucket
│   ├── ride_screenshots.py      ride transaction screenshot upload → PRIVATE R2 bucket
│   ├── qr.py                    QR payload plate extraction + vehicle_identifier validation
│   ├── points.py                points ledger primitives (credit_points + per-action wrappers,
│   │                            incl. the reshaped ride-mode awards battery_contribution/
│   │                            nav_distance_bonus; credit_points is where the
│   │                            100-points-per-ride cap and the even-points assert live)
│   ├── track_verify.py          pure server-side verifier for the donated waypoint chain:
│   │                            signature → chain integrity → monotonic/bounds → speed →
│   │                            GBFS start/end correlation → volume minimums
│   ├── battery_model.py         empirical battery-burn regression: nightly observation-gap
│   │                            mining + weekly refit, plus ingest_donated_observation
│   │                            (the donated-track battery feedback loop, sql/051)
│   ├── ride_limits.py           the operator's three hard ride invariants — 100 points/ride,
│   │                            3 km between consecutive path points, 80 km/ride — plus the
│   │                            shared path measurement both ride modules close out with
│   ├── polyline.py              Google polyline encode/decode (ride paths)
│   ├── valhalla.py              Valhalla HTTP client + trip shape/summary/maneuver
│   │                            extraction (per-leg shape indices re-offset in one pass)
│   ├── r2_map.py                SigV4 sync of the private R2 sidecar artifacts: the
│   │                            routing .pbf + canopy sidecar, and the Photon index
│   ├── badges.py                server-computed profile badges (recomputed on every read;
│   │                            mileage/streak badges union tracked_rides.distance_meters
│   │                            with off-feed rides.distance_m — ended rides only)
│   ├── client_ip.py             client IP extraction behind the Cloudflare Tunnel
│   ├── auth.py                  GitHub OAuth + org allowlist (admin panel only — a
│   │                            separate mechanism from accounts.py's rider auth)
│   ├── sentry.py                Sentry SDK init (no-op without DSN)
│   ├── api_public.py            11 public read-only REST routes
│   ├── api_h3.py                public H3 aggregate endpoint
│   ├── api_meta.py              public metadata endpoints — privacy retention policy
│   │                            and the Ride Mode sales-tax rate (`/meta/pricing`)
│   ├── api_user.py              signed-in device map feed
│   ├── api_profile.py           rider profile GET/PUT + public-username endpoints
│   ├── api_lexicon.py           emoji-noun / adjective list + search endpoints
│   ├── api_preferences.py       rider-owned opaque preference blobs: saved map settings,
│   │                            the find-ride preference, ride-mode "Usuals"
│   ├── api_route.py             GET /api/v1/route (+ /profiles) — Valhalla proxy, shade
│   │                            re-ranking, battery estimate, turn-by-turn maneuvers
│   ├── api_geocode.py           GET /api/v1/geocode/search — proxy over the
│   │                            self-hosted Photon sidecar (Denver bbox filter,
│   │                            in_coverage vs the routing graph, 24h LRU cache)
│   ├── api_auth.py              sign-in doors + session lifecycle
│   ├── api_tracked_rides.py     GBFS-detected ride tracking: start/list/active/detail/
│   │                            end-report/track-donation/waypoints(deprecated)/delete
│   ├── api_rides.py             OFF-FEED rides — vehicles not in the GBFS feed (a personal
│   │                            scooter, a competitor's rental). Same lifecycle as tracked
│   │                            rides (start/waypoints/end) plus a one-shot log of a
│   │                            finished ride, owner-only list/export, hard delete. No
│   │                            points; client-asserted distances are plausibility-checked
│   ├── api_points.py            GET /api/v1/points — ledger + running total; GET
│   │                            /api/v1/points/schedule — public action → award map,
│   │                            generated from src/points.py so UI copy cannot drift
│   ├── api_device_recommendations.py  POST .../recommend
│   ├── api_device_photos.py     device photo upload/list/report + GET /api/v1/photos/mine
│   ├── api_qr.py                POST /api/v1/devices/qr-scan
│   ├── api_ride_screenshots.py  ride transaction screenshot upload/list
│   ├── api_ride_surveys.py      POST /api/v1/tracked-rides/{id}/survey — Screen 9's
│   │                            end-of-ride survey + its three point awards (sql/052)
│   ├── api_ride_routes.py       POST /api/v1/ride-routes — Screen 4's chosen route,
│   │                            persisted only when nav_improvement consent is on (sql/052)
│   ├── api_reports.py           map-pin negative reports + quality feedback
│   ├── api_frontend_reports.py  account-aware device/discount report flow
│   ├── api_private.py           bearer-token admin JSON API (distinct from api_admin.py)
│   ├── api_admin.py             GitHub-OAuth-protected admin HTML views
│   ├── api_legal.py             static legal pages
│   └── templates/               Jinja templates for /admin
├── tests/                      pytest, ~60 files, roughly one per src/ module
└── .github/workflows/deploy.yml  build → GHCR → SSH-deploy on push to main
```

## Data model

Core tables in Postgres, all narrow (no 270-column wide schemas). (This
list predates the accounts/reports tables from
API_REQUIREMENTS.md §2-§4 — see those migrations for the full current
set; kept here is the original ingest-pipeline core plus trip tracking.)

| Table | Purpose |
|---|---|
| `observation_cycles` | Per-cycle UUID lifecycle: start_ts, phase timestamps, job_status, errors, JSONB blob |
| `api_failures` | Upstream / archive failures with cycle_id FK |
| `raw_telemetry_points` | Per-device rolling buffer; flushed to R2 every 48h |
| `snapshot_metadata_core` | The 22 RFP-relevant metrics, plus the same total/percent fields per tracked equity group **and** per split dimension (bicycle/scooter, sitting/standing — see `src/equity_groups.py`), one row per cycle |
| `regional_metrics_narrow` | Per-region counts, one row per (cycle, region). Indexed by region_category + region_type + snapshot_time |
| `transmission_attempts` | One row per downstream POST, with http_status_code |
| `system_state` | Tiny KV (e.g. `last_archive_ts`) |
| `trip_events` | One row per detected "successful trip" (a MOVED transition in `device_state.py`) — vehicle, from/to position, distance |
| `daily_trip_summary` | One row per Denver-local calendar day: total trips, distinct vehicles tripped |
| `daily_vehicle_trip_counts` | One row per (day, vehicle) with a trip that day: trip count + popularity rank |

### Boundary taxonomy

| `region_category` | `region_type` | rows |
|---|---|---|
| `disadvantaged_areas` | `v1` | 34 polygons (legacy hand-drawn boundary; RFP compliance metric today, being retired — see API_REQUIREMENTS.md §1.1a) |
| `disadvantaged_areas` | `v2` | 65 census block groups |
| `disadvantaged_areas` | `er1`..`er6` | 34 / 58 / 157 / 93 / 114 / 116 census block groups — DOTI Equity Index, one layer per exact rank tier (er1 = highest need). Partition the scored area; tracked individually (not pre-combined) so a future compliance cutoff can be reconstructed from history. Full metric parity with v1/v2 in both `snapshot_metadata_core` and `daily_sla_compliance` — see `src/equity_groups.py` and API_REQUIREMENTS.md §1.1a. |
| `council_districts` | `council_district` | 11 (CD_1…CD_11; At-Large overlays filtered) |
| `community_networks` | `community_network` | 13 (CN_Central, CN_Southwest, …) |
| `neighborhoods` | `neighborhood` | 78 (NB_AthmarPark, …) |

### The 22 core metrics

Stored in `snapshot_metadata_core` and exposed verbatim via
`/api/v1/snapshots/latest`. Counts: `total_devices_(denver|v1|v2)`,
`total_(bike|scooter)_(denver|v1|v2)`, `total_not_in_denver`. Percentages:
all the natural ratios — bikes_denver, scooters_v1, all_devices_v2, etc.

## Configuration

**Non-secret** values live in `config.json` (committed):

- `gbfs.url`, `gbfs.vehicle_types_url`, `gbfs.timeout_seconds`
- `schedule.cycle_minutes` (10), `schedule.archive_hours` (48)
- `envelope.denver_core`, `envelope.china_glitch` bounding boxes
- `boundaries[]` — one entry per layer, with file path + naming rule
- `transmission.endpoints[]` — `{name, url, method, path, auth_env}`
- `cors.allowed_origins` — strictly enforced
- `auth.allowed_github_orgs` (default; env overrides)
- `valhalla.*` — sidecar URL, `graph_bbox`, and the selectable routing profiles
- `geocode.upstream` / `geocode.enabled` — the Photon sidecar behind
  `/api/v1/geocode/search`; `enabled: false` is a real kill switch (the
  endpoint 503s exactly as it does when the sidecar is down)
- `pricing.tax_rate` / `currency` / `as_of` — served by `/api/v1/meta/pricing`.
  `tax_rate` is a **fraction** (0.0915, not 9.15); a value outside `[0, 1)` is
  refused and the built-in default served instead

**Secrets** come from environment variables only (see `.env.example`):
`POSTGRES_*`, `R2_*`, `SENTRY_DSN`, `OIDC_CLIENT_ID/SECRET`,
`AUTH_ALLOWED_GITHUB_ORGS`, `SESSION_SECRET`.

## Public API

No authentication required. CORS-locked to `scooter.fyi` /
`weseeyouveo.com` / `keepdenverfair.com` for browser callers; anything
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
| `GET /api/v1/equity-estimate` | Device share inside selected equity-rank tiers from the latest snapshot |
| `GET /api/v1/h3/aggregates` | Per-H3-cell aggregates (device_count, trips_started_24h, battery, risk_share, dwell) at res 8/9/10 |

### Compliance

| Endpoint | Returns |
|---|---|
| `GET /api/v1/compliance/daily/latest` | Most recent computed daily SLA compliance window |
| `GET /api/v1/compliance/daily?date=…` | Daily SLA window for one Denver-local date |
| `GET /api/v1/compliance/daily/range` | Range of daily SLA rows, ascending |

### Routing & geocoding

Both upstreams are self-hosted sidecars in this repo's compose file — a
Denver-clipped Valhalla graph (`valhalla`) and a Colorado-scoped Photon
index (`photon`, built from `docker/photon/`, seeded from R2; see
`scripts/build_photon_index.md`). No third-party routing or geocoding API,
no API key, and no rider query leaves the box. Both are rate limited per IP
because a sidecar round trip is expensive.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/route?from=&to=&profile=&maneuvers=` | GeoJSON `Feature`: route geometry, distance/duration/elevation, battery estimate, and — with `maneuvers=true` — turn-by-turn cues whose shape indices are re-offset onto the returned LineString. Directions are **beta**: every response carries a `beta_warning` string clients must show to riders. 30/min per IP |
| `GET /api/v1/route/profiles` | The selectable routing profiles + `graph_bbox` (config-driven; treat as the live list). 60/min per IP |
| `GET /api/v1/geocode/search?q=…&lat=…&lon=…&limit=…` | Up to 8 Denver-scoped hits as `{label, lat, lon, kind, in_coverage}`; `in_coverage` is routing-graph membership so clients can grey out un-routable picks. 20/min per IP; 503 `geocoder_unavailable` when the sidecar is down or disabled |

### Leaderboard

FEATURE_PLAN §11: the trailing-28-day H3 r8 "area leader" report,
recomputed nightly (`src/area_leaders.py`, `sql/048_h3_r8_area_leaders.sql`)
and served with privacy applied **live** at read time -- an account's
`show_in_leaderboards`/`show_public_username` choice (or a never-
backfilled `display_name`) takes effect on the very next request, not at
tomorrow's 09:15 run. See `src/api_leaderboard.py`.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/leaderboard/map` | `{computed_at, window_start, window_end, cells: {"<h3 string>": {total_points, distinct_earners, leader, runners_up}}}` -- full eligible top-3 per cell in one fetch, so the choropleth and click-through detail come from one request. A skipped-for-privacy rank falls through (leader = highest surviving stored rank). Weak ETag keyed on `(computed_at, sha256(rendered cells))` -- not run-only, so an eligibility/color/name change between recomputes still busts a client's cache. `public, max-age=600` |

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
| `GET /api/v1/reports/summary` | Per-region report aggregate (~10 min cache) |
| `GET /api/v1/reports/export/monthly.csv` | Monthly CSV export for DOTI/journalists |

### Other

| Endpoint | Returns |
|---|---|
| `GET /` | JSON banner listing every mounted endpoint (discovery only) |
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
| `PUT /api/v1/profile` | Partial update of `rate_plan`/`theme`/`favorites`/`email`/`phone_number`/`show_public_username`/`show_in_leaderboards`/`home_lat`/`home_lng`/`work_lat`/`work_lng`/`royalty_title`/`ruling_color`/`ruling_border_color`/`ruling_alpha` |
| `POST /api/v1/profile/username/regenerate` | Re-roll your public username to a new random adjective+emoji pair |
| `PUT /api/v1/profile/username` | Choose a specific adjective and/or emoji (partial update) |
| `POST /api/v1/profile/phone/code` | Text a code to prove you answer your listed number |
| `POST /api/v1/profile/phone/verify` | Type it back → `phone_verified` on **this** account |
| `GET /api/v1/profile/map-settings` | Every saved map setting for the caller |
| `GET /api/v1/profile/map-settings/{name}` | One saved map setting |
| `PUT /api/v1/profile/map-settings/{name}` | Create or replace a named map setting (opaque JSON blob) |
| `DELETE /api/v1/profile/map-settings/{name}` | Delete a named map setting |
| `GET /api/v1/profile/find-ride-pref` | The caller's find-ride preference, or `null` if never set |
| `PUT /api/v1/profile/find-ride-pref` | Create or replace the find-ride preference (at most one per rider) |
| `DELETE /api/v1/profile/find-ride-pref` | Clear the find-ride preference (idempotent) |
| `GET /api/v1/profile/ride-usuals` | Every saved ride-mode "Usual" (options preset) for the caller |
| `GET /api/v1/profile/ride-usuals/{name}` | One saved Usual |
| `PUT /api/v1/profile/ride-usuals/{name}` | Create or replace a named Usual (opaque JSON blob: `ride_options` + `label`); 10 per account |
| `DELETE /api/v1/profile/ride-usuals/{name}` | Delete a named Usual |
| `GET /api/v1/profile/favorite-devices` | Vehicles the caller keeps, with live state; position withheld while in use |
| `POST /api/v1/profile/favorite-devices` | Keep a vehicle — needs a valid QR scan **and** a fix within 75 m of it; the scan is the identity, so `vehicle_identifier` is optional; 10 per account |
| `PATCH /api/v1/profile/favorite-devices/{vehicle_identifier}` | Rename, or turn the availability alert on/off |
| `DELETE /api/v1/profile/favorite-devices/{vehicle_identifier}` | Let one go |
| `GET /api/v1/emoji-nouns` | Full emoji → noun-word list, for building a username picker |
| `GET /api/v1/emoji-nouns/search?q=…` | Partial word match on the emoji-noun list |
| `GET /api/v1/adjectives` | Full curated adjective list |
| `GET /api/v1/adjectives/search?q=…` | Partial word match on the adjective list |
| `GET /api/v1/royalty-titles` | Curated titles that can prefix a public username |
| `GET /api/v1/royalty-titles/search?q=…` | Partial match on the title list |
| `GET /api/v1/ruling-colors` | The 128-colour leaderboard palette + already-claimed (fill, border) pairs |
| `GET /api/v1/user/devices/current` | Signed-in device map feed; adds plate/admin fields for admin-allowlisted sessions |
| `POST /api/v1/reports/discount` | Missed-discount evidence, optional receipt upload |

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
| `DELETE /api/v1/tracked-rides/{ride_id}` / (bare) | Hard-delete one ride / every ride you own |
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
| `POST /api/v1/devices/qr-scan` | Validate a scanned QR against the claimed device; awards a first-scan bonus |

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
| `GET /api/v1/private/area-leaders` | Full, unfiltered §11 leaderboard: every stored rank 1-3 per cell with real account ids/points/tie-break provenance -- no privacy filtering |
| `GET /api/v1/private/reports` | Admin listing of all negative reports |
| `GET /api/v1/private/quality-feedback` | Admin listing of all quality feedback |

## Admin panel

At `https://data.scooter.fyi/admin`, behind GitHub OAuth — deliberately a
separate door from rider auth, and **staying that way** (the proposal to
retire it in favour of the bearer/`admin_allowlist` path was withdrawn
2026-07-28). Reached via
Cloudflare Tunnel (`cloudflared` sidecar) — the VPS does not expose port
80 or 443 to the internet. Users must be members of an org in
`AUTH_ALLOWED_GITHUB_ORGS`. HTML views (GET unless noted):

- `/admin/login` — start GitHub OAuth login
- `/admin/auth/callback` — GitHub OAuth callback
- `/admin/logout` — clear the admin session
- `/admin` — redirects to `/admin/login` (signed out) or `/admin/cycles` (signed in)
- `/admin/cycles` — paginated cycle log with status colors
- `/admin/cycles/{cycle_id}` — every phase timestamp, JSONB blob,
  transmission attempts, related failures
- `/admin/failures` — recent `api_failures` rows
- `/admin/scheduler` — active crontab + recent cycle cadence view
- `/admin/scheduler/edit` — crontab textarea editor form
- `/admin/scheduler/edit` (POST) — validate (via `supercronic -test`) and save/reset the crontab
- `/admin/regions?layer=…` — current snapshot's per-region counts
- `/admin/admins` — admin allowlist management view
- `/admin/admins/add` (POST) — add an email to the admin allowlist
- `/admin/admins/remove` (POST) — remove an email from the admin allowlist

## Run locally

```bash
cp .env.example .env
# Minimum: set POSTGRES_PASSWORD; leave CLOUDFLARE_TUNNEL_TOKEN /
# R2 / Sentry / OIDC blank. Set SESSION_HTTPS_ONLY=false locally.
# Also uncomment the `ports: "8080:8080"` block in pipeline_worker
# so you can reach the worker without a tunnel.

docker compose up --build pipeline_worker denver_spatial_db
# (skip the cloudflared service — it'll fail without a real token)

curl localhost:8080/health   # 4-key JSON
curl localhost:8080/api/v1/snapshots/latest   # 503 until first cycle lands (~15s)
```

## Run tests

```bash
python3.11 -m pip install -r requirements.txt pytest
python3.11 -m pytest -v
# ~1300 tests across ~95 files. Most run with no real Postgres (a fake
# cursor/connection is monkeypatched in) — test_compute_sql exercises the
# real DuckDB spatial join, and files ending _pg.py additionally skip
# unless VEO_TEST_PG_DSN points at a real, migratable Postgres instance.
```

### Running the `_pg.py` tests

The ~140 `_pg.py` tests are the only coverage of the `sql/` files as Postgres
actually executes them — the guarded `DO $$` constraint blocks, the partial
unique indexes, and `test_migration_replay_pg.py`'s replay-over-live-data
check are all invisible to a fake cursor. **Skipped is not passed**: run them
before shipping a migration.

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

Push to `main`. `.github/workflows/deploy.yml`:

1. Builds the image, pushes to `ghcr.io/z280/scooter-fyi-api:latest`
2. SCPs `docker-compose.yml`, `config.json`, `sql/`, `docker/` to
   `/opt/veo-audit/` — `docker/` because the `photon` sidecar is built on the
   box from `docker/photon/`, not pulled from GHCR
3. SSHes in, renders `.env.new` from GitHub Secrets, `docker compose pull`s
   with it, and only then `mv`s it over `.env`, builds `photon` (a layer-cache
   no-op unless its Dockerfile moved) and rolls containers — a failed pull
   leaves the live `.env` (and the running stack) untouched
4. `curl /health` — fails the workflow if not green

Renaming the repo? See the
[post-rename operator checklist](MIGRATION.md#post-rename-operator-checklist).

Required GitHub Secrets:

| Secret | Notes |
|---|---|
| `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY` | dedicated passwordless ed25519 keypair |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Postgres credentials |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` | Cloudflare R2 token scoped to one bucket |
| `SENTRY_DSN` | optional; blank disables Sentry |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | GitHub OAuth App; callback `https://data.scooter.fyi/admin/auth/callback` |
| `AUTH_ALLOWED_GITHUB_ORGS` | comma-separated, e.g. `z280` |
| `SESSION_SECRET` | `openssl rand -hex 32` |
| `CLOUDFLARE_TUNNEL_TOKEN` | from Cloudflare Zero Trust → Networks → Tunnels (see below) |

The VPS only needs Docker + a `deploy` user with passwordless sudo and
the SSH public key in `~/.ssh/authorized_keys`. Everything else (image,
config, schema) is pushed by the workflow.

### Cloudflare Tunnel setup (one-time, before first deploy)

The `cloudflared` sidecar terminates the admin panel's TLS at Cloudflare's
edge, so the VPS doesn't expose any ports to the internet.

1. Cloudflare dashboard → **Zero Trust** → **Networks → Tunnels**
   → **Create a tunnel** → name `veo-audit` → **Save**.
2. On the "Install connector" page, copy the token from the
   `cloudflared service install <TOKEN>` command — that's the
   `CLOUDFLARE_TUNNEL_TOKEN` GitHub Secret. Click **Next**.
3. **Public Hostnames** → **Add a public hostname**:
   - **Subdomain:** `admin`
   - **Domain:** `scooter.fyi`
   - **Type:** `HTTP`
   - **URL:** `pipeline_worker:8080`
   - **Save hostname**.
4. (Optional) Add a second public hostname for the unauthenticated
   public API, e.g. `api.scooter.fyi` → `pipeline_worker:8080`.
5. GitHub OAuth App → **Settings** → set
   **Authorization callback URL** to
   `https://data.scooter.fyi/admin/auth/callback`.

Adding/changing routes after the first deploy is a dashboard operation —
no redeploy needed. Rotating the token does require updating the GitHub
Secret and redeploying.

## Operating tips

- **First cycle**: fires ~5 s after the worker comes up (boot job),
  then every 2 min. Watch `docker compose logs -f pipeline_worker`.
- **Stale upstream**: if Veo's `last_updated` hasn't changed since the
  previous cycle, the cycle aborts with `job_status='stale_aborted'`
  and a row in `api_failures`. This is normal during outages.
- **Failures**: Sentry gets every uncaught exception (tagged with
  `cycle_id`). `api_failures` is the authoritative audit log.
- **Archive**: the 48-hour job is idempotent — it only truncates after
  R2 returns HTTP 200. If R2 is unreachable, `raw_telemetry_points`
  just keeps growing until the next attempt.
- **Schema changes**: drop a new `sql/00N_*.sql` file. `src/pg.py`
  applies anything not in `schema_migrations` at boot. All migrations
  use `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` for
  belt-and-suspenders re-runnability.

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
| `cloudflared` | 128 MiB | tiny — outbound HTTPS tunnel daemon |
| Native Hermes (host) | ~512 MiB | API-based agent process; **not** enforced by this repo |

Total of the long-running ceilings: ~9.6 GiB on the 12 GiB VPS. These are
**limits, not steady-state usage** — Valhalla's headroom is only touched
during a tile rebuild and the two fetch sidecars have exited by then — but the
sum is now close enough to the box that another always-on service needs a hard
look at these numbers first, not after.
