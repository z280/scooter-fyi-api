# veo-audit — VPS migration runbook (data.scooter.fyi)

Moves the veo-audit stack (Postgres + worker + scheduler + cloudflared) from
the old OVH box to the new VPS with **no DNS change** and a write-pause of only
a few minutes. Secrets ride GitHub Actions; only Postgres data and the live
scheduler crontab are hand-carried.

> Fill in first: `OLD=ubuntu@ai.neill.io`, `NEW=ubuntu@<new-ip>`.
> This runbook is part of the cross-service cutover — see
> [`agentz/MIGRATION.md`](https://github.com/zNeill/agentz/blob/main/MIGRATION.md)
> for the order relative to TheSystem and hermes (veo-audit goes **first**
> because it owns the shared cloudflared tunnel + docker network the
> coordination service attaches to).

## What moves how

| Piece | Source of truth | Migration mechanism |
|---|---|---|
| Container image | GHCR (`ghcr.io/z280/scooter-fyi-api:latest`) | pulled on deploy — nothing to move |
| `.env` secrets (salt, tunnel token, R2, Postmark, Stripe, …) | **GitHub Actions repo secrets** | repoint `VPS_HOST` secret → new box, re-run workflow |
| `docker-compose.yml`, `config.json`, `sql/` | this repo | `scp`'d to `/opt/veo-audit/` by deploy.yml |
| **Postgres data** (`pgdata` volume) | old box only | `scripts/migrate-state.sh` (pg_dump → pg_restore) |
| **Live scheduler crontab** (`scheduler_state` volume) | old box only | `scripts/migrate-state.sh` (volume tar) |
| Public routing (data.scooter.fyi) | Cloudflare tunnel | reuse same token; swap which box runs cloudflared |

### The two non-negotiables

- **`VEHICLE_IDENTIFIER_SALT` must be byte-identical** on the new box. It's an
  existing GitHub secret, so repointing `VPS_HOST` carries it automatically —
  do **not** regenerate it, or every previously-emitted `vehicle_identifier`
  orphans.
- **Reuse the same `CLOUDFLARE_TUNNEL_TOKEN`** (also an existing secret). The
  new cloudflared registers as a second connector on the *same* tunnel; the
  dashboard-managed ingress (`data.scooter.fyi → pipeline_worker:8080`,
  `thesystem.neill.io → coordination:8087`) is unchanged because both resolve
  by container name inside the shared docker network.

## Steps

### 1. Provision the new box
Base OS prep (docker, the `/opt/veo-audit` dir, the CI deploy user) is covered
by `agentz/scripts/provision-new-box.sh`. Confirm:
```bash
ssh $NEW 'docker --version && test -d /opt/veo-audit && echo ok'
```

### 2. Point CI at the new box and deploy an EMPTY stack
In the GitHub repo settings → Secrets:
- set `VPS_HOST` → new box IP/host (and `VPS_USER` if it differs)
- ensure the `VPS_SSH_KEY` public half is in `~$VPS_USER/.ssh/authorized_keys` on the new box

Then run the **Build and deploy** workflow (`workflow_dispatch`). It builds,
`scp`s compose/config, renders `.env` from secrets, and `docker compose up -d`.
The stack comes up with an empty Postgres and connects cloudflared to the tunnel
(now two connectors — old still serving; that's fine).

Verify the new box is healthy *through its own loopback* (not the tunnel yet):
```bash
ssh $NEW 'curl -sf localhost:8080/healthz && echo OK'   # needs the dev port map, see note
```
> Prod compose does **not** map the worker's port to the host. To probe before
> cutover, either check container health
> (`docker inspect --format '{{.State.Health.Status}}' pipeline_worker`) or
> temporarily add a `127.0.0.1:8080:8080` mapping via a compose override.

### 3. Freeze writes on the old box (start of the ~few-min pause)
Stop ingest + API so Postgres stops changing, but leave Postgres running for the
dump:
```bash
ssh $OLD 'cd /opt/veo-audit && docker compose stop scheduler pipeline_worker'
```

### 4. Migrate the data
```bash
OLD=$OLD NEW=$NEW ./scripts/migrate-state.sh           # add DRY_RUN=1 to preview
```
This pg_dumps the old DB, restores it into the new one, and copies the live
crontab. Re-run-safe (restore is `--clean --if-exists`).

### 5. Reload the scheduler + verify on the new box
```bash
ssh $NEW 'cd /opt/veo-audit && docker compose restart scheduler'
ssh $NEW 'docker exec denver_spatial_db psql -U veo -d veo_audit -tAc \
   "SELECT max(cycle_ts) FROM ingest_cycles"'   # newest cycle should match old box
```

### 6. Cut the tunnel over (end of the pause)
Stop cloudflared on the **old** box so only the new connector serves traffic:
```bash
ssh $OLD 'cd /opt/veo-audit && docker compose stop cloudflared'
```
Within seconds Cloudflare routes `data.scooter.fyi` (and `thesystem.neill.io`)
solely to the new box. Confirm from the public edge:
```bash
curl -s https://data.scooter.fyi/healthz
```
The new scheduler resumes ingest on its next `*/2` tick.

### 7. Decommission the old stack (after a soak)
Once you've watched a few clean ingest cycles + one archive on the new box:
```bash
ssh $OLD 'cd /opt/veo-audit && docker compose down'      # keep volumes as a backstop
```
Keep the old `pgdata` volume around for a week as a rollback safety net.

## Rollback
If something's wrong before step 6, nothing has cut over — just restart the old
stack (`docker compose start scheduler pipeline_worker`) and investigate the new
box out of band. After step 6, roll back by restarting cloudflared on the old
box and stopping it on the new one; the old Postgres still holds all data up to
the freeze.

## Post-rename operator checklist

One-time, for the `veo-audit` → `scooter-fyi-api` GitHub repo rename. GitHub
redirects git remotes, web URLs, issues/PRs, and keeps repo secrets — but GHCR
packages and anything that writes to the repo via the API do **not** follow.
Work through this the day of the rename.

**1. The box's `.env` still points at the old image until a deploy succeeds.**

`/opt/veo-audit/.env` contains `WORKER_IMAGE=ghcr.io/z280/veo-audit:latest`,
and the *only* thing that rewrites it is a successful `deploy.yml` run. GHCR
serves the old package forever after a rename, so until that deploy lands, a
manual `docker compose up -d` on the box pulls and runs **pre-rename code**,
silently and successfully. This — not the `${WORKER_IMAGE:-…}` fallback in
`docker-compose.yml` — is the real stale-image pointer. Confirm it flipped:
```bash
ssh $NEW 'grep WORKER_IMAGE /opt/veo-audit/.env'   # want .../scooter-fyi-api:latest
ssh $NEW 'docker inspect pipeline_worker --format "{{.Config.Image}}"'
```
Don't hand-run `docker compose up -d` between the rename and the first green
deploy.

**2. Check the new package's visibility and repo linkage — it's a decision.**

The first post-rename build creates a *brand-new* GHCR package,
`ghcr.io/z280/scooter-fyi-api`. The old one is **private**, but the repo is
**public**, and a package created via `GITHUB_TOKEN` inherits the linked
repo's visibility — so the new package may well come out **public**. Decide
which you want, don't discover it later:

Packages → `scooter-fyi-api` → *Package settings* → confirm **visibility**
and that **"Repository source"** is linked to `z280/scooter-fyi-api` (the
linkage is what lets the deploy job's `packages: read` pull it).

The image bakes in `config.json`, `sql/`, `data/`, and `src/` — but **not**
`.env` (verified). So a public package leaks source and config, not secrets.

**3. Delete or tombstone the old `ghcr.io/z280/veo-audit` package.**

Once the new package is confirmed working and the box has rolled onto it,
remove the old one (Packages → `veo-audit` → *Danger zone* → delete). While
it exists, any stale `WORKER_IMAGE` or unset-variable fallback keeps pulling
old code and *succeeding* — the failure is silent. Deleting it converts that
into a loud pull error.

**4. Hand-fix `finish-ovh3-cicd.sh` (outside this repo).**

`/home/ubuntu/finish-ovh3-cicd.sh:63` hardcodes `z280/veo-audit` when setting
repo secrets with `gh`. GitHub's REST API answers **301** for a renamed repo
and `gh` does not reliably follow redirects on **writes**, so secret-setting
will fail or silently no-op. Update that path by hand.

**What deliberately keeps the old name** — do not "fix" these:

| Name | Why |
|---|---|
| `name: veo-audit` in `docker-compose.yml` | Compose project name; prefixes every live volume (`veo-audit_pgdata`, …). Changing it points the stack at empty volumes and a blank Postgres. |
| `/opt/veo-audit` on the box | Cosmetic once the project name is pinned; renaming buys nothing and risks the deploy paths. |
| `veo_audit` database, `veo-audit-archive` R2 bucket, `veo-audit` Cloudflare tunnel | Live resource identifiers. |
| "Veo Audit" in public dataset/report copy | Deliberate public brand, distinct from the repo name. |

## Notes
- The `ADMIN_EMAILS` env is deprecated (admins live in Postgres now) — it rides
  along harmlessly; the real admin allowlist migrates inside the pg_dump.
- If you tuned the ingest cadence via `/admin/scheduler/edit`, that edit is on
  the `scheduler_state` volume and is carried by step 4 — the repo `crontab`
  only seeds a fresh volume, so don't rely on it to reflect prod cadence.
