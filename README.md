# veo-audit

Denver micromobility spatial analytics pipeline. Polls the Veo GBFS feed
every 10 minutes, geo-tags each device against five boundary layers
(Disadvantaged Areas v1/v2, Neighborhoods, Council Districts, Community
Networks), and stores cycle-by-cycle metadata + per-region counts in
Postgres. Cold storage of raw points goes to Cloudflare R2 as Parquet
every 48 hours.

The original purpose was tracking compliance with Denver RFP §3.0 (30%
of fleet in Equity Areas) — see `VEO_AUDIT.md` for that history. The
v3.2 architecture (this README) generalizes the pipeline so any future
frontend (scooter.fyi, weseeyouveo.com, keepdenverfair.com, …) can XHR-poll the public REST
API for live state.

## Architecture

```
Veo GBFS feed                                Browser
     ▲                                             │ https://data.scooter.fyi
     │ */10 min                                    ▼
┌──────────────────────────┐               ┌────────────────┐
│  scheduler               │               │   cloudflared  │   Cloudflare Tunnel
│  supercronic + crontab   │               │   128 MiB cap  │   (TLS at CF edge)
│  256 MiB cap             │               └────────────────┘
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
runs natively on the same 12 GiB VPS with a 7.5 GiB sandbox.

## Repo layout

```
.
├── config.json                 non-secret runtime config
├── .env.example                env template (secrets ONLY)
├── docker-compose.yml          four services, hard memory caps
├── Dockerfile                  python:3.11-slim + FastAPI + DuckDB + supercronic + tini
├── crontab                     supercronic schedule for the scheduler container
├── data/                       baked-in boundary files (7)
│   ├── v1.json                 Disadvantaged Areas v1 — 34 polygons (legacy, being retired)
│   ├── v2.json                 Disadvantaged Areas v2 — 65 census block groups
│   ├── v3.json                 Equity Index rank ≤ 2 — 92 census block groups (provisional)
│   ├── v4.json                 Equity Index rank ≤ 3 — 249 census block groups (provisional)
│   ├── NB.geojson              78 neighborhoods
│   ├── CD.geojson              council districts (11 numbered + 2 at-large)
│   └── CN.geojson              13 community networks
├── sql/001_init.sql            schema, applied idempotently at boot
├── src/
│   ├── main.py                 FastAPI app, lifespan, migrations
│   ├── cli.py                  subcommands run by the scheduler container
│   ├── config.py               loads config.json + env
│   ├── pg.py                   psycopg pool + migration runner
│   ├── duck.py                 ephemeral DuckDB session factory
│   ├── ingest.py               GBFS fetch + freshness + envelope tagging
│   ├── compute.py              DuckDB CTEs → core + narrow rows
│   ├── cycle.py                observation_cycles lifecycle state machine
│   ├── transmit.py             fanout to downstream endpoints
│   ├── archive.py              48-hour Parquet → R2 → TRUNCATE
│   ├── api_public.py           4 read-only public REST routes
│   ├── api_admin.py            GitHub-OAuth-protected admin views
│   ├── auth.py                 GitHub OAuth + org allowlist
│   ├── sentry.py               Sentry SDK init (no-op without DSN)
│   └── templates/              Jinja templates for /admin
├── tests/                      pytest — 10 tests, ~60 s
└── .github/workflows/deploy.yml  build → GHCR → SSH-deploy on push to main
```

## Data model

Seven tables in Postgres, all narrow (no 270-column wide schemas):

| Table | Purpose |
|---|---|
| `observation_cycles` | Per-cycle UUID lifecycle: start_ts, phase timestamps, job_status, errors, JSONB blob |
| `api_failures` | Upstream / archive failures with cycle_id FK |
| `raw_telemetry_points` | Per-device rolling buffer; flushed to R2 every 48h |
| `snapshot_metadata_core` | The 22 RFP-relevant metrics, one row per cycle |
| `regional_metrics_narrow` | Per-region counts, one row per (cycle, region). Indexed by region_category + region_type + snapshot_time |
| `transmission_attempts` | One row per downstream POST, with http_status_code |
| `system_state` | Tiny KV (e.g. `last_archive_ts`) |

### Boundary taxonomy

| `region_category` | `region_type` | rows |
|---|---|---|
| `disadvantaged_areas` | `v1` | 34 polygons (legacy hand-drawn boundary; RFP compliance metric today, being retired — see API_REQUIREMENTS.md §1.1a) |
| `disadvantaged_areas` | `v2` | 65 census block groups |
| `disadvantaged_areas` | `v3` | 92 census block groups (DOTI Equity Index, rank ≤ 2 — provisional) |
| `disadvantaged_areas` | `v4` | 249 census block groups (DOTI Equity Index, rank ≤ 3 — provisional) |
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

**Secrets** come from environment variables only (see `.env.example`):
`POSTGRES_*`, `R2_*`, `SENTRY_DSN`, `OIDC_CLIENT_ID/SECRET`,
`AUTH_ALLOWED_GITHUB_ORGS`, `SESSION_SECRET`.

## Public API

All read-only, CORS-locked to `scooter.fyi` / `weseeyouveo.com` / `keepdenverfair.com`:

| Endpoint | Returns |
|---|---|
| `GET /health` | `{last_data_ingest_ts, last_data_upload_ts, last_cycle_id, last_retrieval_ts}` |
| `GET /api/v1/snapshots/latest` | Latest row of `snapshot_metadata_core` |
| `GET /api/v1/spatial-snapshot?layer=…&time=…` | `{region_name: {total, bikes, scooters}}` for a layer |
| `GET /api/v1/analytics/trend?layer=…&name=…&range=7d` | Time-series for a region |

## Admin panel

At `https://data.scooter.fyi/admin`, behind GitHub OAuth. Reached via
Cloudflare Tunnel (`cloudflared` sidecar) — the VPS does not expose port
80 or 443 to the internet. Users must be members of an org in
`AUTH_ALLOWED_GITHUB_ORGS`. Read-only views:

- `/admin/cycles` — paginated cycle log with status colors
- `/admin/cycles/{cycle_id}` — every phase timestamp, JSONB blob,
  transmission attempts, related failures
- `/admin/failures` — recent `api_failures` rows
- `/admin/regions?layer=…` — current snapshot's per-region counts

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
# 10 tests, ~60s. test_compute_sql exercises the real DuckDB spatial join.
```

## Deploy

Push to `main`. `.github/workflows/deploy.yml`:

1. Builds the image, pushes to `ghcr.io/z280/veo-audit:latest`
2. SCPs `docker-compose.yml`, `config.json`, `sql/` to `/opt/veo-audit/`
3. SSHes in, writes `.env` from GitHub Secrets, pulls and rolls containers
4. `curl /health` — fails the workflow if not green

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
  then every 10 min. Watch `docker compose logs -f pipeline_worker`.
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
| `scheduler` | 256 MiB | supercronic + each job's transient Python process |
| `cloudflared` | 128 MiB | tiny — outbound HTTPS tunnel daemon |
| Native Hermes (host) | 7.5 GiB | enforced via cgroups, **not** by this repo |

Total Docker footprint: ~3.9 GiB on the 12 GiB VPS. The remaining ~0.6 GiB
headroom absorbs transactional surges and host OS buffers.
