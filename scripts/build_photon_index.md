# Building the Photon geocoding index

Manual runbook. Run it **once to seed** the geocoder and **quarterly** to refresh
it. Nothing in the compose stack builds this index: the `photon` sidecar only
*serves* it, and `python -m src.cli fetch_photon_index` only *downloads* it.

The artifact this produces is one object in the private routing bucket:

```
r2://$R2_MAP_BUCKET/photon/photon-index-<YYYYMMDD>.tar.zst
```

`src/r2_map.py:sync_photon_index()` picks the **newest date in the key name**
(not the newest `LastModified`), ETag-gates it against
`/photon/photon-index.etag`, unpacks `photon_data/` into the `photon_files`
volume, and logs loudly that the `photon` container must be restarted.

---

## Why Colorado, not the US or the planet

Photon 1.x embeds OpenSearch **in the same JVM** it serves from, so the index
size is a memory problem, not just a disk problem. The compose service is pinned
at `-Xmx1536m` / `mem_limit: 2048m` on a host that also runs Postgres (2.5 GiB),
Valhalla (3 GiB) and the worker — a full-US extract does not fit that budget and
a planet index is 95 GB of disk and wants ~64 GB of RAM (photon's own README).

A Colorado extract is the smallest region that still covers every address a
Denver rider can plausibly type, including destinations just outside the routing
graph — which is the point: `/api/v1/geocode/search` filters on
`envelope.denver_core` (wider than the graph) and flags `in_coverage` per result
so the client can grey out un-routable picks instead of hiding them. A
Denver-only extract would make that flag useless and would drop the
just-over-the-line addresses riders actually search for.

**A full-US extract is explicitly rejected**, per the plan. If someone later
needs a wider index, it stops being a sidecar and needs its own external
OpenSearch (photon's `-transport-addresses`) plus a memory budget of its own.

## Why quarterly

New addresses in OSM arrive continuously but slowly; a quarter-stale index
misses recently-built infill and nothing else. The failure mode is a rider
typing an address that returns no hit — recoverable (they drop a pin or type it
again), and not worth standing up Nominatim replication (photon's
`update-init`/`update`) on the production host to avoid. Re-run this runbook on
1 Jan / 1 Apr / 1 Jul / 1 Oct, or sooner if a rider reports a missing address.

## Version lock ⚠️

The index format is tied to the photon major version. Build the index with the
**same jar version** `docker/photon/Dockerfile` pins (`ARG PHOTON_VERSION`), and
when bumping that version, rebuild and re-upload the index in the same change.
A mismatched index typically fails at JVM startup — the container never becomes
healthy and `/api/v1/geocode/search` 503s, which is at least loud.

---

## Prerequisites

* A **throwaway** machine (a laptop or a scratch VM), not the production host:
  the Nominatim import is I/O-hungry and would starve the live stack.
* Docker, ~40 GB free disk, ≥8 GB RAM.
* `zstd` and `tar` on the host.
* R2 credentials for `$R2_MAP_BUCKET` (the same scoped token the Valhalla assets
  use — `R2_MAP_ACCESS_KEY_ID` / `R2_MAP_SECRET_ACCESS_KEY`).
* Wall-clock: ~1–2 h, mostly the Nominatim import.

```sh
export WORKDIR=~/photon-build && mkdir -p "$WORKDIR" && cd "$WORKDIR"
export PHOTON_VERSION=1.2.1        # MUST match docker/photon/Dockerfile
export STAMP=$(date -u +%Y%m%d)
export NOMINATIM_PASSWORD=throwaway-not-a-secret
```

## 1. Get the extract

```sh
curl -fL -O https://download.geofabrik.de/north-america/us/colorado-latest.osm.pbf
# ~250 MB. Geofabrik also publishes .md5 — check it:
curl -fL -O https://download.geofabrik.de/north-america/us/colorado-latest.osm.pbf.md5
md5sum -c colorado-latest.osm.pbf.md5
```

## 2. Import it into a throwaway Nominatim

Photon does not read `.osm.pbf`. It builds its index from a **Nominatim
database**, which is why this step exists at all. The container is disposable —
it is deleted in step 6 and never goes near production.

```sh
docker run -d --name nominatim-throwaway \
  -e PBF_PATH=/nominatim/data/colorado-latest.osm.pbf \
  -e NOMINATIM_PASSWORD="$NOMINATIM_PASSWORD" \
  -e IMPORT_STYLE=full \
  -e THREADS=4 \
  -v "$WORKDIR/colorado-latest.osm.pbf":/nominatim/data/colorado-latest.osm.pbf:ro \
  -p 8080:8080 \
  -p 5433:5432 \
  mediagis/nominatim:5.3

docker logs -f nominatim-throwaway     # wait for the import to finish
```

Notes:

* `IMPORT_STYLE=full` — photon wants POI names, not just address points. A
  reduced style (`address`) yields an index that cannot answer "REI" or "Union
  Station", only house numbers.
* `-p 5433:5432` publishes the container's PostgreSQL so the photon jar (running
  on the host in step 3) can reach it. Drop this if you instead run photon
  inside the same docker network.
* mediagis renames its env vars occasionally between major tags — if the
  container exits immediately, check that image tag's README before debugging
  anything else.
* Sanity check when it finishes:
  `curl 'http://localhost:8080/search?q=1701+Champa+St,+Denver&format=json'`

## 3. Build the photon index from that database

```sh
curl -fL -O "https://github.com/komoot/photon/releases/download/${PHOTON_VERSION}/photon-${PHOTON_VERSION}.jar"
# Verify against the checksum pinned in docker/photon/Dockerfile (ARG PHOTON_SHA256):
sha256sum "photon-${PHOTON_VERSION}.jar"

java -jar "photon-${PHOTON_VERSION}.jar" import \
  -host 127.0.0.1 -port 5433 \
  -database nominatim -user nominatim -password "$NOMINATIM_PASSWORD" \
  -languages en \
  -country-codes us \
  -data-dir "$WORKDIR"
```

This writes `$WORKDIR/photon_data/`. `-data-dir` is the **parent** of
`photon_data`, which is exactly how the sidecar mounts it (`/photon` →
`/photon/photon_data`, `src/r2_map.PHOTON_DATA_DIRNAME`).

* `-languages en` keeps the index to one name variant — photon defaults to
  en,de,fr,it, which inflates it for no benefit here.
* In photon **< 1.0** this subcommand was the flag `-nominatim-import`; the
  pinned 1.x jar uses the `import` subcommand shown above.

## 4. Verify before uploading

Never upload an index that has not answered a query. A subtly empty index
produces a perfectly healthy container and zero results forever.

```sh
cd "$WORKDIR"
java -Xmx1536m -jar "photon-${PHOTON_VERSION}.jar" serve \
  -data-dir "$WORKDIR" -listen-ip 127.0.0.1 -listen-port 2322 &

curl -s http://127.0.0.1:2322/status                       # {"status":"Ok", ...}
# The same bbox the API sends (minLon,minLat,maxLon,maxLat from
# config.json envelope.denver_core) — a lat/lon-ordered bbox returns nothing,
# which is the single easiest way to be fooled here:
curl -s 'http://127.0.0.1:2322/api?q=1701+Champa&limit=6&lang=en&bbox=-105.2,39.6,-104.6,39.9' | head -c 800
curl -s 'http://127.0.0.1:2322/api?q=Union+Station&lat=39.75&lon=-105.00&limit=6&lang=en' | head -c 800
kill %1
```

Expect a Denver house hit for the first (`osm_key: place`/`building`,
`housenumber: 1701`) and a POI for the second. Confirm `-Xmx1536m` was enough —
that is the production heap.

## 5. Pack and upload

```sh
cd "$WORKDIR"
tar -c photon_data | zstd -T0 -19 -o "photon-index-${STAMP}.tar.zst"
ls -lh "photon-index-${STAMP}.tar.zst"
```

The tarball **must** have `photon_data/` as its single top-level directory —
`sync_photon_index()` refuses anything else rather than guessing (it would
otherwise silently install an index photon cannot find). `tar -c photon_data`
from the parent directory is what produces that.

```sh
export AWS_ACCESS_KEY_ID=$R2_MAP_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY=$R2_MAP_SECRET_ACCESS_KEY
aws s3 cp "photon-index-${STAMP}.tar.zst" \
  "s3://${R2_MAP_BUCKET}/photon/photon-index-${STAMP}.tar.zst" \
  --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
```

Keep the previous index in the bucket (the date-suffixed key means it is not
overwritten): it is the rollback. Delete anything older than the last two by
hand — nothing prunes this prefix.

## 6. Tear down and deploy

```sh
docker rm -f nominatim-throwaway
docker volume prune -f          # the throwaway Nominatim volumes
```

On the production host:

```sh
docker compose run --rm photon_index_fetch     # or wait for the 05:00 cron
docker compose restart photon                  # REQUIRED: index read at startup
docker compose ps photon                       # wait for (healthy)
curl -s 'http://127.0.0.1:8080/api/v1/geocode/search?q=1701+Champa&limit=6'
```

`fetch_photon_index` is a no-op when the ETag is unchanged, so the 05:00
`refresh_photon_index` cron picks a new index up on its own — but it cannot
restart the `photon` container (the scheduler deliberately has no Docker
socket, exactly like `refresh_routing_graph` and Valhalla's tile rebuild). It
logs `PHOTON INDEX UPDATED … must be RESTARTED`; the restart is the operator's.
