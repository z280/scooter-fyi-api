# scooter-fyi-api — Public API

REST API serving Denver Veo micromobility fleet data. Polls the upstream
GBFS feed every 10 minutes, geo-tags each device against five spatial
layers (Disadvantaged Areas v1/v2, Council Districts, Community Networks,
Neighborhoods), and exposes both citywide summary metrics and per-region
breakdowns.

The public map surface is read-only and unauthenticated. On top of it sits
a **rider surface** — sign-in, profiles and public usernames, GBFS-detected
tracked rides, a points ledger, device photos, and QR scans — which
requires a bearer token and accepts writes. See
[Accounts & sessions](#accounts--sessions) onward.

This document is the contract for frontend consumers. Backend internals
are in [README.md](./README.md).

---

## Base URL

```
https://data.scooter.fyi
```

All endpoints return `Content-Type: application/json` and use standard
HTTP status codes.

## Authentication

**Not required for the map.** Every read endpoint that powers the public
map and compliance dashboards is unauthenticated, as are
[routing](#routing), [geocoding](#geocoding) and anonymous device reports.
Two published-metadata endpoints are public too:
[`/api/v1/meta/pricing`](#get-apiv1metapricing) and
[`/api/v1/points/schedule`](#get-apiv1pointsschedule) — a client needs the
tax rate and the award table before anyone has signed in.

Everything else takes `Authorization: Bearer <token>` from one of the
sign-in doors in [Accounts & sessions](#accounts--sessions): profiles and
public usernames, [tracked rides](#tracked-rides-gbfs-detected),
the [points ledger](#points), [device photos](#device-photos),
[QR scans and recommendations](#device-engagement), discount reports, and
the signed-in device feed.

Two notes that surprise people:

- **`admin` is not a scope gate.** Admin authorization is membership in
  the `admin_allowlist` table, reachable through *any* sign-in door.
- **There is no paid tier.** Signed-in and admin are the only two gates
  in this system. Nothing is purchasable, nothing unlocks features, and
  there is no billing integration.

The `/admin/*` routes (not documented here) are a separate GitHub OAuth
HTML portal for operators.

## CORS

`Access-Control-Allow-Origin` is set for browser requests from:

- `https://scooter.fyi`
- `https://www.scooter.fyi`
- `https://denver.scooter.fyi`
- `https://denver-scooter-fyi.pages.dev`
- `https://weseeyouveo.com`
- `https://www.weseeyouveo.com`
- `https://keepdenverfair.com`
- `https://www.keepdenverfair.com`

Plus any URL matching the pattern:
- `https://<anything>.denver-scooter-fyi.pages.dev` (Cloudflare Pages preview deploys for the denver.scooter.fyi static site)

Other origins receive no CORS header (browser-side XHR will fail).
Server-side fetches from any origin work fine — CORS only applies to
browsers.

## Update cadence

A new snapshot lands **every 10 minutes**, approximately aligned to the
clock (e.g. xx:00, xx:10, xx:20). The upstream GBFS feed itself updates
on its own schedule; if Veo's feed is unchanged from the previous poll,
the cycle is **aborted as stale** and `/api/v1/snapshots/latest` will
keep returning the same row until fresh data arrives.

Recommended client polling interval: **60 seconds** if you want
near-real-time, **5 minutes** if you don't need to be aggressive. Going
faster than 60s wastes bytes — the data doesn't change.

## Conventions

| | |
|---|---|
| **Timestamps** | ISO 8601 with `Z` suffix or `+00:00` offset. Always UTC. Convert client-side for display. |
| **Counts** | Non-negative integers. |
| **Percentages** | Floats `0.00` – `100.00`, rounded to 2 decimal places. |
| **Nullable percentages** | A percentage is `null` when its denominator is 0 (e.g. `percent_bikes_v1` is `null` if no devices are inside v1). Always null-check before formatting. |
| **Device classification** | `form_factor` is one of `"bicycle"`, `"scooter"`, or `"unknown"`. The 22 core metrics count only `bicycle` and `scooter`; `unknown` devices are excluded from per-form-factor totals but included in `total_devices_*`. **Not taken as-given from Veo's upstream `vehicle_types.json`** — `vehicle_type_id: 4` (the seated, pedal-equipped "Apollo" model) is declared `"scooter"` upstream but corrected to `"bicycle"` here after direct visual confirmation, since the compliance-relevant distinction is the seated/pedaled/accessible form, not Veo's internal ID. See `_FORM_FACTOR_OVERRIDES` in `src/ingest.py`. |
| **Spatial filtering** | A device is `denver_core` if its coordinates fall inside the Denver city polygon (union of all 78 official neighborhood boundaries) **buffered outward by 200m**. The buffer keeps vehicles Veo lets riders start from just over the city line in the dataset — and, deliberately, in the compliance denominator. A rough lat/lon bounding box is a fast first-pass; final classification is the buffered polygon (membership is corrected both ways against it). Devices beyond the buffer (Aurora, Lakewood, the Veo repair shop if >200m out, etc.) are tagged `other_outlier` and excluded from all citywide metrics. `total_not_in_denver` exposes the count of excluded devices (China factory glitches + beyond-the-buffer combined). |

---

## Endpoints

### `GET /health`

Operational status. Use for uptime monitoring and to check freshness of
the most recent ingest cycle.

**Request:**
```http
GET /health
```

**Response 200:**
```json
{
  "last_data_ingest_ts": "2026-05-29T18:30:14+00:00",
  "last_data_upload_ts": "2026-05-28T06:00:02+00:00",
  "last_cycle_id": "8f3a2d10-1234-4abc-8def-0123456789ab",
  "last_retrieval_ts": "2026-05-29T18:34:51.012345+00:00"
}
```

| Field | Type | Description |
|---|---|---|
| `last_data_ingest_ts` | string \| null | UTC timestamp of the most recent successful cycle. `null` if no cycles have completed yet. |
| `last_data_upload_ts` | string \| null | UTC timestamp of the most recent successful 48-hour Cloudflare R2 archive upload. `null` until the first archive runs. |
| `last_cycle_id` | string \| null | UUID of the most recent successful cycle. |
| `last_retrieval_ts` | string | Server's current UTC timestamp at the moment of this response. Useful as a freshness check / clock skew detector. |

**Freshness heuristic:** if `(last_retrieval_ts - last_data_ingest_ts) > 15 minutes`, the pipeline is likely lagging.

---

### `GET /api/v1/snapshots/latest`

The full set of 22 RFP-mandated citywide compliance metrics from the
most recent cycle, **plus** the same per-group total/percent fields for
every other tracked equity group (`er1`–`er6` — see
[Tracked equity groups](#tracked-equity-groups-v1-v2-er1er6) below).
This is the most commonly consumed endpoint — it answers "what's the
fleet doing right now?"

**Request:**
```http
GET /api/v1/snapshots/latest
```

**Response 200:**
```json
{
  "cycle_id": "8f3a2d10-1234-4abc-8def-0123456789ab",
  "snapshot_time": "2026-05-29T18:30:14+00:00",
  "total_devices_denver": 5903,
  "total_devices_v1": 1284,
  "total_devices_v2": 946,
  "total_bike_denver": 4035,
  "total_bike_v1": 851,
  "total_bike_v2": 602,
  "total_scooter_denver": 1868,
  "total_scooter_v1": 433,
  "total_scooter_v2": 344,
  "total_not_in_denver": 12,
  "percent_all_devices_v1": 21.75,
  "percent_all_devices_v2": 16.03,
  "percent_all_bikes_v1": 21.09,
  "percent_all_bikes_v2": 14.92,
  "percent_all_scooters_v1": 23.18,
  "percent_all_scooters_v2": 18.42,
  "percent_bikes_denver": 68.36,
  "percent_scooters_denver": 31.64,
  "percent_bikes_v1": 66.28,
  "percent_scooters_v1": 33.72,
  "percent_bikes_v2": 63.64,
  "percent_scooters_v2": 36.36,
  "total_devices_equity": 1811,
  "total_bike_equity": 1211,
  "total_scooter_equity": 600,
  "percent_all_devices_equity": 30.68,
  "percent_all_bikes_equity": 30.01,
  "percent_all_scooters_equity": 32.12,
  "percent_bikes_equity": 66.87,
  "percent_scooters_equity": 33.13,
  "total_devices_er1": 1198,
  "total_bike_er1": 782,
  "total_scooter_er1": 416,
  "percent_all_devices_er1": 20.29,
  "percent_all_bikes_er1": 19.38,
  "percent_all_scooters_er1": 22.27,
  "percent_bikes_er1": 65.28,
  "percent_scooters_er1": 34.72
  /* … the same 8 fields, suffixed _er2 … _er6, omitted here for brevity … */
}
```

**Response 503:** No snapshot has landed yet (cold start, first ~15 s after deploy).
```json
{ "detail": "no snapshots yet" }
```

#### Field reference

| Field | Type | Definition |
|---|---|---|
| `cycle_id` | string | UUID of this snapshot's observation cycle. Stable per cycle, changes every 10 min. |
| `snapshot_time` | string | UTC ISO 8601 of when the cycle ran. |
| `total_devices_denver` | int | All devices (bikes + scooters + unknown form factors) located inside the Denver city polygon (union of the 78 official neighborhood boundaries) **buffered outward by 200m** — so vehicles startable from just over the city line are counted. Excludes devices beyond the buffer, such as Veo's repair facility (if >200m out). |
| `total_devices_v1` | int | Devices located inside the **Disadvantaged Areas v1** boundary. |
| `total_devices_v2` | int | Devices located inside the **Disadvantaged Areas v2** boundary. |
| `total_bike_denver` | int | `form_factor == "bicycle"` devices inside Denver. |
| `total_bike_v1` | int | Bicycles inside v1. |
| `total_bike_v2` | int | Bicycles inside v2. |
| `total_scooter_denver` | int | `form_factor == "scooter"` devices inside Denver. |
| `total_scooter_v1` | int | Scooters inside v1. |
| `total_scooter_v2` | int | Scooters inside v2. |
| `total_not_in_denver` | int | Devices reporting coordinates outside the 200m-buffered Denver city polygon. Includes both obvious outliers (China factory glitches, devices in transit) and adjacent-jurisdiction devices more than 200m past the line (Aurora, Lakewood, repair shops well over the city line). Excluded from all `*_denver`/`*_v1`/`*_v2` counts. |
| `percent_all_devices_equity` | float \| null | `total_devices_equity / total_devices_denver * 100`, against the city's official Equity Area map. **This is the primary RFP §3.0 compliance metric — Denver requires ≥30%.** |
| `percent_all_devices_v1` | float \| null | `total_devices_v1 / total_devices_denver * 100`. Was the primary metric until the city clarified the map in August 2026; retained as history. |
| `percent_all_devices_v2` | float \| null | `total_devices_v2 / total_devices_denver * 100`. |
| `percent_all_bikes_v1` | float \| null | `total_bike_v1 / total_bike_denver * 100`. |
| `percent_all_bikes_v2` | float \| null | `total_bike_v2 / total_bike_denver * 100`. |
| `percent_all_scooters_v1` | float \| null | `total_scooter_v1 / total_scooter_denver * 100`. |
| `percent_all_scooters_v2` | float \| null | `total_scooter_v2 / total_scooter_denver * 100`. |
| `percent_bikes_denver` | float \| null | `total_bike_denver / total_devices_denver * 100`. The bike share of the Denver fleet. |
| `percent_scooters_denver` | float \| null | `total_scooter_denver / total_devices_denver * 100`. The scooter share. (Bikes + scooters + unknown = 100%, so bikes + scooters may sum to <100.) |
| `percent_bikes_v1` | float \| null | Bike share of devices inside v1. |
| `percent_scooters_v1` | float \| null | Scooter share inside v1. |
| `percent_bikes_v2` | float \| null | Bike share inside v2. |
| `percent_scooters_v2` | float \| null | Scooter share inside v2. |

#### Tracked equity groups (v1, v2, er1–er6)

The 8 field families above (`total_devices_<g>`, `total_bike_<g>`,
`total_scooter_<g>`, `percent_all_devices_<g>`, `percent_all_bikes_<g>`,
`percent_all_scooters_<g>`, `percent_bikes_<g>`, `percent_scooters_<g>`)
are computed identically for **every** tracked group
`<g> ∈ {v1, v2, er1, er2, er3, er4, er5, er6}` — not just v1/v2. The
group registry lives in `src/equity_groups.py`; adding a group there
(plus a matching migration and `config.json` boundary entry) is the only
change needed for it to appear here and in the daily SLA endpoint below.

`er1`–`er6` are Denver DOTI's authoritative census-block-group Equity
Index, one group per exact `EquityGroupRank` tier (`er1` = highest
need). They are tracked **individually and atomically** — not
pre-combined into a cutoff — specifically so that whatever cutoff DOTI
confirms as contractually authoritative can be reconstructed from
history later (e.g. a "rank ≤ 2" metric = `er1 + er2`) without this
system having had to guess the right combination up front. None of
`er1`–`er6` is a compliance boundary, and the cutoff question they existed
to keep answerable was settled in August 2026 when the city named the
official Equity Area map: the `equity` group, and
`percent_all_devices_equity`, are now **the** RFP §3.0 metric.
`percent_all_devices_v1` and the `erN` families remain computed and
returned as history; see API_REQUIREMENTS.md §1.1a.

**Every tracked group also gets the same breakdown along a second,
independent axis: `vehicle_use_type` (sitting vs standing), not just
`form_factor` (bicycle vs scooter).** The field families are the same
shape, suffixed `sitting`/`standing` instead of `bike`/`scooter`:
`total_sitting_<g>`, `total_standing_<g>`, `percent_all_sitting_<g>`,
`percent_all_standing_<g>`, `percent_sitting_<g>`, `percent_standing_<g>`
— plus citywide `total_sitting_denver`, `total_standing_denver`,
`percent_sitting_denver`, `percent_standing_denver`. This exists because
`form_factor` is Veo's own GBFS vocabulary (itself corrected in at least
one case — see `vehicle_model_name` in the devices/current field
reference above), while sitting/standing is the accessibility-relevant
distinction for compliance purposes. The two dimensions agree for every
vehicle observed so far but are computed independently, driven by
`SPLIT_DIMENSIONS` in `src/equity_groups.py` — adding a third dimension
there (plus a matching migration) is the only change needed for it to
appear here too.

---

### `GET /api/v1/spatial-snapshot`

Per-region device counts for a single layer, suitable for rendering a
choropleth map. Returns the latest available snapshot (or the snapshot
nearest to a given timestamp).

**Query parameters:**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `layer` | string | yes | — | One of `v1`, `v2`, `er1`, `er2`, `er3`, `er4`, `er5`, `er6`, `council_district`, `community_network`, `neighborhood`. See [Layer reference](#layer-reference). |
| `time` | string | no | latest | ISO 8601 UTC timestamp. Snaps to the most recent snapshot at or before this time. Useful for historical playback. |

**Example request:**
```http
GET /api/v1/spatial-snapshot?layer=neighborhood
```

**Response 200:**
```json
{
  "snapshot_time": "2026-05-29T18:30:14+00:00",
  "layer": "neighborhood",
  "regions": {
    "NB_AthmarPark": { "total": 41, "bikes": 28, "scooters": 13 },
    "NB_Auraria":    { "total": 87, "bikes": 52, "scooters": 35 },
    "NB_Baker":      { "total": 73, "bikes": 49, "scooters": 24 },
    "NB_CBD":        { "total": 312, "bikes": 198, "scooters": 114 },
    "NB_CapitolHill":{ "total": 264, "bikes": 171, "scooters": 93 },
    "NB_FivePoints": { "total": 145, "bikes": 102, "scooters": 43 }
    /* … 72 more entries … */
  }
}
```

**Response 404:** layer has no data yet (no cycles have populated this layer — should only happen at cold start).
```json
{ "detail": "no data for layer=neighborhood" }
```

**Response 400:** bad `time` parameter.
```json
{ "detail": "bad time format: ..." }
```

#### Notes

- `regions` is a flat map: `{region_name → counts}`. Region names are stable strings — see [Layer reference](#layer-reference) for the full enumeration per layer.
- Counts are integers ≥ 0. `total = bikes + scooters + unknown`, where `unknown` is any device whose `form_factor` couldn't be resolved. Almost always `total == bikes + scooters` exactly.
- The shape is identical for every layer; only the set of region names changes.
- A region appears in the response **even when its count is 0**. So you can `Object.keys(regions)` to get the full layer enumeration once and not worry about missing regions on later polls.

---

### `GET /api/v1/analytics/trend`

Time-series counts for a single region. Use for line charts, sparklines,
trend deltas.

**Query parameters:**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `layer` | string | yes | — | Same set as `spatial-snapshot`. |
| `name` | string | yes | — | The `region_name` (e.g. `NB_FivePoints`, `CD_3`, `V1_007`). |
| `range` | string | no | `7d` | Time window. Format: `\d+[dh]` (`24h`, `7d`, `30d`). Max practical range depends on data retention; raw points are flushed every 48 h but aggregates persist indefinitely. |

**Example request:**
```http
GET /api/v1/analytics/trend?layer=neighborhood&name=NB_FivePoints&range=24h
```

**Response 200:**
```json
{
  "layer": "neighborhood",
  "region_name": "NB_FivePoints",
  "range": "24h",
  "points": [
    { "snapshot_time": "2026-05-28T18:30:14+00:00", "count_total": 132, "count_bikes": 91, "count_scooters": 41 },
    { "snapshot_time": "2026-05-28T18:40:11+00:00", "count_total": 135, "count_bikes": 93, "count_scooters": 42 },
    { "snapshot_time": "2026-05-28T18:50:09+00:00", "count_total": 138, "count_bikes": 95, "count_scooters": 43 }
    /* … one point per snapshot in the window, ~144 points for range=24h … */
  ]
}
```

**Response 400:** bad `range`.
```json
{ "detail": "range must look like '7d' or '24h'" }
```

#### Notes

- Points are returned in ascending `snapshot_time` order (oldest first).
- Approximately one point per snapshot, so `range=24h` ≈ 144 points (6 per hour × 24), `range=7d` ≈ 1,008 points.
- If a region's count was 0 for some snapshots in the window, those points are still included with `count_total = 0`. (Gaps in the time-series indicate the cycle was aborted as `stale` or `upstream_failure`, not that the region had no devices.)
- Empty `points` array means no snapshots exist in the requested window — most likely the region name is wrong, or the layer was added after the window's start.

---

---

### `GET /api/v1/boundaries`

Lists every available boundary layer with its feature count, bbox, and
the GeoJSON URL. Useful as a layer-toggle catalog: hit this once on app
load to discover what's available, then lazy-load each layer's
geometry when the user enables it.

**Request:**
```http
GET /api/v1/boundaries
```

**Response 200:**
```json
{
  "layers": [
    { "region_category": "disadvantaged_areas", "region_type": "v1", "feature_count": 34, "bbox": [-105.0626, 39.6473, -104.7718, 39.7983], "url": "/api/v1/boundaries/v1" },
    { "region_category": "disadvantaged_areas", "region_type": "v2", "feature_count": 65, "bbox": [-105.0626, 39.6450, -104.7344, 39.7984], "url": "/api/v1/boundaries/v2" },
    { "region_category": "disadvantaged_areas", "region_type": "er1", "feature_count": 34, "bbox": [-105.0626, 39.6450, -104.7344, 39.7984], "url": "/api/v1/boundaries/er1" },
    /* … er2 … er6, same shape … */
    { "region_category": "council_districts", "region_type": "council_district", "feature_count": 11, "bbox": [-105.1100, 39.6143, -104.5995, 39.9142], "url": "/api/v1/boundaries/council_district" },
    { "region_category": "community_networks", "region_type": "community_network", "feature_count": 13, "bbox": [-105.1100, 39.6143, -104.7344, 39.8274], "url": "/api/v1/boundaries/community_network" },
    { "region_category": "neighborhoods", "region_type": "neighborhood", "feature_count": 78, "bbox": [-105.1100, 39.6143, -104.5996, 39.9142], "url": "/api/v1/boundaries/neighborhood" }
  ]
}
```

Cached for 1 hour at the edge (`Cache-Control: public, max-age=3600`).

---

### `GET /api/v1/boundaries/{layer}`

Returns the full GeoJSON FeatureCollection for one boundary layer. The
URL is what `/api/v1/boundaries` advertises.

**Layer values:** `equity`, `v1`, `v2`, `er1`–`er6`, `neighborhood`, `council_district`, `community_network`.

`equity` is the city's official Equity Area map — the one the contract binds. See the Layer reference for the rest.

**Example request:**
```http
GET /api/v1/boundaries/neighborhood
```

**Response 200:**
```json
{
  "type": "FeatureCollection",
  "metadata": {
    "region_category": "neighborhoods",
    "region_type": "neighborhood",
    "feature_count": 78,
    "bbox": [-105.1100, 39.6143, -104.5996, 39.9142]
  },
  "features": [
    {
      "type": "Feature",
      "id": "NB_AthmarPark",
      "geometry": { "type": "Polygon", "coordinates": [[[ /* ring coords */ ]]] },
      "properties": {
        "region_category": "neighborhoods",
        "region_type": "neighborhood",
        "region_name": "NB_AthmarPark"
      }
    }
    /* … 77 more … */
  ]
}
```

#### Notes

- **Heavily cached** (`Cache-Control: public, max-age=86400, stale-while-revalidate=604800`) — boundaries change only when the city republishes the polygon files (rare). Safe to fetch once per session.
- **`id` matches `properties.region_name`** — same convention as `/api/v1/devices/current` and `/api/v1/spatial-snapshot`. Map libraries use top-level `id` for feature-state (click, hover); paint expressions use `["get", "region_name"]` from properties.
- **Geometry types vary by layer:** v1, v2, community_network, and neighborhood are all `Polygon`; council_district has some `MultiPolygon`. Mapbox/MapLibre/Leaflet handle both transparently.
- **Approx response sizes (gzip):** v1 ~15 KB, v2 ~70 KB, council_district ~150 KB, community_network ~30 KB, neighborhood ~90 KB.
- **Joining boundaries with live counts:** the `region_name` in this endpoint is the same key as `/api/v1/spatial-snapshot?layer={layer}.regions` and `/api/v1/analytics/trend?layer={layer}&name={region_name}`. See the choropleth example below.

#### Map rendering example (MapLibre GL JS — outline overlay with layer toggle)

```javascript
const map = new maplibregl.Map({ /* ... */ });
const BASE = "https://data.scooter.fyi/api/v1/boundaries";

const layerDefs = [
  { id: "v1",                 label: "Disadvantaged Areas (v1)",  color: "#e63946" },
  { id: "v2",                 label: "Disadvantaged Areas (v2)",  color: "#c1121f" },
  { id: "neighborhood",       label: "Neighborhoods",             color: "#457b9d" },
  { id: "council_district",   label: "City Council Districts",    color: "#2a9d8f" },
  { id: "community_network",  label: "City Regions",              color: "#8338ec" },
];

map.on("load", async () => {
  for (const def of layerDefs) {
    map.addSource(`bnd-${def.id}`, { type: "geojson", data: `${BASE}/${def.id}` });
    map.addLayer({
      id: `${def.id}-fill`,
      type: "fill",
      source: `bnd-${def.id}`,
      paint: { "fill-color": def.color, "fill-opacity": 0.1 },
      layout: { visibility: "none" },
    });
    map.addLayer({
      id: `${def.id}-outline`,
      type: "line",
      source: `bnd-${def.id}`,
      paint: { "line-color": def.color, "line-width": 1.5 },
      layout: { visibility: "none" },
    });
  }

  // Toggle UI — built with safe DOM methods, no innerHTML
  const controls = document.querySelector("#overlay-controls");
  for (const def of layerDefs) {
    const label = document.createElement("label");
    label.style.color = def.color;
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.layer = def.id;
    input.addEventListener("change", () => {
      const v = input.checked ? "visible" : "none";
      map.setLayoutProperty(`${def.id}-fill`, "visibility", v);
      map.setLayoutProperty(`${def.id}-outline`, "visibility", v);
    });
    label.appendChild(input);
    label.appendChild(document.createTextNode(" " + def.label));
    controls.appendChild(label);
  }
});
```

#### Choropleth example (color polygons by current device count)

Combines `/api/v1/boundaries/{layer}` (static geometry) with `/api/v1/spatial-snapshot?layer={layer}` (live counts), joined by `region_name`:

```javascript
async function loadChoropleth(layerType) {
  const [geo, counts] = await Promise.all([
    fetch(`https://data.scooter.fyi/api/v1/boundaries/${layerType}`).then(r => r.json()),
    fetch(`https://data.scooter.fyi/api/v1/spatial-snapshot?layer=${layerType}`).then(r => r.json()),
  ]);

  // Merge counts into properties so paint expressions can use them
  for (const feat of geo.features) {
    const c = counts.regions[feat.properties.region_name] || { total: 0, bikes: 0, scooters: 0 };
    feat.properties.count_total = c.total;
    feat.properties.count_bikes = c.bikes;
    feat.properties.count_scooters = c.scooters;
  }

  map.getSource("choropleth").setData(geo);
}

map.addSource("choropleth", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
map.addLayer({
  id: "choropleth-fill",
  type: "fill",
  source: "choropleth",
  paint: {
    "fill-color": [
      "interpolate", ["linear"], ["get", "count_total"],
      0,   "#f1faee",
      50,  "#a8dadc",
      150, "#457b9d",
      300, "#1d3557",
    ],
    "fill-opacity": 0.7,
  },
});
loadChoropleth("neighborhood");
setInterval(() => loadChoropleth("neighborhood"), 90_000);
```

#### Leaflet equivalent (outline overlay with layer control)

```javascript
const map = L.map("map").setView([39.74, -104.99], 11);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png").addTo(map);

const BASE = "https://data.scooter.fyi/api/v1/boundaries";
const overlays = {};
for (const def of [
  { id: "v1", label: "Disadvantaged Areas (v1)", color: "#e63946" },
  { id: "v2", label: "Disadvantaged Areas (v2)", color: "#c1121f" },
  { id: "neighborhood", label: "Neighborhoods", color: "#457b9d" },
  { id: "council_district", label: "City Council Districts", color: "#2a9d8f" },
  { id: "community_network", label: "City Regions", color: "#8338ec" },
]) {
  const geo = await fetch(`${BASE}/${def.id}`).then(r => r.json());
  overlays[def.label] = L.geoJSON(geo, {
    style: { color: def.color, weight: 1.5, fillOpacity: 0.1 },
    onEachFeature: (feat, lyr) => lyr.bindPopup(feat.properties.region_name),
  });
}
L.control.layers({}, overlays, { collapsed: false }).addTo(map);
```

---

### `GET /api/v1/devices/history/hourly`

Fleet size over time — the Tools drawer's "Devices over time" chart. Public
(the same aggregate count the map footer already publishes, just over
time); `?days=1..14`, default 14. Each hour is the **last** cycle observed
in that hour:

```json
{ "days": 14, "hours": [
  { "hour": "2026-08-10T14:00:00+00:00", "total": 500, "available": 420,
    "reserved": 30, "out_of_service": 50,
    "models": {
      "Astro": { "available": 200, "reserved": 15, "out_of_service": 25 },
      "Rover": { "available": 220, "reserved": 15, "out_of_service": 25 } } },
  ...
] }
```

Counts are Denver-core devices on the polygon-corrected status, matching
`total_devices_denver`'s scope. `out_of_service` = `is_disabled` (disabled
wins over reserved, per GBFS); absent booleans read as available.
`models` carries the same three status counts **per model**, keyed by the
feed's own display names — a new model simply appears, every metric can be
broken down by model, and the top-level counts are exactly the per-model
sums (a model's total is the three counts summed).

Hours predating sql/069 (or an ingest outage) backfill from
`snapshot_metadata_core`'s totals with the breakdowns `null` — an honest
"we know how many, not what state", never zeros.

---

### `GET /api/v1/devices/current`

GeoJSON FeatureCollection of every device's current position from the
most recent successfully-completed cycle. Suitable for direct ingestion
into map libraries (Mapbox GL JS, MapLibre GL JS, Leaflet, OpenLayers).

By default returns **only devices inside the 200m-buffered Denver city
polygon** (`spatial_status='denver_core'`) — China-factory glitches and
devices more than 200m past the city line (Aurora, Lakewood, repair
shops) are hidden unless explicitly requested. The 200m buffer keeps
vehicles startable from just over the line on the map.

**Query parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `form_factor` | string | (all) | Filter by `bicycle` or `scooter`. |
| `spatial_status` | string | (default below) | Filter by `denver_core`, `china_glitch`, or `other_outlier`. Explicit value overrides `include_outliers`. |
| `include_outliers` | bool | `false` | When true, returns devices regardless of envelope. Ignored if `spatial_status` is set. |
| `bbox` | string | (none) | `min_lon,min_lat,max_lon,max_lat` WGS84 bounding box. Useful for viewport-level queries. |
| `include` | string | (none) | Comma-separated opt-in field groups: `ranks`, `h3`. Unknown tokens → `400`. See [Lean default payload](#lean-default-payload--include-opt-ins) below. |

**Example request:**
```http
GET /api/v1/devices/current?form_factor=scooter
```

**Response 200:**
```json
{
  "type": "FeatureCollection",
  "metadata": {
    "cycle_id": "8f3a2d10-1234-4abc-8def-0123456789ab",
    "snapshot_time": "2026-05-30T18:30:14+00:00",
    "device_count": 1868,
    "filters": {
      "form_factor": "scooter",
      "spatial_status": null,
      "include_outliers": false,
      "bbox": null
    },
    "include": []
  },
  "features": [
    {
      "type": "Feature",
      "id": "abc123",
      "geometry": { "type": "Point", "coordinates": [-104.9876, 39.7392] },
      "properties": {
        "device_id": "abc123",
        "form_factor": "scooter",
        "spatial_status": "denver_core",
        "vehicle_identifier": "8c4a1f0d2e9b7a35",
        "is_disabled": false,
        "is_reserved": false,
        "current_range_meters": 45293,
        "battery_percent": 100,
        "propulsion_type": "electric",
        "number_failed_starts": 0,
        "first_observed_at_location": "2026-05-30T16:10:09+00:00",
        "reliability_tier": "ok",
        "dwell_percentile_hood": 42,
        "dwell_peer_median_hours": 6.4,
        "vehicle_use_type": "standing",
        "vehicle_model_name": "Astro",
        "feature_status": "up_to_date",
        "device_features": {
          "bell": true, "cup_holder": false, "phone_holder": true,
          "basket": false, "poor_condition": []
        }
      }
    },
    {
      "type": "Feature",
      "id": "abc124",
      "geometry": { "type": "Point", "coordinates": [-104.9851, 39.7411] },
      "properties": {
        "device_id": "abc124",
        "form_factor": "bicycle",
        "spatial_status": "denver_core",
        "vehicle_identifier": "1b6e2d44a991f070",
        "is_disabled": false,
        "is_reserved": true,
        "current_range_meters": 37538,
        "battery_percent": 84,
        "propulsion_type": "electric",
        "number_failed_starts": 0,
        "first_observed_at_location": "2026-05-30T17:40:12+00:00",
        "reliability_tier": "unknown",
        "dwell_percentile_hood": 18,
        "dwell_peer_median_hours": 7.1,
        "vehicle_use_type": "sitting",
        "vehicle_model_name": "Apollo",
        "feature_status": "needs_features_confirmed",
        "device_features": null
      }
    }
    /* … ~1,866 more features … */
  ]
}
```

#### Lean default payload + `?include=` opt-ins

The default field set above is deliberately lean — this payload is
re-downloaded every 90 s by low-end phones. Two field groups exist but
are **off the wire unless requested** (`metadata.include` echoes what was
applied):

- `?include=ranks` → the seven analysis-mode battery-ranking fields:
  `range_percentile_by_type`, `range_rank_unique_by_type`,
  `range_rank_all_by_type`, `range_rank_all_devices`,
  `range_rank_h3_8_peers`, `range_rank_h3_9_peers`,
  `range_rank_h3_10_peers`.
- `?include=h3` → `h3_8_index` / `h3_9_index` / `h3_10_index`, now
  **string-encoded** in canonical h3 form (e.g. `"8928308280fffff"`).
  When these fields were integers they exceeded JS `MAX_SAFE_INTEGER`
  and lost precision in `JSON.parse`; the opt-in re-introduction fixes
  the encoding at the same time. For most aggregation use cases prefer
  [`/api/v1/h3/aggregates`](#get-apiv1h3aggregatesres8910), which does
  the bucketing server-side.

Combine groups with a comma: `?include=ranks,h3`. Unknown tokens → `400`.

#### Conditional requests (ETag / 304)

Every response carries `Cache-Control: public, max-age=30` and a weak
ETag keyed on `(cycle_id, include tokens)`. Send it back and unchanged
polls cost headers instead of megabytes:

```javascript
let etag = null;
async function refresh() {
  const r = await fetch("https://data.scooter.fyi/api/v1/devices/current",
                        { headers: etag ? { "If-None-Match": etag } : {} });
  if (r.status === 304) return;        // same cycle — nothing to redraw
  etag = r.headers.get("ETag");
  map.getSource("devices").setData(await r.json());
}
```

The ETag changes only when a new cycle lands (~every 10 min). It is
**weak** on purpose: `has_negative_report`, dwell values, and the tiers
derived from them drift *within* a cycle, so a 304 can defer those by at
most one cycle length.

#### Feature property reference

| Field | Type | Description |
|---|---|---|
| `device_id` | string | The upstream Veo `bike_id` from GBFS `free_bike_status`. **Rotates per trip** by GBFS spec mandate — do not treat as stable. |
| `form_factor` | string | `"bicycle"`, `"scooter"`, or `"unknown"`. Not taken as-given from Veo's upstream `vehicle_types.json` — corrected against direct visual confirmation where the upstream registry is known to be wrong (see `vehicle_model_name` below). |
| `spatial_status` | string | `"denver_core"`, `"china_glitch"`, or `"other_outlier"`. |
| `vehicle_identifier` | string \| null | 16-hex-character stable per-scooter identifier (e.g. `"8c4a1f0d2e9b7a35"`). Persistent across trips, unlike `device_id`. Computed as `HMAC-SHA256(server_salt, visible_plate)[:16]`. This is the stable key for reports and cross-cycle joins. May be null if the upstream payload omits a plate. **The raw plate is NOT exposed on this public endpoint** — it's served only to `ADMIN_EMAILS` sessions via `/api/v1/user/devices/current` (see below). |
| `is_disabled` | bool \| null | `true` when the scooter is out of service (low battery, mechanical fault, impound). Disabled devices still count toward fleet totals because they occupy space. |
| `is_reserved` | bool \| null | `true` when a rider has the scooter on hold (typically a 5–10 min reservation window before unlock). |
| `current_range_meters` | int \| null | Estimated remaining range from upstream, in meters. |
| `battery_percent` | int \| null | Server-computed 0–100 state of charge. Exact SoC recovery: upstream `current_range_meters` is an integer percent mapped through one fleet-wide 100-value lookup table (same table for every vehicle type — verified stable across a 37-day archive; see `data/range_soc_lut.json` and API_REQUIREMENTS.md §7.1), so percent = the value's rank in that table. Values outside the table (vendor drift) fall back to linear scaling against the observed 45,293 m full-charge cap, clamped to [0, 100]. `null` when range is missing (e.g. pedal-only `"human"` bikes). NOT scaled by the rated per-type max, which the archive disproved (a full bicycle would read 68%). |
| `propulsion_type` | string \| null | `"electric"`, `"electric_assist"` (pedal-assist), or `"human"` (pedal-only). Splits the `form_factor: "bicycle"` bucket into throttle e-bikes vs pedal-assist vs acoustic. |
| `h3_8_index` / `h3_9_index` / `h3_10_index` | string \| null | **Opt-in via `?include=h3`.** [Uber H3](https://h3geo.org/) hexagonal cell IDs at resolutions 8 (~750m wide), 9 (~210m), and 10 (~75m), **string-encoded in canonical h3 form** (previously raw 64-bit integers, which exceed JS `MAX_SAFE_INTEGER`). Same value across resolutions for stationary devices; change when the scooter moves. Prefer `/api/v1/h3/aggregates` for per-cell rollups. |
| `range_percentile_by_type` | string \| null | **Opt-in via `?include=ranks`.** One of `"0"`, `"25"`, `"50"`, `"75"`. Which quartile of unique `current_range_meters` values **within the same `form_factor`** this scooter falls into. `"75"` = top quartile (most range). |
| `range_rank_unique_by_type` | string \| null | **Opt-in via `?include=ranks`.** `"x/y"` where `x` is the rank of this scooter's range value among the `y` *distinct* range values within its form_factor (ascending; ties share a position). |
| `range_rank_all_by_type` | string \| null | **Opt-in via `?include=ranks`.** `"x/y"` where `y` is the count of scooters of this form_factor and `x` is this scooter's rank ascending (1 = lowest range). **Ties get the highest position in the tied group**: 20 scooters tied for the top range in a fleet of 100 all show `"100/100"`. |
| `range_rank_all_devices` | string \| null | **Opt-in via `?include=ranks`.** Same as above but `y` = all eligible scooters across types. |
| `range_rank_h3_8_peers` / `range_rank_h3_9_peers` / `range_rank_h3_10_peers` | string \| null | **Opt-in via `?include=ranks`.** Range rank within the same h3 cell at the given resolution. A scooter alone in its cell shows `"1/1"`. |
| `has_negative_report` | bool | `true` when ≥1 citizen-submitted report has been filed against this `vehicle_identifier` at this exact `h3_10_index` cell within the last 24h. Becomes `false` automatically when the scooter moves to a different h3_10 cell. Submit reports via `POST /api/v1/reports`. |
| `feature_status` | string | How much to trust what we know about this vehicle's crowdsourced equipment: `"needs_features_confirmed"` (nobody has ever reported it — every device starts here), `"needs_review"` (two reports disagreed), or `"up_to_date"`. Always on the wire, never behind an `?include=` token: it is what a client's "☑️ Confirm Features" affordance reads to decide whether it is offering 12, 14 or 6 points, so opting in would mean showing the wrong number. See [`POST /api/v1/reports/device-features`](#post-apiv1reportsdevice-features). |
| `device_features` | object \| null | `{ bell, cup_holder, phone_holder, basket, poor_condition[] }` — the current consensus. **`null` until something is known about the vehicle**, and each field inside is itself **tri-state: `true` / `false` / `null`** — `false` claims a rider looked and saw nothing, `null` says nobody has answered that question yet. Partial objects are normal: a vehicle known only through a ride-survey basket answer (or the Rover catalog seed) carries `basket` with the other three `null`, and a vehicle confirmed before the basket question existed (sql/058) carries `basket: null`. Filter with `=== true` and the distinction never bites — an unknown feature doesn't satisfy a "must have it" filter, same as an absent one. `poor_condition` lists which of the *present* features are not in good condition (always a subset of the `true` ones; empty means everything works). |
| `quality_designation` | string | One of `"poor"`, `"acceptable"`, `"good"`, `"great"`, or `"N/A"`. Composite score from range, dwell time, failed-start count, active negative reports, and peer-relative dwell outliers (a dwell-outlier per the rules under `dwell_percentile_hood` costs one extra tier, stacking with the absolute-dwell demerits). `"N/A"` for disabled, reserved, or rangeless devices. See README / src/quality.py for the rule set. |
| `number_failed_starts` | int \| null | How many times the upstream `bike_id` rotated (someone started a rental) **without the scooter moving** since it arrived at its current location. Resets to 0 when the scooter moves. Null when the device isn't state-tracked (no plate in the upstream payload). |
| `first_observed_at_location` | string \| null | UTC ISO 8601 timestamp of when we first observed the scooter at its current location. `now - first_observed_at_location` = dwell time. Resets when the scooter moves. Null when the device isn't state-tracked. |
| `reliability_tier` | string | `"ok"`, `"unknown"`, or `"high_risk"` — a single "will it actually unlock?" signal. `high_risk`: an active negative report, ≥2 failed starts, 1 failed start + ≥24 h dwell, ≥72 h dwell (recalibrated from 96 h — 48 h is already the citywide p90), or a **peer-relative dwell outlier with ≥48 h dwell** (see `dwell_percentile_hood`). `unknown`: device not state-tracked; `quality_designation` is `"N/A"` (disabled/reserved/rangeless); exactly 1 failed start without enough dwell to corroborate it as high_risk (one `bike_id` rotation can still be a rebalancing scan, but it's no longer a clean bill of health either); or dwell ≥2× `dwell_peer_median_hours` **and** past the patience floor `min(36 h, 16 × dwell_peer_median_hours)` (a softer, earlier-warning version of the high_risk outlier rule — just the ratio, no percentile; both ratio rules compare against the unrounded median, not the rounded value on the wire — see that field's note). `ok`: everything else. The patience floor scales with how busy the block is: a 1 h median can't flag anything before 16 h, and any block with a median above 2.25 h waits the flat 36 h. It only ever *delays* an `unknown` — a block sleepy enough that 2× its median already exceeds 36 h keeps being judged on its own ratio. Failed starts and negative reports are never gated by it. Formula lives in `src/quality.py` (`compute_reliability_tier`) so the audit stays reproducible. Unlike `quality_designation`, battery range never affects this field. |
| `dwell_percentile_hood` | int \| null | 0–100: where this device's dwell sits (≤-fraction, self included) among its **local peers** — all state-tracked devices in `gridDisk(r9 cell, 1)` (its res-9 hex + 6 neighbors, ~0.74 km² centered on the device), widening to `gridDisk(r9, 2)` and then the citywide distribution whenever a ring has <5 peers. `null` when the device isn't state-tracked or no ≥5-peer set exists even citywide. A device is a **dwell outlier** when percentile ≥90 AND dwell ≥3× `dwell_peer_median_hours` AND dwell ≥24 h (absolute floor so high-turnover blocks can't flag fresh scooters). |
| `dwell_peer_median_hours` | float \| null | Median dwell (hours, 1 decimal) of the peer set used for `dwell_percentile_hood` — lets the UI explain verdicts: "idle 31 h — 5× its block's typical 6 h". Same null conditions. **Rounded for display only** — the `high_risk` outlier rule (≥3×), the `unknown` ratio rule (≥2×) and the `unknown` patience floor (`min(36 h, 16 ×)`) in `reliability_tier` all compare each device's dwell against the underlying unrounded median (`DwellPeerStats.peer_median_hours` in `src/quality.py`), so a value that looks exactly on a 2×/3×/16× boundary here may already be on the other side of it internally. |
| `vehicle_use_type` | string \| null | `"sitting"` or `"standing"` — whether a rider sits or stands to operate the vehicle. Independent of `form_factor`: this is the accessibility-relevant distinction for compliance purposes, tracked as its own axis in case a future vehicle class doesn't follow the current pattern (every bicycle sits, every scooter stands, as of everything observed so far). Null for a `vehicle_type_id` we haven't classified in any way. See [Tracked equity groups](#tracked-equity-groups-v1-v2-er1er6) for how this feeds the compliance snapshot. |
| `vehicle_model_name` | string \| null | Veo's own in-app display name for the physical vehicle model — `"Astro"` (kick scooter), `"Cosmo"` (throttle e-bike, no pedals), `"Apollo"` (two-person pedal e-bike, seated, ~18mph), or `"Rover"` (three-wheeled seated cargo trike, in the feed since 2026-07, previously mislabelled `"Cosmo"` until 2026-07-29). Visually confirmed per `vehicle_type_id`, not read from any upstream field (Veo's GBFS feed doesn't expose model names). Null for a `vehicle_type_id` not yet confirmed — absence doesn't imply anything about the vehicle, just that nobody's looked yet. **This vocabulary is open-ended: Veo adds models, and this field gains values without an API version bump** (Rover is exactly that event). Clients MUST render devices whose model they don't recognize — treat an unknown name like `null` (generic pin, no model chip), never as a reason to drop the device from the map or from filters. |

#### Public write endpoints

| Endpoint | Body | Purpose |
|---|---|---|
| `POST /api/v1/reports` | `{vehicle_identifier?\|vehicle_plate?, report_lat, report_lon, problem_tags[], problem_description?, h3_*_index?}` | Submit a negative report. Server computes its own h3 cells from `report_lat`/`report_lon`. At least one of `vehicle_identifier` or `vehicle_plate` is required. Returns `{id, reported_at, vehicle_identifier, h3_10_index}`. |
| `POST /api/v1/quality-feedback` | `{vehicle_identifier, h3_10_index, polarity, designation_observed?, comment?}` | Positive or negative feedback on our `quality_designation`. `polarity` is `"positive"` or `"negative"`. Returns `{id, feedback_at}`. |

**Anti-abuse:** these endpoints are currently public with no rate-limit
or CAPTCHA. Before any public-launch marketing push we'll add per-IP
rate limits and consensus surfacing (a report only flips
`has_negative_report` to `true` once N independent reporters file it).
Until then, treat the public report flow as best-effort.


**Response 503:** No completed cycle yet (very fresh deploy).
```json
{ "detail": "no completed cycles yet" }
```

**Response 400:** Malformed `bbox`.

#### Notes

- `coordinates` is `[longitude, latitude]` per the GeoJSON spec (note: x, y order — not lat/lon).
- `id` and `properties.device_id` are the same value, duplicated for convenience: GeoJSON `id` is what map libraries use for feature interaction (click handlers, hovers); `properties.device_id` survives projection through layer styles.
- `device_id` is the upstream Veo `bike_id`, which is **already public via Veo's GBFS feed** — no new privacy exposure.
- Typical response sizes (lean default field set — the pre-diet payload with `?include=ranks,h3` runs ~35–40% larger before compression):
  - All Denver devices (~5,900 features): ~300 KB JSON, ~65 KB gzip
  - Filtered to scooters (~1,870 features): ~95 KB / ~22 KB gzip
  - bbox-filtered (downtown ~500 features): ~26 KB / ~7 KB gzip
- Responses are gzip-compressed at the origin and brotli-recoded at the CDN edge for clients that prefer it; send `Accept-Encoding` as usual.
- Recommended polling: **60–120 seconds with `If-None-Match`** (see the ETag section above) — unchanged polls return `304` for free. The upstream cycle only fires every 10 minutes, so faster polling wastes bytes.
- For viewport-aware rendering, pass `bbox` to keep response sizes small. The server-side filter is index-backed and cheap.

#### Map rendering example (MapLibre GL JS)

```javascript
const map = new maplibregl.Map({ /* ... */ });

map.on("load", async () => {
  map.addSource("devices", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "scooters",
    type: "circle",
    source: "devices",
    filter: ["==", ["get", "form_factor"], "scooter"],
    paint: { "circle-radius": 4, "circle-color": "#e63946" },
  });
  map.addLayer({
    id: "bikes",
    type: "circle",
    source: "devices",
    filter: ["==", ["get", "form_factor"], "bicycle"],
    paint: { "circle-radius": 4, "circle-color": "#1d4ed8" },
  });

  async function refresh() {
    const r = await fetch("https://data.scooter.fyi/api/v1/devices/current");
    const geo = await r.json();
    map.getSource("devices").setData(geo);
    document.querySelector("#count").textContent =
      `${geo.metadata.device_count} devices · ${new Date(geo.metadata.snapshot_time).toLocaleTimeString()}`;
  }
  refresh();
  setInterval(refresh, 90_000);
});
```

#### Viewport-aware variant (Leaflet)

```javascript
async function refresh() {
  const b = map.getBounds();
  const bbox = `${b.getWest()},${b.getSouth()},${b.getEast()},${b.getNorth()}`;
  const r = await fetch(`https://data.scooter.fyi/api/v1/devices/current?bbox=${bbox}`);
  const geo = await r.json();
  layer.clearLayers();
  L.geoJSON(geo, {
    pointToLayer: (feat, latlng) =>
      L.circleMarker(latlng, {
        radius: 4,
        color: feat.properties.form_factor === "scooter" ? "#e63946" : "#1d4ed8",
      }),
  }).addTo(layer);
}
map.on("moveend", refresh);
refresh();
```

---

### `GET /api/v1/user/devices/current`

The **signed-in** map feed. Identical shape and query parameters to
`/api/v1/devices/current` (`form_factor`, `spatial_status`,
`include_outliers`, `bbox`, `include`, ETag/304), but requires a rider
session (`Authorization: Bearer <token>` from magic-link or Google
sign-in) and — when the session email is in `ADMIN_EMAILS` — adds the
admin-only private fields.

This replaces the retired `/api/v1/private/devices/current`. Use it as the
drop-in map source once a user is signed in: any signed-in rider gets the
public field set (so the map works for everyone signed in), `ADMIN_EMAILS`
sessions additionally get plates.

**Auth:** `401` when the bearer is missing, invalid, or expired.

**Response:** same as `/api/v1/devices/current`, plus `metadata.viewed_by`
(the session email) and `metadata.admin` (whether the private fields were
included). When the session email is in `ADMIN_EMAILS` each feature also
carries:

| Field | Type | Description |
|---|---|---|
| `vehicle_plate` | string \| null | The raw visible plate painted on the scooter. Null when the upstream payload had no plate. |
| `first_ever_observed_at` | string \| null | UTC ISO 8601 of the first time we ever saw this `vehicle_identifier` (not reset by moves, unlike `first_observed_at_location`). |
| `max_observed_range_meters` | int \| null | Highest `current_range_meters` ever observed for this vehicle. |
| `max_observed_range_at` | string \| null | UTC ISO 8601 of when that max was observed. |

**Admin gate:** the private fields unlock by raw membership of the session
email in `ADMIN_EMAILS`, so they work for an allowlisted email signed in
via **either** door (magic-link or Google) — the same gate as the
operator-facing `/api/v1/private/*` endpoints. Both doors prove email
ownership. A non-allowlisted rider gets the base map.

**Caching:** `Cache-Control: private, max-age=30` (per-user; never
shared-cached) with a cycle-keyed weak ETag that also varies on whether
plates were included.

```javascript
// Signed-in map: one endpoint, richer for admins, works for everyone.
const r = await fetch("https://data.scooter.fyi/api/v1/user/devices/current",
                      { headers: { "Authorization": "Bearer " + token } });
if (r.status === 401) { /* session expired — prompt re-auth, fall back to public map */ }
const geo = await r.json();
map.getSource("devices").setData(geo);
if (geo.metadata.admin) showPlateLayer(geo);   // admins only
```

---

### `GET /api/v1/h3/aggregates?res=8|9|10`

Per-cell aggregates on the [Uber H3](https://h3geo.org/) hex grid, for
analysis-mode choropleth layers — colored hexes by device density, usage
heat, battery, risk, or dwell — **without** downloading and aggregating
the full devices payload client-side on every refresh.

Derived entirely from the most recent completed cycle (plus the trailing
24 h of trip events, anchored at that cycle's `snapshot_time`), so the
response only changes when a new cycle lands: it carries a cycle-keyed
weak ETag and `Cache-Control: public, max-age=600`.

**Query parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `res` | int | yes | H3 resolution: `8` (~750 m cells), `9` (~210 m), or `10` (~75 m). |

**Request:**
```http
GET /api/v1/h3/aggregates?res=9
```

**Response 200:**
```json
{
  "res": 9,
  "cycle_id": "8f3a2d10-1234-4abc-8def-0123456789ab",
  "snapshot_time": "2026-07-06T18:30:14+00:00",
  "cells": {
    "8928308280fffff": {
      "device_count": 14,
      "trips_started_24h": 31,
      "starts_per_hour_peak": 4,
      "avg_battery_percent": 62,
      "risk_share": 0.21,
      "avg_dwell_hours": 9.4
    }
    /* … ~1,800 occupied cells at res 9 … */
  }
}
```

**Response 503:** no completed cycle yet (cold start), `{ "detail": "no completed cycles yet" }`.

#### Per-cell field reference

| Field | Type | Description |
|---|---|---|
| `device_count` | int | Devices (`denver_core` only) currently parked in the cell. |
| `trips_started_24h` | int | Successful starts whose **from**-position falls in this cell in the trailing 24 h ending at `snapshot_time`. A "start" is the state tracker observing a device leave its spot between consecutive cycles — the same movement event that resets dwell. Failed starts are a separate signal (`number_failed_starts` on the devices endpoint). |
| `starts_per_hour_peak` | int | Max starts in any single UTC clock hour within that 24 h window — usage heat. |
| `avg_battery_percent` | int \| null | Mean `battery_percent` of the cell's parked devices that have one; `null` when none do. |
| `risk_share` | float \| null | Fraction (2 decimals) of the cell's parked devices with `reliability_tier == "high_risk"` — same formula as the devices endpoint, dwell outliers included. `null` for trip-only cells with `device_count` 0. |
| `avg_dwell_hours` | float \| null | Mean dwell (1 decimal) of the cell's state-tracked devices; `null` when none are tracked. |

#### Notes

- **Cell keys are canonical h3 strings** (e.g. `"8928308280fffff"`), never
  raw integers — the 64-bit ints exceed JS `MAX_SAFE_INTEGER` and lose
  precision in `JSON.parse`. `h3-js` accepts them directly
  (`h3.cellToBoundary(key)` → polygon ring for rendering).
- A cell appears if it has **either** parked devices or trip starts;
  fully-empty cells are omitted. Zero-fill client-side if you need dense
  coverage.
- Rough sizes: res 9 ≈ 1.8k occupied cells ≈ tens of KB before
  compression; res 10 is a few× that, res 8 a few× less.
- Poll like the devices endpoint: `If-None-Match` with the returned ETag;
  a new cycle (~every 10 min) is the only thing that changes the payload.

---

### `GET /api/v1/leaderboard/map`

FEATURE_PLAN §11's H3 r8 "area leader" report -- the 🏆 Leaderboard
view's choropleth **and** click-through detail in one fetch. Reads the
daily recompute (`src/area_leaders.py`, `sql/048_h3_r8_area_leaders.sql`;
trailing 28 days, `status='confirmed'` ledger rows only, tie-break
`points DESC, first_point_at ASC, account_id ASC`), but privacy is
applied **live**, at read time, against `accounts` -- never baked into
the stored report. An account with `show_in_leaderboards=false`,
`show_public_username=false`, or a `NULL` `display_name` (an
unbackfilled username, sql/025) is skipped and the next stored rank
(1..3) falls through into `leader`; `runners_up` is whatever eligible
ranks remain. This takes effect on the very next request -- an opted-out
rider is never shown again, not just from tomorrow's run.

**Request:**
```http
GET /api/v1/leaderboard/map
```

**Response 200:**
```json
{
  "computed_at": "2026-07-29T09:15:00+00:00",
  "window_start": "2026-07-01T09:15:00+00:00",
  "window_end": "2026-07-29T09:15:00+00:00",
  "cells": {
    "8828308281fffff": {
      "total_points": 144,
      "distinct_earners": 4,
      "leader": {
        "display_name": "Duke Swift 🦦",
        "points": 88,
        "ruling_color": "#7c54cd",
        "ruling_border_color": "#382264",
        "ruling_alpha": 0.6
      },
      "runners_up": [
        { "display_name": "...", "points": 30,
          "ruling_color": null, "ruling_border_color": null, "ruling_alpha": null }
      ]
    },
    "8828308283fffff": {
      "total_points": 0, "distinct_earners": 0, "leader": null, "runners_up": []
    }
    /* ... ~720 cells ... */
  }
}
```

**There is no 503.** This endpoint is computed live from the points
ledger; the stored version used to fail outright before the first nightly
recompute, and this one answers from the ledger alone.

#### Notes

- **Computed at read time** (sql/061). `computed_at` is when you asked,
  and `window_start`/`window_end` are the trailing 28 days ending then --
  not a nightly run's window. Points earned a minute ago are in the
  payload.
- **The universe is the one precomputed part.** `h3_r8_area_report` lists
  every r8 cell that has ever had an observed device, all-time -- those
  are the unclaimed cells drawn as bare outlines, and "a device has been
  seen here" is not a fact about the trailing window.
  `src/area_leaders.py:refresh_universe` refreshes it **weekly**. The
  endpoint unions it with whatever has points in the window, so a cell
  that earned its first point since the last refresh still renders
  immediately.
- **Cell keys are canonical h3 strings** (`h3.int_to_str`), never raw
  64-bit integers -- same JS `MAX_SAFE_INTEGER` reason as
  [`/api/v1/h3/aggregates`](#get-apiv1h3aggregatesres8910).
- `total_points`/`distinct_earners` are **not** privacy-filtered -- they
  are aggregate ledger facts with no identity attached. A cell can show
  real nonzero totals with `leader: null` when every earner there opted
  out.
- `leader` + `runners_up` together are at most 3 entries, and can be
  fewer (including both empty/null). No `royalty_title` field:
  `display_name` already composes it (sql/044's generated column).
- A rider with no claimed ruling-color pair leads with
  `ruling_color: null` -- the API never invents a default; that's a
  frontend decision. `ruling_alpha` is nulled alongside an unclaimed
  pair (it otherwise carries a non-null `0.60` schema default that would
  leak as a meaningless fill opacity).
- **ETag is content-only** -- `W/"arealb:<sha256(cells)[:16]>"` over a
  canonical (`sort_keys=True`) serialization. There is no run id to key
  on any more, and `computed_at` moves every request, so keying on it
  would make every tag unique and defeat the 304 entirely. Any
  eligibility/color/name/points change busts a client's cached copy
  immediately. `Cache-Control: public, max-age=30`, down from 600 --
  that freshness is the point of the change.

---

### `GET /api/v1/leaderboard/regional`

The whole-database companion to `/api/v1/leaderboard/map` -- added per an
explicit product clarification: the per-cell report already answers "for
each hexagon where points were earned, who leads it" (no additional
spatial scoping needed there), and this endpoint answers the separate
question "across the whole database, who is ranked highest." Same
trailing-28-day window as the per-cell report -- not split by cell, top
`MAX_REGIONAL_LEADERS` (25) accounts by summed confirmed points.
Same read-time privacy filtering as the per-cell endpoint
(`show_in_leaderboards`, `show_public_username`, non-null
`display_name`), except an ineligible entry is simply dropped
rather than backfilled from a runner-up pool -- there is no larger stored
pool beyond the top 25 to fall through into -- so the returned list can
be shorter than 25, and surviving entries are renumbered to a contiguous
`rank` starting at 1.

**Request:**
```http
GET /api/v1/leaderboard/regional
```

**Response 200:**
```json
{
  "computed_at": "2026-07-29T09:15:00+00:00",
  "window_start": "2026-07-01T09:15:00+00:00",
  "window_end": "2026-07-29T09:15:00+00:00",
  "leaders": [
    { "rank": 1, "display_name": "Duke Swift 🦦", "points": 312,
      "ruling_color": "#7c54cd", "ruling_border_color": "#382264", "ruling_alpha": 0.6 },
    { "rank": 2, "display_name": "...", "points": 210,
      "ruling_color": null, "ruling_border_color": null, "ruling_alpha": null }
  ]
}
```

**There is no 503** -- same reason as the map: this is computed from the
ledger, not read out of a table a nightly job fills.

Computed live (sql/061) over the same trailing 28 days, the same
`confirmed`-only rule and the same tie-break as the map, just grouped by
account instead of by `(cell, account)`. The depth cap is applied to
**eligible** entries rather than in SQL: filtering happens after the
aggregate, so a pre-truncated set would return fewer than 25 whenever a
top earner had opted out.

`Cache-Control: public, max-age=30`; the weak ETag is content-only
(`W/"arealb:<sha256(leaders)[:16]>"`), for the same reason as the map's.

---

### `GET /api/v1/equity-estimate?ranks=1,2`

Device share inside a **candidate equity-rank cutoff** — the combined
`er1`–`er6` tiers you select — from the most recent snapshot. This is a
server-side stand-in for the client's 8k-point point-in-polygon pass:
weak clients can render a "% of fleet in ranks ≤ 2" gauge from a
sub-kilobyte response instead of geometry math over the full devices
payload.

The `erN` tiers [partition the scored area](#notes-on-the-layers) (a
device is in at most one), so combining ranks is a plain sum of the
per-tier fields `/api/v1/snapshots/latest` already carries — this
endpoint just does the arithmetic where the bandwidth isn't paid for.

**Query parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `ranks` | string | yes | Comma-separated `EquityGroupRank` tiers to combine, each in 1..6. Duplicates deduped, order irrelevant: `ranks=1,2` ≡ `ranks=2,1`. |

**Request:**
```http
GET /api/v1/equity-estimate?ranks=1,2
```

**Response 200:**
```json
{
  "cycle_id": "8f3a2d10-1234-4abc-8def-0123456789ab",
  "snapshot_time": "2026-07-06T18:30:14+00:00",
  "ranks": [1, 2],
  "total_devices": 2054,
  "total_bikes": 1339,
  "total_scooters": 715,
  "percent_all_devices": 24.71,
  "percent_all_bikes": 26.02,
  "percent_all_scooters": 22.63
}
```

- Percentages are against the citywide denominators
  (`total_devices_denver` etc.), rounded to 2 decimals, `null` when the
  denominator is 0 — the same conventions as `/api/v1/snapshots/latest`.
- **Response 400:** malformed `ranks` (non-integer, empty, or outside 1..6).
- **Response 503:** no snapshot yet.
- Carries a weak ETag keyed on `(cycle_id, ranks)` and
  `Cache-Control: public, max-age=60`.
- Reminder: no `erN` combination is a compliance boundary. This endpoint
  was built to preview candidate cutoffs while the city had not yet said
  which map binds; it has, so `percent_all_devices_equity` is the metric
  and this is now a historical comparison tool (see
  API_REQUIREMENTS.md §1.1a).

---

### `GET /api/v1/compliance/daily/latest`

Most recent daily 6 AM – 9 AM Denver SLA window. **This is the contractually-correct compliance metric per License Exhibit B** — the every-10-min `/snapshots/latest` value is informational, but the binding SLA is the morning-window daily average. Computed once per day at 9:00 AM Denver time.

> **Which field is the answer:** `avg_percent_all_devices_equity` / `compliance_equity_pass`, measured against the city's official Equity Area map (`equity`). The city clarified that map in August 2026; before then this documentation pointed at `..._v1`, which is now retained history. The `_v1`, `_v2` and `_erN` families are all still computed and still returned, so a dashboard built against the old field keeps working — it is just no longer reporting the number the contract turns on.
>
> **Days that predate the map return `null` there.** The `*_equity` columns did not exist when those snapshots were recorded, so they are backfilled by a nightly job that reconstructs each cycle's fleet from `device_history` (`python -m src.cli reprocess_equity_compliance`). A `null` means "not reprocessed yet", not "zero" — treat it as pending, the way `/api/v1/compliance/calendar` does.

**Request:**
```http
GET /api/v1/compliance/daily/latest
```

**Response 200:**
```json
{
  "sla_date": "2026-05-30",
  "window_start_ts": "2026-05-30T12:00:00+00:00",
  "window_end_ts": "2026-05-30T15:00:00+00:00",
  "snapshot_count": 18,
  "avg_total_devices_denver": 5874.39,
  "avg_total_devices_v1": 1281.50,
  "avg_total_devices_v2": 944.22,
  "avg_total_bike_denver": 4011.06,
  "avg_total_bike_v1": 847.94,
  "avg_total_bike_v2": 599.83,
  "avg_total_scooter_denver": 1863.33,
  "avg_total_scooter_v1": 433.56,
  "avg_total_scooter_v2": 344.39,
  "avg_total_not_in_denver": 11.78,
  "avg_percent_all_devices_v1": 21.82,
  "avg_percent_all_devices_v2": 16.07,
  "avg_percent_all_bikes_v1": 21.14,
  "avg_percent_all_bikes_v2": 14.95,
  "avg_percent_all_scooters_v1": 23.27,
  "avg_percent_all_scooters_v2": 18.48,
  "avg_percent_bikes_denver": 68.29,
  "avg_percent_scooters_denver": 31.71,
  "avg_percent_bikes_v1": 66.16,
  "avg_percent_scooters_v1": 33.84,
  "avg_percent_bikes_v2": 63.52,
  "avg_percent_scooters_v2": 36.48,
  "avg_total_devices_equity": 1802.11,
  "avg_total_bike_equity": 1204.90,
  "avg_total_scooter_equity": 597.21,
  "avg_percent_all_devices_equity": 30.68,
  "avg_percent_all_bikes_equity": 30.04,
  "avg_percent_all_scooters_equity": 32.05,
  "avg_percent_bikes_equity": 66.86,
  "avg_percent_scooters_equity": 33.14,
  "compliance_equity_pass": true,
  "compliance_v1_pass": false,
  "compliance_v2_pass": false,
  "avg_total_devices_er1": 1189.44,
  "avg_total_bike_er1": 776.61,
  "avg_total_scooter_er1": 412.83,
  "avg_percent_all_devices_er1": 20.24,
  "avg_percent_all_bikes_er1": 19.36,
  "avg_percent_all_scooters_er1": 22.15,
  "avg_percent_bikes_er1": 65.30,
  "avg_percent_scooters_er1": 34.70,
  /* … the same 8 avg_* fields, for er2 … er6 … */
  "computed_at": "2026-05-30T15:00:08+00:00"
}
```

**Response 200 (pending):** No daily row computed yet (first run pending, or pipeline just deployed). Returns the same shape with every field nulled and `snapshot_count: 0`, so the gauge can render a "pending" state without special-casing a non-2xx status. The `avg_*` and `compliance_*` fields are `null` (not absent), matching the field reference below.
```json
{ "sla_date": null, "window_start_ts": null, "window_end_ts": null, "snapshot_count": 0, "avg_percent_all_devices_equity": null, "avg_percent_all_devices_v1": null, /* … all other avg_* fields null, including er1..er6 … */ "compliance_equity_pass": null, "compliance_v1_pass": null, "compliance_v2_pass": null, "computed_at": null }
```

#### Field reference

| Field | Type | Description |
|---|---|---|
| `sla_date` | string (date) \| null | Denver-local date the window covers (YYYY-MM-DD). `null` in the pending response. |
| `window_start_ts` | string \| null | 6:00 AM Denver expressed as UTC. `null` in the pending response. |
| `window_end_ts` | string \| null | 9:00 AM Denver expressed as UTC. `null` in the pending response. |
| `snapshot_count` | int | Number of cycles whose `snapshot_time` fell inside the window. Typically 18 (3 hours × 6 cycles/hour). Lower values indicate cycle misses; 0 means no data. |
| `avg_*` fields | float \| null | Arithmetic mean of the corresponding `snapshot_metadata_core` field across all snapshots in the window, **for every tracked group** (`v1`, `v2`, `er1`–`er6` — see [Tracked equity groups](#tracked-equity-groups-v1-v2-er1er6)). Null when `snapshot_count == 0`. |
| `compliance_equity_pass` | bool \| null | `avg_percent_all_devices_equity >= 30`, against the official map. **The SLA boolean.** Null when no data — including on days that predate the map and have not been reprocessed yet. |
| `compliance_v1_pass` | bool \| null | `avg_percent_all_devices_v1 >= 30`. Was the primary SLA boolean; retained as history. Null when no data. |
| `compliance_v2_pass` | bool \| null | Same for v2. The contractually-binding map (v1 vs v2) is being confirmed with DOTI; track both for now. |
| `computed_at` | string \| null | UTC timestamp of when this row was computed. `null` in the pending response. |

**No `compliance_erN_pass` fields.** No individual equity-rank tier is
itself a compliance boundary, so there's nothing to store a pass/fail
flag for. Combine whichever `avg_percent_all_devices_erN` values make up
a candidate cutoff (e.g. `er1 + er2` for a "rank ≤ 2" reading) and
compute pass/fail client-side.

---

### `GET /api/v1/compliance/daily?date=YYYY-MM-DD`

The daily SLA window for a specific Denver-local date. Useful for history/playback.

**Request:**
```http
GET /api/v1/compliance/daily?date=2026-05-30
```

Returns the same shape as `/api/v1/compliance/daily/latest`. Returns `404` if no row exists for that date (either no data was collected, or backfill hasn't been run).

---

### `GET /api/v1/compliance/daily/range?start=YYYY-MM-DD&end=YYYY-MM-DD&limit=N`

A range of daily SLA windows, ascending by date. Use for compliance dashboards (rolling 30-day, monthly, etc.).

**Query parameters:**

| Name | Type | Required | Default |
|---|---|---|---|
| `start` | YYYY-MM-DD | yes | — |
| `end` | YYYY-MM-DD | no | today |
| `limit` | int | no | 366 (max 1000) |

**Request:**
```http
GET /api/v1/compliance/daily/range?start=2026-05-16&end=2026-05-30
```

**Response 200:**
```json
{
  "start": "2026-05-16",
  "end": "2026-05-30",
  "count": 15,
  "rows": [
    { "sla_date": "2026-05-16", "snapshot_count": 11, "avg_percent_all_devices_v1": 19.84, "compliance_v1_pass": false, /* … all other fields … */ },
    { "sla_date": "2026-05-17", "snapshot_count": 18, "avg_percent_all_devices_v1": 20.71, "compliance_v1_pass": false, /* … */ },
    /* … */
  ]
}
```

Days without any computed row are simply omitted from `rows` — don't expect dense coverage immediately after deploy or during pipeline outages. `/api/v1/compliance/calendar` below is the dense-by-construction alternative when you need a cell per day.

---

### `GET /api/v1/compliance/calendar?month=YYYY-MM&count=N&group=equity`

Per-day compliance pass/fail for whole calendar months. Built for a calendar grid: unlike `/range`, it returns **every day of every requested month**, including days with no data and days that haven't happened yet.

That density is the point. "The job never computed this day" and "this day failed" are different facts, and a sparse response renders them identically — as a gap in the grid. So each day carries an explicit `status`:

| `status` | Meaning |
|---|---|
| `pass` | The 6–9 AM window average met the threshold |
| `fail` | It did not |
| `no_data` | No `daily_sla_compliance` row for that day at all |
| `pending` | A row exists, but this group's average is `null`. For `equity` that means the day predates the official map and the reprocessing job hasn't reached it yet — **not** a failure |

**Query parameters:**

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `month` | YYYY-MM | no | current Denver month | The **newest** month returned |
| `count` | int | no | `2` (max 12) | Walks **backwards** from `month`, so the default is "this month and last" |
| `group` | string | no | `equity` | One of `equity`, `v1`, `v2`. `equity` is the official map; v1/v2 are the pre-clarification series |

**Request:**
```http
GET /api/v1/compliance/calendar
```

**Response 200:**
```json
{
  "group": "equity",
  "threshold": 30.0,
  "today": "2026-08-21",
  "months": [
    {
      "month": "2026-07",
      "first_date": "2026-07-01",
      "last_date": "2026-07-31",
      "pass_days": 4,
      "fail_days": 27,
      "days": [
        { "date": "2026-07-01", "status": "fail", "percent": 19.84, "snapshot_count": 88, "in_future": false },
        { "date": "2026-07-02", "status": "pass", "percent": 31.02, "snapshot_count": 90, "in_future": false }
        /* … one entry per day of the month … */
      ]
    },
    { "month": "2026-08", "…": "…" }
  ]
}
```

Months come back **oldest first**, so the array reads in calendar order. `in_future` is computed server-side against Denver's clock — the client's may not be in Denver.

An unknown `group` is a `400`, not a fallback: the value reaches a column name, so it's checked against the compliance-group registry rather than trusted.

---

## Routing

Bicycle routing over a Denver-clipped Valhalla graph, with an empirical
battery-burn estimate attached. Public, no auth.

> **⚠️ Navigation directions are in beta — and clients MUST say so.**
> Routes and turn-by-turn cues can be inaccurate or unsafe. Every `/route`
> and `/route/profiles` response carries a `beta_warning` string; render it
> (or an equivalent warning) anywhere directions are shown to a rider, so
> nobody follows a cue into traffic thinking it's authoritative. Riders
> should use their own judgment and obey posted signs, signals, and traffic
> laws. The field disappears from the payload when directions leave beta —
> don't hardcode the text.

**The routing graph is smaller than the map.** It covers
`[-105.06, 39.65, -104.88, 39.79]` (west, south, east, north) — narrower
than both the app's map bounds and the audit's `denver_core` envelope.
Coordinates outside it are **rejected, not clamped**: a silently relocated
origin would produce a confidently wrong distance and battery estimate.
Expect `out_of_coverage` in normal use near the edges and handle it.

The graph also **excludes High Injury Network streets**, so a location
whose only nearby road is on the HIN yields `no_route_from_location`
rather than a route.

### `GET /api/v1/route`

| Param | Required | Description |
|---|---|---|
| `from` | yes | Origin as `lat,lon` (e.g. `39.7392,-104.9876`). |
| `to` | yes | Destination as `lat,lon`. |
| `profile` | no | `safe` \| `range` \| `shade` \| `express`. Defaults to `safe`. |
| `vehicle_model` | no | `Astro` \| `Cosmo` \| `Apollo` \| `Rover` — selects a model-specific battery curve. A model with no fitted curve yet (or any unrecognized value) falls back to the fleet-wide estimate rather than erroring. |
| `explain` | no | `true` adds a `diagnostics` block. |
| `maneuvers` | no | `true` adds `properties.maneuvers` — turn-by-turn cues for the nav HUD. |

**Response 200** — a GeoJSON `Feature`, so it drops straight into a map
source:

```json
{
  "type": "Feature",
  "geometry": { "type": "LineString", "coordinates": [[-104.9876, 39.7392] /* … */] },
  "properties": {
    "profile": "safe",
    "label": "Safe & Protected",
    "distance_meters": 2412.0,
    "duration_seconds": 611.0,
    "elevation_gain_meters": 18.4,
    "shade_score": null,
    "battery_percent_estimate": 7.2,
    "battery_model": "regression",
    "graph_bbox": [-105.06, 39.65, -104.88, 39.79],
    "beta_warning": "Navigation directions are in beta and may be inaccurate or unsafe. Use your own judgment, watch the road, and obey posted signs, signals, and traffic laws."
  }
}
```

| Field | Type | Description |
|---|---|---|
| `distance_meters` | number \| null | Route length. `null` if Valhalla omitted it. |
| `duration_seconds` | number | Valhalla's estimate for the selected costing. |
| `elevation_gain_meters` | number | Cumulative climb — the dominant term in battery burn. |
| `shade_score` | number \| null | Tree-canopy coverage of the chosen route. Only computed for `profile=shade` (or any profile when `explain=true`). `null` when canopy data is unavailable. |
| `battery_percent_estimate` | number \| null | Estimated battery burn for this route, in percentage points. **`null` whenever `battery_model` is `"unavailable"`.** |
| `battery_model` | `"regression"` \| `"unavailable"` | Whether a fitted model produced the number. Only these two values. |
| `graph_bbox` | `[w, s, e, n]` | Echoed on every response so clients can pre-filter without a second call. |
| `beta_warning` | string | Present on **every** response while directions are in beta. Show it to the rider wherever directions are rendered — see the warning at the top of this section. |
| `maneuvers` | array | **Only present when `maneuvers=true`.** Turn-by-turn cues; shape detailed below. |

> **The battery estimate is currently always `null` in production.** The
> regression is fitted from accumulated battery-trip observations, and no
> model has been fitted yet — so `battery_model` reads `"unavailable"`
> (with `reason: "no_model"` under `explain=true`) on every request,
> including when `vehicle_model` is supplied. Clients must treat the
> estimate as optional and render the route without it. It will start
> returning numbers once enough observations accumulate; nothing on the
> client needs to change when that happens.

**`profile=shade` is guaranteed never to do worse than the default.** It
requests alternates, scores each against the canopy data, and includes the
default profile's route as a candidate — because shade's own costing
generates a different route family, re-ranking only within it once
measured *less* canopy than not asking for shade at all.

**`maneuvers=true`** adds the turn-by-turn cues the nav HUD needs:

```json
"maneuvers": [
  { "instruction": "Turn right onto Champa Street", "type": 10,
    "street_names": ["Champa Street"], "length_meters": 412.0,
    "time_seconds": 96.0, "begin_shape_index": 14, "end_shape_index": 22 }
]
```

| Field | Type | Description |
|---|---|---|
| `instruction` | string | Valhalla's written instruction. |
| `type` | number | Valhalla's maneuver-type enum, passed through unchanged. |
| `street_names` | string[] | Always an array — empty for an unnamed way. |
| `length_meters` | number \| null | Converted from Valhalla's kilometres. `null` if omitted upstream. |
| `time_seconds` | number \| null | `null` if omitted upstream. |
| `begin_shape_index` / `end_shape_index` | number | Indices into **this response's** `geometry.coordinates`. |

The shape indices are **re-offset for you**. Valhalla numbers them per leg,
while `geometry` is one concatenated LineString that drops a duplicated
leg-boundary vertex wherever one exists — so raw per-leg indices would
misplace every cue on a multi-waypoint route. Cues belonging to a leg that
contributed no geometry are omitted rather than pointed at the wrong
coordinate.

**Errors.** These carry a structured object as `detail`, not a bare string:

| Status | `error` | Meaning |
|---|---|---|
| 400 | `unknown_profile` | Unrecognized `profile`. Response lists valid `profiles`. |
| 400 | `out_of_coverage` | `from` or `to` outside the graph. Response includes `graph_bbox`. |
| 422 | `no_route_from_location` | No cycling-permitted road near one endpoint (often an HIN exclusion). |
| 422 | `no_route` | Both endpoints routable, but no path between them. |
| 503 | `router_unavailable` | The Valhalla sidecar is down or timed out. |
| 429 | — | More than 30 requests/minute from one IP (`/route`; 60/minute on `/route/profiles`). Carries `Retry-After` in seconds. Unlike the rows above, `detail` is a plain string. |

Malformed `from`/`to` (not two parseable floats) is a plain `400`. The rate
limit is checked **before** param validation, so a malformed request still
consumes quota.

### `GET /api/v1/route/profiles`

Advertises the selectable profiles so clients needn't hardcode them.
Public, no auth.

Rate limited to 60 requests/minute per IP (429 with `Retry-After`).

```json
{
  "default": "safe",
  "graph_bbox": [-105.06, 39.65, -104.88, 39.79],
  "beta_warning": "Navigation directions are in beta and may be inaccurate or unsafe. Use your own judgment, watch the road, and obey posted signs, signals, and traffic laws.",
  "profiles": [
    { "key": "safe",    "label": "Safe & Protected",    "shade_ranked": false },
    { "key": "range",   "label": "The Range Maximizer", "shade_ranked": false },
    { "key": "shade",   "label": "The Shaded Canopy",   "shade_ranked": true },
    { "key": "express", "label": "Commuter Express",    "shade_ranked": false }
  ]
}
```

Profiles are config-driven (`config.json` → `valhalla.profiles`); treat
this endpoint, not the table above, as the live list.

---

## Geocoding

Address search for the ride wizard, served by a **self-hosted Photon**
sidecar over a Colorado-scoped OpenStreetMap index. There is no third-party
geocoder in this path, no API key, and no rider query leaves the box.

### `GET /api/v1/geocode/search`

Public, no auth. Rate limited to **20 requests/minute per IP** (bucket
`geocode_ip`); a 429 carries `Retry-After`.

| Param | Required | Description |
|---|---|---|
| `q` | yes | Free-text query, 2–100 characters. Interior whitespace is collapsed. |
| `lat` | no | Bias latitude. Must be sent **with** `lon`. |
| `lon` | no | Bias longitude. Must be sent **with** `lat`. |
| `limit` | no | 1–8. Defaults to 6. |

Results are restricted to the Denver envelope (`config.json` →
`envelope.denver_core`), which is deliberately **wider** than the routing
graph — that is what makes `in_coverage` meaningful.

**Response 200**

```json
{
  "results": [
    { "label": "1701 Champa St, Denver", "lat": 39.747, "lon": -104.992,
      "kind": "house", "in_coverage": true }
  ]
}
```

| Field | Type | Description |
|---|---|---|
| `label` | string | One human-readable line, composed from the matched name/housenumber/street/city/state. Render as-is. |
| `lat` / `lon` | number | The point to route to, rounded to 6 dp. |
| `kind` | `"house"` \| `"street"` \| `"poi"` \| `"locality"` | What was matched. Only these four values. |
| `in_coverage` | boolean | Whether the point is inside the routing `graph_bbox`. **`false` means `GET /api/v1/route` will reject it with `out_of_coverage`** — grey the row out instead of letting the rider pick it and fail one screen later. |

Ordering is the geocoder's relevance ranking (biased by `lat`/`lon` when
supplied); do not re-sort client-side.

**Errors.** These carry a structured object as `detail`, not a bare string:

| Status | `error` | Meaning |
|---|---|---|
| 400 | `bad_bias` | Exactly one of `lat`/`lon` was supplied. Send both or neither. |
| 422 | `bad_query` | `q` is shorter than 2 or longer than 100 characters. (An out-of-range `limit` is FastAPI's own 422.) |
| 429 | — | Bucket full; `Retry-After` is seconds. |
| 503 | `geocoder_unavailable` | The sidecar is down, timed out (3 s), returned an error, **or** geocoding is disabled in config. Deliberately indistinguishable: degrade to a plain "type the address" field. Do not retry in a loop. |

Responses are cached in-process for 24 h (512 entries, keyed on the
normalized query plus the bias rounded to 2 dp), so a keystroke-debounced
field costs the sidecar nothing after the first hit. Cached responses still
count against the rate limit.

---

## Accounts & sessions

Three sign-in doors, one session model. Session-minting endpoints return
exactly `{ "token": "...", "expires": "<ISO 8601>" }`; store the token
and send it as `Authorization: Bearer <token>`. Tokens are opaque
(256-bit random) and stored server-side only as hashes.

The email doors come in two flavors: **magic link** (we email a link the
user clicks) and **typed code** (we email a short `AA000AA` code the user
types back — handy for signing in on the same tab/device without leaving
to an inbox app). Both mint the same kind of session.

**Scopes:** every session has `rider`. `admin` is a signal scope, granted
at sign-in for an allowlisted email through **any** door — but it gates
nothing: **admin authorization is membership in the admin allowlist**,
evaluated live per request, so an allowlist change applies immediately
rather than at the next sign-in. Read `admin` from
`GET /api/v1/auth/session` for that live answer; the scope is only a
snapshot of it. The
allowlist is stored in Postgres (`admin_allowlist` table) and managed from
the GitHub-gated admin portal at `/admin/admins` (or
`python -m src.cli admin add <email>`) — it replaced the `ADMIN_EMAILS`
env var.

**Expiry:** rider sessions last 30 days and slide — call
`POST /api/v1/auth/refresh` any time to rotate the token and get a fresh
30 days (the old token is revoked). Admin sessions last a fixed 24 h;
refresh rotates without extending.

**Discovering which doors are on:** call `GET /api/v1/auth/config` on
load. It's public (no auth) and returns
`{ "google_client_id": string | null, "google_enabled": bool, "magic_link_enabled": bool, "code_enabled": bool }`
— the source of truth for which doors to render (the Google button + the
client id to initialize Google Identity Services with, the magic-link
form, the typed-code form). The Google OAuth client id is **not** a
secret; it only names the audience and is meant to be embedded in the
browser. `*_enabled` mirror the `503`-when-unconfigured conditions on the
endpoints below (`magic_link_enabled` and `code_enabled` both track
Postmark config). Cached `public, max-age=300`.

| Endpoint | Body / notes |
|---|---|
| `GET /api/v1/auth/config` | Public. → `{ google_client_id, google_enabled, magic_link_enabled, code_enabled, sms_enabled }`. Render sign-in doors + init Google Identity Services from this. `google_enabled` is false when unconfigured **or** force-disabled via `GOOGLE_AUTH_ENABLED` (and `google_client_id` is null in that case). |
| `POST /api/v1/auth/google` | `{ "credential": "<Google ID token>" }` from Google Identity Services / One Tap. Verified locally (signature, audience, expiry, `email_verified`). → `{token, expires}`. `503` when unconfigured or force-disabled via `GOOGLE_AUTH_ENABLED`. |
| `POST /api/v1/auth/magic-link` | `{ "email": "you@example.com" }` → always `202 { "sent": true }` (no account-existence oracle). Emails a single-use link (15-min TTL). Limits: 3/hour per email, 10/hour per IP. `502` if the email provider fails, `503` if unconfigured. |
| `POST /api/v1/auth/redeem` | `{ "token": "<from the emailed link>" }` → `{token, expires}`. Single-use; `401` if invalid, expired, or already used. |
| `POST /api/v1/auth/code` | `{ "email": "you@example.com" }` → always `202 { "sent": true }`. Emails a short `AA000AA` code (10-min TTL). Only the newest code per email is live. Limits: 3/hour per email, 10/hour per IP. `502` if the email provider fails, `503` if unconfigured. |
| `POST /api/v1/auth/code/verify` | `{ "email": "you@example.com", "code": "AB123XY" }` → `{token, expires}`. Case-insensitive; spaces/hyphens ignored. `401` if the code is wrong, expired, already used, or after too many wrong tries (5 — which burns the code). Verify attempts are rate-limited 30/hour per IP. |
| `POST /api/v1/auth/sms/code` | `{ "phone_number": "(303) 555-1212" }` → `202 { "sent": true }`. Texts a short `AA000AA` code (10-min TTL) via z280-comms. US numbers only, any format. Limits: 3/hour per number, 5/hour per IP, 250/day globally — the daily ceiling is **skipped for a number already verified**, so a rider whose only door is SMS can't be locked out by other traffic. A failed send does **not** invalidate a code you already hold. `400` unusable number, `409` **the recipient has blocked texts — show `detail` verbatim, it names the keyword and number that unblock**, `429` over quota, `502` send failed, `503` if unconfigured. |
| `POST /api/v1/auth/sms/code/verify` | `{ "phone_number": "(303) 555-1212", "code": "AB123XY" }` → `{token, expires}`. Case-insensitive; spaces/hyphens ignored. Typing the code back is what marks the number **verified** — an account is created if none has proved that number yet. `401` wrong/expired/too many tries (5), `409` if the number is contested (needs an operator). Limits: 10/hour per number, 30/hour per IP. |
| `POST /api/v1/auth/refresh` | Bearer required. → `{token, expires}` (new token; old one revoked). |
| `GET /api/v1/auth/session` | Bearer required. → `{ email, scopes, admin, expires }`. `401` when invalid/expired — treat as signed out. **`admin` is the authorization answer** (`is_admin_email` — the allowlist check `/private/*` enforces) and is evaluated **live**, so an allowlist change applies on the next request without re-signing in. The `admin` *scope* is a mint-time snapshot of the same allowlist; both are agnostic to the sign-in door. Clients gating admin UI should read `admin`. |
| `POST /api/v1/auth/signout` | Bearer required. Revokes the token. → `{ "revoked": true }` |

### `GET /api/v1/profile` / `PUT /api/v1/profile`

Bearer required. GET returns:

```json
{
  "email": "you@example.com",
  "phone_number": "+13035550123",
  "public_username": "Brave 🦉",
  "show_public_username": true,
  "show_in_leaderboards": false,
  "rate_plan": "resident",
  "theme": null,
  "favorites": [],
  "home_lat": 39.7392,
  "home_lng": -104.9876,
  "work_lat": null,
  "work_lng": null,
  "royalty_title": "Queen",
  "display_name": "Queen Brave 🦉",
  "ruling_color": "#c53637",
  "ruling_border_color": "#026fd7",
  "ruling_alpha": 0.6,
  "badges": [ { "id": "first_report", "label": "Filed a report", "earned_at": "2026-07-01T18:00:00+00:00" } ]
}
```

`display_name` is **read-only and server-computed**: your `royalty_title`,
a space, then your `public_username` — or just the username when you have
no title. It is a generated column, so it cannot drift out of step with
either part; re-rolling your username changes it immediately. Unlike
`public_username` it is *not* unique — the title is decoration, and two
riders can both be Queen.

PUT accepts any subset of the client-writable fields — omitted fields are
untouched, `"theme": null` clears the theme:

| Field | Type | Notes |
|---|---|---|
| `rate_plan` | `"resident" \| "visitor" \| "equity"` | Drives the frontend cost ticker. |
| `theme` | string \| null | Free-form, ≤64 chars. |
| `favorites` | array | Opaque JSON, ≤100 entries — shape TBD by the frontend. |
| `email` | string \| null | ≤320 chars. Nullable — an account can be reachable by phone alone. |
| `phone_number` | string \| null | ≤32 chars, validated and normalized server-side. |
| `show_public_username` | bool | When false, your `public_username` is withheld from every public attribution (e.g. device photos) at **read** time, so flipping it applies retroactively and immediately. |
| `show_in_leaderboards` | bool | Opt into leaderboard listings. |
| `home_lat` / `home_lng` | number \| null | Set together. |
| `work_lat` / `work_lng` | number \| null | Set together. |

`public_username` and `badges` are server-computed and
ignored if sent — see the username endpoints below to change your
username.

**Profile completion awards 10 points, once.** Checked on every PUT.
Criteria are email **and** `rate_plan` **and** `phone_number` **and** at
least one of home/work coordinates. Note that `email` is nullable and
`rate_plan` defaults to `"visitor"`, so in practice this reduces to
"set a phone number and one location."

**Badge ids:** `first_report`, `reporter_10`, `ghost_hunter` (one of your
reports corroborated by a *different* reporter within 7 days),
`discount_watchdog`, `miles_10`, `miles_100`, `streak_7` (rides on 7
consecutive days). Badges are recomputed on every read, so
new thresholds apply retroactively.

The mileage and streak badges (`miles_10`, `miles_100`, `streak_7`) are
computed from [tracked rides](#tracked-rides-gbfs-detected) **and**
[off-feed rides](#off-feed-rides) together — every ride where you reported
an end. Which mechanism logged a ride is an implementation detail you
never chose, so both measure identically and both count. Every distance
source counts toward mileage, so a rider who uploads no waypoints still
earns badges; their miles just accrue more slowly, since a
`straight_line` distance undercounts. A ride with `distance_meters: null`
still counts toward `streak_7` (it happened) but adds nothing to mileage.

Tracked rides that ended before distance was recorded at all have been
backfilled with their start → end straight line, tagged `straight_line`.

**A ride nobody ended counts for nothing** — an off-feed ride that
[expired](#the-24-hour-window) after 24 hours, or a tracked ride whose
watch window elapsed. Both drop out for the same reason from both sides:
whatever their waypoints got to before the phone went quiet, no one ever
said the ride finished.

Because a one-shot `POST /api/v1/rides` counts the distance *you* assert,
that number is [checked for plausibility](#is-this-ride-possible) before
it is stored. The check is on the ride, not on the badge: it rejects
impossible rides at the door rather than discounting off-feed mileage
afterwards, so an honest off-feed rider's miles are worth exactly as much
as anyone else's.

### Public usernames

Every account gets a generated `public_username`: a curated adjective plus
an emoji-noun, presented as the capitalized adjective, a space, then the
emoji -- e.g. `Brave 🦉` (sql/060; `src/accounts.py:format_public_username`
is the one place Python knows that presentation, and it must match the
generated column character for character). Both halves are validated against
server-side lists — **never free text** — so usernames can be shown
publicly without moderation. Usernames are unique.

| Endpoint | Notes |
|---|---|
| `POST /api/v1/profile/username/regenerate` | Re-roll to a new random pair. → `{ "public_username": "..." }` |
| `PUT /api/v1/profile/username` | `{ "adjective"?: string, "emoji"?: string }` — partial: omit either half to keep your current one. `400` if you send neither, `400` if a value isn't on the curated list, `409` if the resulting username is taken. → `{ "public_username": "..." }` |
| `POST /api/v1/profile/phone/code` | Bearer required. `{ "phone_number"? }` — omit to use the number already on your profile. Texts an `AA000AA` code. Draws on the **same** send budget as the SMS sign-in door (one handset). Same `400`/`409`/`429`/`502`/`503` set as `/auth/sms/code`. |
| `POST /api/v1/profile/phone/verify` | Bearer required. `{ "phone_number", "code" }` → `{ phone_number, phone_verified: true }`. Attaches the proved number to **this** account — which is what stops SMS sign-in from creating a second account for a rider who listed their number in their profile. `409` if another account has already verified it. |

Both share **one** rate-limit bucket of 10/hour per account — they mutate
the same field, so a combined cap is what actually limits abuse.

### Username lexicon

The curated word lists, for building a username picker. Bearer required
(like every rider endpoint), though the content is static.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/emoji-nouns` | `{ "emoji_nouns": [ { "emoji": "🦉", "word": "owl" }, … ] }`, sorted by word. |
| `GET /api/v1/emoji-nouns/search?q=owl` | Same shape, case-insensitive substring match on the word. `q` is 1–64 chars. |
| `GET /api/v1/adjectives` | `{ "adjectives": ["brave", "bright", …] }`, sorted. |
| `GET /api/v1/adjectives/search?q=bra` | Same shape, case-insensitive substring match. |
| `GET /api/v1/royalty-titles` | `{ "royalty_titles": ["King", "Queen", "Monarch", …] }`, in picker order (related titles adjacent), not alphabetical. |
| `GET /api/v1/royalty-titles/search?q=high` | Same shape, case-insensitive substring match. |
| `GET /api/v1/ruling-colors` | The palette + claimed pairs — see below. |

### Ruling colours

Your territory on the leaderboard map is drawn with a **fill** and an
**inner border**, both chosen from a curated 128-colour palette, plus an
opacity you control.

```json
{
  "ruling_colors": [ { "hex": "#c53637", "name": "red-500", "hue_family": "red" }, … ],
  "taken_pairs": [ { "fill": "#c53637", "border": "#026fd7" } ]
}
```

Rules, all enforced by the database:

* **The (fill, border) PAIR is globally unique** — 128 × 127 = 16 256
  claims. You may share a fill with another rider, or share a border, but
  not both. Adjacent territories can therefore never render identically.
* **Fill and border must differ**, and are set **together** — send both,
  or send both as `null` to clear and release your claim.
* **`ruling_alpha` is 0.10–1.00** (default `0.60`) and applies to the
  **fill only**; the border always renders opaque. To leave the map
  entirely, set `show_in_leaderboards: false` rather than a low alpha.

`taken_pairs` lets a picker grey out unavailable combinations instead of
discovering them by `409` on save. It lists pairs only — never which
account holds one.

| Status | When |
|---|---|
| `400` | one-sided colour update, fill equal to border, or a value not on the curated list |
| `409` | that exact (fill, border) pair is already claimed |
| `422` | `ruling_alpha` outside 0.10–1.00 |

### Saved map settings & find-ride preference

Two rider-owned stores of **opaque JSON**. The API never reads inside the
blob, never merges it, and never validates its shape — `PUT` replaces
wholesale. Max 16 KB per blob; max 50 saved map settings per rider.

| Endpoint | Notes |
|---|---|
| `GET /api/v1/profile/map-settings` | `{ "map_settings": [ { "name": "commute", "settings": {…}, "created_at": …, "updated_at": … } ] }`, most recently updated first. |
| `GET /api/v1/profile/map-settings/{name}` | One setting. `404` if that name isn't yours. |
| `PUT /api/v1/profile/map-settings/{name}` | `{ "settings": { … } }` — creates or replaces. `409` at the 50-setting cap (you can still overwrite settings you already have), `413` over 16 KB. |
| `DELETE /api/v1/profile/map-settings/{name}` | `404` if absent. |
| `GET /api/v1/profile/find-ride-pref` | `{ "find_ride_pref": null }` until you set one. **`null` means never set** — distinct from an empty object you chose. |
| `PUT /api/v1/profile/find-ride-pref` | `{ "settings": { … } }` — at most one per rider; a second PUT replaces the first. |
| `DELETE /api/v1/profile/find-ride-pref` | Idempotent — deleting an absent preference returns `200`, since there is only one and "gone" is the state you asked for. |

Names are 1–64 characters and scoped to you: two riders can both have a
setting called `commute`.

### Ride Mode Usuals

A **Usual** is a saved answer to the ride wizard's options screen — the
frontend's `ride_options` object plus a display `label` — applied wholesale
from the Usuals picker (Screen 2.5). Same store as the saved map settings
above (`user_preferences`, kind `ride_mode_usual`, `sql/050`) and therefore
the same rules: **opaque JSON** the API never reads inside, `PUT` replaces
wholesale, names are 1–64 characters, max 16 KB per blob. Max **10 Usuals**
per rider.

| Endpoint | Notes |
|---|---|
| `GET /api/v1/profile/ride-usuals` | `{ "ride_usuals": [ { "name": "commute", "settings": {…}, "created_at": …, "updated_at": … } ] }`, most recently updated first. |
| `GET /api/v1/profile/ride-usuals/{name}` | One Usual. `404` if that name isn't yours. |
| `PUT /api/v1/profile/ride-usuals/{name}` | `{ "settings": { "label": "Morning commute", "ride_options": { … } } }` — creates or replaces. `409` at the 10-Usual cap (you can still overwrite Usuals you already have), `413` over 16 KB, `422` on a name longer than 64 characters. |
| `DELETE /api/v1/profile/ride-usuals/{name}` | `404` if absent. → `{ "deleted": true, "name": "commute" }` |

The `settings` blob is **not** validated here even though its shape is
known: `ride_options` is checked when it is used to start a ride
(`POST /api/v1/tracked-rides`), so a Usual saved by a newer client than the
API has heard of still round-trips. Usuals and saved map settings are
separate namespaces — the same rider may hold a map setting **and** a Usual
both called `commute`.

---

## My Scooters

Vehicles a rider has **kept** after proving at the kerb that they were
standing at one. Ten per account. Session-authed throughout; there is no
anonymous form, because there is no anonymous account to hang one on.

A favourite is **not a claim and not a reservation**. Nothing here stops
anybody riding anything — that is as true of My Scooters as it is of dibs.

### The gate

Keeping a vehicle needs **both**:

1. a `qr_raw_value` that validates for that `vehicle_identifier` — the same
   check `POST /api/v1/devices/qr-scan` makes; and
2. a fix within **75 m** of where the fleet last saw the vehicle.

The second is not redundant. The scan proves the caller **has the plate** — it
hashes the plate out of the payload and compares — and nothing in the scan
endpoint has ever compared the submitted position to anything. That is fine
for a points bonus and not fine for a feature whose premise is "you were
there". 75 m is the radius the in-app "Unlock in Veo" gate already uses, and
it is generous on purpose: a GBFS position is up to two minutes stale and
street-canyon GPS is routinely 20–30 m out, and those errors do not cancel.

**Your position is not stored.** The check runs at write time and the
coordinates are discarded.

**`vehicle_identifier` is optional — the scan is the identity.** The same rule
`POST /api/v1/reports/device-features` follows: a rider keeping a scooter from
the My Scooters panel has the camera open on one they never tapped on the map,
and the identifier is a salted hash the client cannot compute. Send it if you
have it; leave it out and the sticker decides.

When you *do* send it, it has to agree with the sticker — a mismatch is a
`400`, not a re-target. That differs from a features report on purpose: there,
the answers describe the scooter the rider was standing at, so the scan
outvotes the tap and the data is saved against the right vehicle. Here there
is nothing to salvage, and quietly keeping a different scooter would be the
API deciding which one somebody meant.

### The withholding

**A favourite's position is not returned while the vehicle is in a rental.**
Neither is its battery or its range — a charge falling five points every ten
minutes is a coarse track of the same journey.

Veo keeps rented vehicles in the feed for the whole rental, broadcasting a
live moving position, and `/api/v1/devices/current` publishes that today. So
the capability is already public. What a favourite would add is a **one-tap,
persistent, targeted** subscription to one vehicle somebody physically located
— which is the difference between a public dataset and a tool for following a
person. Parked position, yes. Moving position, no.

`position_withheld` is always present, and is `true` rather than the field
simply being missing, so a client cannot read absence as a bug and "fix" it
with a cached value.

| Endpoint | Notes |
|---|---|
| `GET /api/v1/profile/favorite-devices` | `{ "favorite_devices": [...], "max_favorites": 10 }`, newest first. |
| `POST /api/v1/profile/favorite-devices` | `{ "qr_raw_value", "lat", "lng", "vehicle_identifier"?, "nickname"? }` → `201`. Re-keeping one you already have is also `201`, with `already_favorited: true` and a refreshed `verified_at`. |
| `PATCH /api/v1/profile/favorite-devices/{vehicle_identifier}` | `{ "nickname"?, "notify_on_available"? }`. An empty-string nickname clears it; an absent one leaves it. No re-scan needed. |
| `DELETE /api/v1/profile/favorite-devices/{vehicle_identifier}` | `404` if it was not yours. → `{ "deleted": true, "vehicle_identifier": … }` |

One entry in the list:

```json
{
  "vehicle_identifier": "8c4a1f0d2e9b7a35",
  "nickname": "My Rover",
  "state": "available",
  "position_withheld": false,
  "notify_on_available": false,
  "verified_at": "2026-08-29T12:00:00+00:00",
  "created_at": "2026-08-20T09:14:00+00:00",
  "last_seen_at": "2026-08-29T11:58:00+00:00",
  "vehicle_model_name": "Cosmo",
  "vehicle_use_type": "sitting",
  "lat": 39.7392, "lon": -104.9903,
  "battery_percent": 71,
  "current_range_meters": 12000
}
```

`state` is one of:

| State | Meaning | Position? |
|---|---|---|
| `available` | In the feed, rentable | yes |
| `unavailable` | In the feed but out of service, or absent for under 12 hours | yes |
| `in_use` | Somebody is riding it (`is_reserved`, or a rental open in `device_state`) | **withheld** |
| `gone` | Not seen for 12 hours or more, or no longer known at all | **withheld** — the last position we have is stale enough that publishing it would be a claim we cannot make |

`in_use` is tested **before** out-of-service, unlike the map's own
`hideUnavailable`, which collapses the two. Both hide the vehicle, but only
one of them is a person riding, and "out of service" is the wrong thing to
tell a rider about a scooter somebody is on.

### Errors

| Code | `detail.error` | When |
|---|---|---|
| `400` | `qr_mismatch` | The payload does not hash to that vehicle |
| `400` | `unknown_device` | No vehicle with that identifier |
| `400` | `nothing_to_change` | A `PATCH` with neither field |
| `403` | `too_far_from_device` | Carries `meters_away` and `meters_allowed`. Also returned when we have **no** recent position for the vehicle — otherwise "we don't know where it is" would be the reliable way past the gate |
| `404` | `not_favorited` | `PATCH`/`DELETE` on one that is not yours |
| `409` | `favorite_limit_reached` | Carries `max_favorites`. Re-verifying one you already keep still works at the cap |

### Points

Keeping a vehicle runs the same `qr_scan` award path the scan endpoint does:
once per `(account, vehicle)`, advisory-locked. Keeping one you have never
scanned pays 100; keeping one you have pays nothing. There is no way to
double-pay by favouriting something you already scanned.

---

## Rider reports

### `POST /api/v1/reports/device`

Report a scooter that failed you. Anonymous is fine (3/hour per IP);
sending a bearer token links the report to your account (10/hour) and
weighs it double in the public aggregates.

```json
{ "vehicle_identifier": "8c4a1f0d2e9b7a35", "report_type": "not_rideable",
  "observed_at": "2026-07-04T16:20:00Z", "lat": 39.7392, "lng": -104.9876 }
```

`report_type`: `not_rideable` | `dead_battery` | `damaged` |
`improperly_parked` | `not_found`.

> **Deprecated alias.** `failed_unlock` is still accepted and is stored,
> deduped and scored as `not_rideable` — it was renamed because the
> rider-facing question is broader than "did the unlock work": could you
> ride it or not? Send `not_rideable`. The alias exists only so a frontend
> and backend that deploy at different times can't 422 each other's
> riders, and it will be removed once no client sends the old spelling.
> Nothing ever reads `failed_unlock` back out — exports, aggregates and
> the points ledger only ever show `not_rideable`.

`observed_at`,
`lat`, `lng` optional — without coordinates the report is anchored to the
scooter's last known cell. →
`{ "id": 17, "reported_at": "...", "deduped": false, "points_awarded": 10 }`.
An identical (vehicle, type, reporter) report within 30 minutes returns
the existing row with `"deduped": true` instead of creating a new one.

`not_found` — the scooter isn't where the map says it is — is the newest
type. Like `improperly_parked` it is a **non-reliability** signal in the
points table but, unlike it, `not_found` *does* feed
`has_negative_report` / `reliability_tier`: a scooter nobody can locate is
a scooter that failed the rider.

**Reports earn points** (see [Points](#points)): `not_rideable`,
`damaged`, and `improperly_parked` are 10 each, `not_found` is 4, and
`dead_battery` earns none. Points require **both** a bearer token and a
resolvable location — your `lat`/`lng`, or the scooter's last known H3
cell as a fallback. Anonymous reports are still accepted and still count
in the aggregates; they just return `"points_awarded": 0`.

Reports feed `has_negative_report` and `reliability_tier` on
`/api/v1/devices/current` for 24 h or until the scooter moves, whichever
comes first — **except `improperly_parked`**, which is a parking-compliance
signal, not a ride-quality one: it still counts in `/reports/summary` and
the monthly CSV export, but a badly-parked scooter can ride perfectly, so
it deliberately does **not** flip `has_negative_report` / `reliability_tier`.
(The frontend also opens Veo's public Zendesk "improperly parked" form
pre-filled when a rider files one.)

### `POST /api/v1/reports/device-features`

"I'm standing at this scooter — here's what's actually bolted to it."
Veo's feed says nothing about bells, cup holders, phone holders or baskets,
so the only way the map can ever be filtered on equipment is riders telling
us. Anonymous is fine (5/hour per IP); a bearer token links the report to
your account (40/hour) and is what makes it earn points.

```json
{ "vehicle_identifier": "8c4a1f0d2e9b7a35", "device_id": "abc123",
  "submitted_plate": "1025543",
  "has_bell": true, "has_cup_holder": false, "has_phone_holder": true,
  "has_basket": false,
  "all_good_condition": false, "poor_condition": ["bell"],
  "lat": 39.7392, "lng": -104.9876 }
```

`has_bell`, `has_cup_holder` and `has_phone_holder` are **required** — the
client's toggles start unpressed, but a half-answered survey is a `422`, not
a partial report.

**`has_basket` is optional, and omitting it is not a "no".** The question is
newer than the clients (sql/058), so a client that predates it sends nothing
and the report stores `NULL` — an *abstention*. An abstained feature is
excluded from that report's agreement check and from the consensus vote, so
an older client and a current one reporting the same scooter agree about the
three features they both asked about instead of flipping the vehicle into
`needs_review` over a question only one of them put to the rider. Send the
field if you ask the question; omit it if you don't. Do not send `false` for
a rider you never asked.

Ask about the basket on **every** model, not only the ones that usually
carry one: the Trike's cargo basket is standard equipment (and bends), the
Cosmo's is optional, and a confirmed "no" on an Astro is what makes an
equipment filter trustworthy rather than full of holes.

`poor_condition` names which of the features **this same report says are
present** are not in good condition. Two rules, both enforced with a
`422`:

* it may only name features you reported present (`["cup_holder"]` with
  `"has_cup_holder": false` is rejected — and an *abstained* feature counts
  as not present, so `["basket"]` with no `has_basket` is rejected too: a
  client that never asked about baskets cannot report a broken one);
* it must be non-empty exactly when `all_good_condition` is `false`. The
  two fields are one fact stated twice, and the server stores the list —
  so "something's wrong but I won't say what" has nowhere to live, and
  sending it is a client bug worth hearing about rather than something to
  silently normalise.

**The plate is the whole anti-abuse story.** You can't confirm a scooter's
features from your sofa, because you can't read the plate under its QR
code from there. A **wrong plate is still a `200`**: the report is stored
(a rash of near-miss plates is a real signal — riders mixing up two
scooters parked side by side), and the response says `"plate_valid":
false, "points_awarded": 0`. Nothing downstream ever reads it: the
consensus job skips invalid rows entirely. Matching ignores whitespace,
punctuation and case, so `#1025543` and `1025543` both match.

**Or scan the QR instead (sql/067).** `qr_raw_value` — the decoded payload
of the sticker on the deck, verbatim — stands in for the typed plate as
proof-of-presence, and with it both `submitted_plate` **and**
`vehicle_identifier` become optional (a report still needs at least one
identity and at least one proof; either gap is a `422`). The server
extracts the plate from the payload, hashes it, and:

* **resolved to a known vehicle** → the report attaches to *that* vehicle
  — even when it differs from the `vehicle_identifier` you sent, because
  the answers describe the scooter the rider actually scanned, not the dot
  they tapped. `plate_valid` is `true` by construction, the original claim
  is kept server-side for audit, and the response's `vehicle_identifier`
  tells you where the report landed so you can say so to the rider.
* **resolved to nothing** (damaged sticker, unrecognized payload shape) →
  falls back to your `vehicle_identifier` and the typed-plate rule; with
  no `vehicle_identifier` to fall back to, `404`.

The raw payload is logged on the report row either way, and a resolved
scan also refreshes the per-device QR registry.

→ `{ "id": 17, "reported_at": "...", "deduped": false, "plate_valid": true,
     "points_awarded": 14, "feature_status": "needs_review",
     "vehicle_identifier": "8c4a1f0d2e9b7a35", "qr_matched": null }`

`qr_matched` is `null` when no scan was sent, `true` when it resolved (and
validated the report), `false` when a scan was sent but didn't resolve.

`feature_status` in the response is the status the vehicle carried **when
the report landed** — which is what chose the award. The status *after*
isn't knowable until the grading job runs (see below), and promising a
status we haven't computed would be worse than a stale one. An identical
(vehicle, answers, reporter) report within 30 minutes returns the existing
row with `"deduped": true`.

**Points** (see [Points](#points)): **12** for the first confirmation of a
device nobody has done before, **14** for confirming one that is
`needs_review`, **6** for reconfirming one that is already `up_to_date`.
Requires a bearer token *and* a valid plate. One award per account per
vehicle per 24 h — a same-day second opinion still votes, it just doesn't
pay twice.

### `GET /api/v1/devices/{vehicle_identifier}/features`

Current consensus for one vehicle, so a client can render an
up-to-the-second status instead of whatever its 90-second map poll last
saw. Public; no plate appears in it.

→ `{ "vehicle_identifier": "...", "feature_status": "up_to_date",
     "features": { "bell": true, "cup_holder": false,
                   "phone_holder": true, "basket": false,
                   "poor_condition": ["bell"] },
     "confirmed_at": "...", "report_count": 3 }`

`features` is `null` — not an all-`false` object — until someone confirms
the vehicle. `false` would claim we know a scooter has no bell; `null`
says nobody has looked, which is the same thing
`"feature_status": "needs_features_confirmed"` says.

#### How a device's `feature_status` moves

Reports are graded by a cron job **every ten minutes, on the 8s**
(`:08, :18, …`), never inline on your request. So a device's status lags
its reports by up to ten minutes, by design: your POST writes one row and
returns, and its latency never depends on how many other people are
reporting the same scooter.

| From | On | To |
| --- | --- | --- |
| `needs_features_confirmed` | the first valid **full** report | `up_to_date` |
| `up_to_date` | a later report that disagrees | `needs_review` |
| `needs_review` | 3 valid reports since the flag, 2/3 majority | `up_to_date` |

**Reports come from two places.** The Confirm Features modal
(`POST /reports/device-features`, above) answers every question. The
[end-ride survey](#post-apiv1tracked-ridesride_idsurvey)'s Cosmo basket
question files a **basket-only** report — it abstains on the other three
features, so it can agree, disagree, or fill in on the basket alone and
can never flag a vehicle over a bell it said nothing about. A basket-only
report on a never-reported vehicle publishes its basket answer but leaves
the status `needs_features_confirmed` (that's the "full" in the first row):
three of four questions are still unasked, and moving on would tell every
client to stop asking them. Likewise a review resolved entirely by
basket-only reports settles the *basket* and returns a never-fully-reported
vehicle to `needs_features_confirmed` rather than claiming `up_to_date`. A
review never erases answers the vote had no opinion on — three basket votes
decide the basket, not the bell.

**The first valid report is authoritative** — not one vote among many.
Every later report is graded against it, and any disagreement (about
presence *or* condition) flags the vehicle. An **abstention is not a
disagreement**: a report that omits `has_basket` is silent about the
basket, not opposed to it, and is graded only on the features it actually
answered. If it turns out to answer a feature the stored consensus has no
opinion about — a basket, on a vehicle confirmed before sql/058 — that
answer is published on the spot, by the same "first answer is
authoritative" rule that handles a vehicle nobody has reported at all. Flagging does **not** overwrite
what we were publishing: one dissenting voice changes the label so more
people are asked, and only a three-way vote replaces the data.

The vote is **per field**, which is what makes "2/3 of what's correct"
reachable: three riders who each disagree about a different feature have no
majority *answer set* at all, but a clear 2/3 on every individual field.

### `POST /api/v1/reports/discount`

Missed equity-discount evidence. **Bearer required** (evidence needs
provenance), 20/day per account. Send JSON:

```json
{ "ride_ended_at": "2026-07-04T16:20:00Z", "zone_version": "v1",
  "end_lat": 39.71, "end_lng": -105.01, "amount_charged_cents": 450 }
```

…or `multipart/form-data` with the same field names plus an optional
`receipt` image part (JPEG/PNG/WebP, ≤10 MB). Receipts are re-encoded on
ingest — EXIF/GPS metadata is destroyed, not just hidden — stored in a
private bucket, and deleted after 18 months (see `/api/v1/meta/privacy`).
→ `{ "id": 3, "created_at": "...", "receipt_stored": true }`

The 20/day limit is applied **before** the receipt is processed or stored,
so a `429` never costs you an upload. Quota is consumed by the attempt,
not by the success.

### `POST /api/v1/reports/model`

"We're showing this as an unrecognized model — tell us what it actually
is." Feeds an operator review queue so the model catalog can be corrected.

**This is a catalog correction, not a failure report.** It never touches
`has_negative_report` / `reliability_tier`, and never appears in
`/reports/summary` or the CSV export. A scooter whose name we got wrong
still rides fine.

Send `multipart/form-data` (or `application/x-www-form-urlencoded` for a
text-only report). A JSON body is refused with `415` — it can't carry the
photo part, so accepting it would silently drop an attachment.

| Field | Required | Notes |
|---|---|---|
| `device_id` | yes | The per-cycle GBFS device id shown to the rider. |
| `description` | yes | ≤2000 chars. |
| `vehicle_identifier` | no | 16 lowercase hex, when the client has one. |
| `lat` / `lng` | no | **Both or neither** — half a pair locates nothing (`422`). |
| `photo` | no | Image ≤10 MB. **Requires a session** — see below. |

→ `{ "id": 12, "created_at": "…", "photo_stored": true }`

**Auth: text is anonymous, photos are not.** A signed-out caller may
submit a text report (5/hour per IP) — naming a scooter model isn't
evidence about a rider, and requiring sign-in would lose most of the
corrections. Attaching a `photo` while signed out returns **`401`**: we do
not accept binary uploads from unauthenticated callers, because IPs are
free and hosting whatever gets uploaded is not. Signed-in callers get
20/hour per account and may attach a photo.

Two things follow for signed-out callers, both enforced *before* the body
is parsed rather than after:

- The whole request body is capped at **64 KB** — plenty for a text
  report, far too small for a photo. Over that is a **`413`**, and we
  never buffer the upload to find out.
- The request must declare a `Content-Length`. A chunked signed-out
  request is refused with **`411`**, since there is nothing to check
  against. Signed-in callers are exempt from both.

The rate limit is applied **before** the photo is processed or stored, so
a `429` costs you nothing and burns none of our storage — and quota is
consumed by the attempt, not by the success.

Photos are re-encoded on ingest (EXIF/GPS destroyed, not hidden), stored
in the same private bucket as receipts, and **deleted after 18 months** by
the same daily job, on the same clock (see `/api/v1/meta/privacy`). The
report row outlives the image: the correction is the point, the photo is
supporting evidence. `503` if photo storage isn't configured on the
deployment — submit without the image.

What a model report stores, beyond the fields in the table above: the
free-text `description`, the coordinates if you sent them, and the IP
address and user-agent the request arrived with — kept for abuse detection
because this endpoint accepts anonymous submissions. None of it appears in
`/reports/summary` or the CSV export.

### `GET /api/v1/reports/summary?layer=<layer>`

Public per-region aggregate for the "Contract violations" choropleth and
the ticker. Same `layer` values as `/api/v1/spatial-snapshot`. Cached
~10 minutes (`Cache-Control: public, max-age=600`).

```json
{
  "layer": "neighborhood",
  "generated_at": "2026-07-04T16:30:00+00:00",
  "regions": {
    "NB_FivePoints": { "device_reports": 4, "discount_reports": 1, "est_overcharge_cents": 225 },
    "NB_CBD":        { "device_reports": 0, "discount_reports": 0, "est_overcharge_cents": 0 }
    /* … every region in the layer, zero-filled … */
  }
}
```

`device_reports` is a weighted count (authenticated ×2, anonymous ×1).
`est_overcharge_cents` assumes the missed discount is 50% of the charged
amount — an estimate, flagged as such until DOTI confirms the rate card.
Reports without coordinates aren't regionalizable and are excluded here
(they still appear in the CSV export).

### `GET /api/v1/reports/export/monthly.csv?month=YYYY-MM`

Public CSV of a month's reports for DOTI and journalists. No auth,
rate-limited (10/hour per IP). Columns never include reporter identity —
no IPs, no emails, just an `authenticated` boolean for evidentiary
weight.

---

## Private API (admin)

Bearer-token JSON endpoints for operators. Gated on `require_admin` —
the session's email must be in the `admin_allowlist` table, reachable
through **any** sign-in door, not a scope. **Distinct from the `/admin/*`
HTML portal**, which is a separate GitHub OAuth surface.

Documented here for completeness; ordinary frontend clients have no
reason to call these, and they return plate-level data the public API
deliberately withholds.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/private/devices/lookup?plate=&vehicle_identifier=` | Resolve a plate ↔ identifier either direction, plus the current state row. Supply exactly one param. |
| `GET /api/v1/private/devices/lookup-batch?plates=a,b,c` | Comma-separated plates → max observed range per plate. |
| `GET /api/v1/private/devices/{vehicle_identifier}/history?since=&until=&limit=` | Time-ordered position-stop history for one scooter. `since` defaults to 7 days ago, `until` to now, `limit` 1–10000 (default 2000). |
| `GET /api/v1/private/devices/max-ranges?form_factor=&limit=` | Devices sorted by highest-ever observed range. `limit` 1–20000 (default 5000). |
| `GET /api/v1/private/trips/daily?date=YYYY-MM-DD&limit=` | Daily trip/popularity rollup for one Denver-local date. `limit` 1–5000 (default 100). |
| `GET /api/v1/private/area-leaders` | Full, unfiltered §11 area-leader report: every stored rank 1-3 per cell with real account ids, points, and `first_point_at` tie-break provenance -- no privacy filtering (that layer belongs only to the public `/api/v1/leaderboard/map`). |
| `GET /api/v1/private/regional-leaders` | Full, unfiltered whole-database leaderboard: every earner with real account ids, points, and `first_point_at` tie-break provenance -- admin sibling of the public `/api/v1/leaderboard/regional`. Live, like its public sibling. |
| `GET /api/v1/private/analytics/daily?days=` | Per-day totals from the user-analytics rollup (`telemetry_daily`): events plus a max-per-event-name distinct-visitor/session figure. `days` 1–3650 (default 30). |
| `GET /api/v1/private/analytics/events?name=&days=` | One event name's daily rollup rows, including `prop_summary` (top-k prop-value counts). |
| `GET /api/v1/private/analytics/requests/daily?days=` | `request_metrics_daily` rows: per route/method/status-class request counts and p50/p95 latency. |
| `GET /api/v1/private/reports` | Admin listing of all negative reports. |
| `GET /api/v1/private/quality-feedback` | Admin listing of all quality feedback. |
| `GET /api/v1/private/admins` | The admin allowlist. → `{ count, admins: [ { email, added_by, added_at, is_you } ] }`, newest first. |
| `POST /api/v1/private/admins` | `{ "email": "..." }` → `{ count, admins, email, added }`. Idempotent: re-adding an existing admin is `200` with `added: false`. `400` if it isn't an email address. |
| `DELETE /api/v1/private/admins?email=...` | → `{ count, admins, email, removed }`. `409` if it would remove the **last** admin. Removing an address that isn't listed is `200` with `removed: false`. |

A non-allowlisted but validly signed-in caller gets `403`; no token at
all gets `401`.

### Managing the allowlist

The three `/admins` routes let an account admin grant and revoke account
admin. **This is a wider trust boundary than the `/admin/admins` HTML
portal**, whose GitHub OAuth gate is separate: there, a GitHub operator
decides who counts as an account admin, and an account admin cannot
promote anyone. Both surfaces edit the same `admin_allowlist` table, and
the portal remains the out-of-band way back in.

Three properties make that survivable, and clients can rely on them:

- **Every write is attributed.** `added_by` records the acting admin's
  email, so the table is an audit trail rather than a bare set.
- **The last admin cannot be removed** (`409`). An empty allowlist locks
  every account out of `/private/*` — including these routes — leaving
  only the GitHub portal or `python -m src.cli admin`. Removing
  *yourself* is allowed while others remain; stepping down is
  legitimate, locking the door on an empty room is not. The check and the
  delete are **one serialized transaction**, so two concurrent removals of
  different addresses cannot both pass a count of two and both commit —
  the second is refused.
- **Writes are rate-limited** 30/hour per account.

Both write routes return the **full refreshed list** alongside their
result, so a UI never needs a follow-up `GET` to redraw. `is_you` is
computed server-side against the same normalization the allowlist is
keyed by (`strip().lower()`), so a client never has to reimplement it to
know which row is dangerous to remove.

`DELETE` takes the address as a **query parameter**, not a path segment:
email addresses are full of characters a path segment handles badly —
dots, `+`, and an `@` that some proxies normalize.

Because `is_admin_email` is evaluated live per request, a change here
takes effect on the target's **next request** — no new sign-in, and a
revoked admin loses access immediately rather than at token expiry.

---

## Ride limits

Three hard caps apply to **every** ride, on both mechanisms
([off-feed](#off-feed-rides) and [tracked](#tracked-rides-gbfs-detected)).
They are set by the operator, not tuned from data, and they are not
negotiable per client:

| | Cap | Where it bites |
|---|---|---|
| **Points per ride** | **100**, total across every award for that ride | `PATCH .../end` credits less than the raw award when it would exceed this |
| **Distance between consecutive waypoints** | **3 000 m** | `POST .../waypoints` returns `422`; `PATCH .../end` drops the leg instead |
| **Total ride distance** | **80 000 m** | `POST .../waypoints` returns `422`; `POST /api/v1/rides` returns `422`; `PATCH .../end` clamps |

Both bounds are inclusive: a leg of exactly 3 000 m and a ride of exactly
80 000 m are fine.

### The rule that matters when you're building a client

**Reporting the end of a ride never fails because of these caps.** There
is no cap-related error on `PATCH .../end` — not for a 3 000 km final leg,
not for a ride that somehow arrives at 500 km. Ending is the one operation
that must always succeed, because you can only have one active ride at a
time and a refused end would leave you holding that slot until the ride
expires hours later.

So the caps are enforced **on the way in**, where the cost of a rejection
is one GPS fix:

- A fix more than 3 km from its neighbour on the path → `422`
  `waypoint_too_far`. The fix isn't stored. The ride is untouched and still
  active; send the next one normally.
- A fix that would push the ride past 80 km → `422`
  `ride_distance_cap_reached`. Same deal — end this ride and start another
  if you're still going.

Note "its neighbour", not "the last fix you sent": waypoints may arrive out
of order, so a late fix is checked against the waypoints on **both** sides of
where it lands in the path.

### What `/end` does instead of failing

| Situation | What is recorded |
|---|---|
| Final leg (last fix → your reported end) over 3 km | The leg is **excluded** from the distance and the end point is left out of the path. `distance_source` gains a `_partial` suffix. Your `end_lat`/`end_lon` are still stored — we keep your report, we just decline to measure a leg we don't believe. |
| Ride measures over 80 km | `distance` is recorded **at the cap** and `distance_clamped_from_m` carries what was actually measured. |
| Ride with **no** waypoints at all | The leg cap does **not** apply. `start → end` is the whole ride rather than a sampling gap, so a 40 km trackless ride records 40 km as `straight_line`. Only the 80 km cap bounds it. |

`distance_clamped_from_m` is `null` on the overwhelming majority of rides.
When it isn't, the distance you're showing is a ceiling, not a
measurement — worth saying so in the UI.

### Points

The 100-point ceiling is per **ride**, summed across every award
attributable to it — so a 600-waypoint ride earns 100, not 1 200. See the
[award table](#award-table).

`qr_scan` is **not** subject to this: it is a device scan rather than a
ride award, and is worth 100 on its own.

The cap is **forward-only**. Ledger entries written before it shipped are
not rewritten or clawed back — the points ledger is append-only and
records what riders were actually granted.

---

## Off-feed rides

Bearer required, open to any signed-in rider. Rides on vehicles the audit
**does not track** — a personal scooter, a competitor's rental, a friend's
e-bike. There is no `vehicle_identifier` by definition, so you describe the
vehicle instead.

Use [Tracked rides](#tracked-rides-gbfs-detected) when there IS a Veo
vehicle to anchor to; use this when there isn't.

**No points are awarded anywhere in this section**, deliberately. Points
reward data about the public fleet, and every fact here is rider-asserted
about a vehicle we cannot see or corroborate. Paying per waypoint for an
unverifiable ride would be an unbounded points faucet.

### Two ways in

| | |
|---|---|
| **Lifecycle** | `start` → `waypoints` → `end`, for a ride happening now. Distance is measured server-side from your track. |
| **One-shot** | A single `POST /api/v1/rides` for a ride already over, where your client computed everything. Distance is whatever you send. |

### Endpoints

| Endpoint | Notes |
|---|---|
| `POST /api/v1/rides/start` | `{start_lat, start_lon, vehicle_kind?, operator?, started_at?}` → the created ride, `status: "active"`. `409` if you already have an active off-feed ride. 20/hour. |
| `GET /api/v1/rides/active` | → `{ "active": <ride> }` or `{ "active": null }` — always wrapped. |
| `POST /api/v1/rides/{id}/waypoints` | `{waypoint_at, lat, lon, metadata?}`. Rebuilds the polyline and distance. 600/hour. `409 {"error": "ride_not_active"}` once ended. `422 {"error": "waypoint_too_far"}` / `{"error": "ride_distance_cap_reached"}` — see [Ride limits](#ride-limits). |
| `GET /api/v1/rides/{id}/waypoints?limit=&after=&before=` | → `{count, waypoints: [...]}`, oldest first. Paginate with `after` — see [below](#paginating-waypoints). |
| `PATCH /api/v1/rides/{id}/end` | `{ended_at, end_lat, end_lon, est_cost_cents?, rate_plan?, started_in_zone?, ended_in_zone?}` → the completed ride. `409` if already ended; `409 {"error": "ride_expired"}` once the [24-hour window](#the-24-hour-window) has passed. **Never fails on a distance cap** — see [Ride limits](#ride-limits). |
| `POST /api/v1/rides` | One-shot log of a finished ride (fields below). 120/day. `422` if the numbers aren't [plausible](#is-this-ride-possible), or if `distance_m` exceeds the 80 km [ride cap](#ride-limits). |
| `GET /api/v1/rides?limit=&before=&status=` | Owner-only, newest first. → `{count, rides: [...]}`. `status` is `active` \| `completed` \| `expired`. |
| `GET /api/v1/rides/export?format=geojson\|csv` | Owner-only full export. GeoJSON decodes each polyline to a `LineString`; see [Export geometry](#export-geometry). |
| `DELETE /api/v1/rides/{id}` | **Immediate hard delete**, cascading to the ride's waypoints. → `{"deleted": true}` |
| `DELETE /api/v1/rides` | **Immediate hard delete of everything you own.** → `{"deleted_count": n}` |

`started_at`, `ended_at`, and `waypoint_at` must all carry a UTC offset.

### Describing the vehicle

| Field | Type | Notes |
|---|---|---|
| `vehicle_kind` | `"scooter" \| "bicycle" \| "other"` \| null | Optional. |
| `operator` | string ≤64 \| null | Free text — `"Lime"`, `"Bird"`, `"personal"`, `"my Segway"`. Descriptive only, never a join key. |

`rate_plan` is **optional here**, unlike on tracked rides: `resident` /
`visitor` / `equity` describe Veo's pricing, which is meaningless on a Lime
scooter or your own bike.

### One-shot `POST /api/v1/rides` body

```json
{ "started_at": "2026-07-27T16:20:00Z", "ended_at": "2026-07-27T16:45:00Z",
  "duration_s": 1500, "distance_m": 2412, "est_cost_cents": 415,
  "rate_plan": "resident", "started_in_zone": true, "ended_in_zone": false,
  "polyline": "_p~iF~ps|U_ulLnnqC", "vehicle_kind": "scooter", "operator": "Lime" }
```

`polyline` is a Google encoded polyline (precision 5), validated at ingest
and required on this path — a one-shot log with no route is just a row of
numbers.

### Is this ride possible?

On this path — and only this path — you compute the distance and we store
what you send (`distance_source: "client"`). Those metres count toward the
mileage badges exactly like a ride we measured ourselves,
because a rider's mileage is the miles they rode and which mechanism
recorded a ride is an implementation detail they never chose. The cost of
that is that the number has to survive two sanity checks first. Both
return **`422`** with a machine-readable `error`; neither ever quietly
rewrites what you sent.

| Check | Bound | `error` |
|---|---|---|
| Ride-average speed | `distance_m / duration_s` ≤ **20 m/s** (72 km/h, 45 mph). A positive `distance_m` with `duration_s: 0` also fails. | `implausible_speed` |
| Distance vs. route | `distance_m` ≤ `min( max(route × 3, route + 1000 m), 80 000 m )`, where `route` is the decoded length of the `polyline` you sent. | `distance_exceeds_polyline` |

Above those sits the hard **80 km** [ride cap](#ride-limits), which
`distance_m` is validated against as a field bound — so a claim over
80 000 m is a `422` before either check runs. The two are not redundant:
the speed bound still catches 79 km covered in 90 seconds, which the
distance cap allows and no scooter did.

**Why 20 m/s.** It is not a speed limit — it is "no micromobility trip
*averages* this". Shared e-scooters are governed at ~24 km/h, a class-3
e-bike tops out at 45 km/h, and the UCI hour record is 15.7 m/s by a
professional on a track. The ceiling is deliberately ~3× the fastest thing
this endpoint is for, because it applies to a whole-ride average: a
downhill sprint, a stretch of GPS drift, or a leg where you put the bike
in a car must not cost you your log. If you are hitting it, check your
units — `distance_m` is metres and `duration_s` is seconds.

**Why the polyline comparison is one-sided.** An encoded polyline is a
*sampled* path, so its decoded length undercounts the real route by
however coarsely you sampled it — which is why the tolerance is 3×, and
why claiming **less** than the route supports is never rejected.
Undercounting isn't something anyone gains from, and a client reporting a
vehicle's own odometer is being honest, not evasive. The `+ 1000 m` floor
exists because the multiplicative rule collapses to nothing on a
degenerate polyline (two coincident points), which would otherwise reject
every very short ride.

If you rode a loop and your app only knows the two endpoints, the decoded
route is near zero and a long ride will be refused. Send the track you
actually rode: a distance is only believable next to the route it was
measured over.

The lifecycle path is unaffected — there the server measures the distance
from your uploaded fixes, so there is nothing to assert.

### Distance, and how much to trust it

Same two-field contract as tracked rides, plus a third source:

| `distance_source` | Meaning |
|---|---|
| `"waypoints"` | Measured along your **whole** path: start point → every fix you uploaded → the end you reported. Good. |
| `"waypoints_partial"` | Same, except at least one leg was over the 3 km [leg cap](#ride-limits) and was left out. A **lower bound over a path with a hole in it** — do not treat it as equivalent to `"waypoints"`. |
| `"straight_line"` | Start → your reported end, crow-flies. The fallback when a lifecycle ride ends with no waypoints at all. Undercounts. |
| `"client"` | Your app computed it and we stored what you sent. Unverifiable. |
| `null` | Ride still active with no waypoints yet. |

`distance_clamped_from_m` sits alongside these: `null` normally, and when
set, the distance we measured before clamping it to the 80 km cap.

**Ending a ride re-measures it.** The distance you see while a ride is
still active covers start → last fix, because that is all we know yet;
`PATCH .../end` recomputes over the same waypoints *plus your reported end*.
That final leg matters more than it sounds: a phone that backgrounds,
saves battery, or loses signal in a tunnel stops producing fixes long
before you stop riding, and a ride with one early fix is otherwise
recorded as a few metres. A ride that uploaded any waypoints keeps
`"waypoints"` and has its `polyline` re-encoded over exactly the waypoints
the distance was measured over, so the two can never disagree.

**The exception is a final leg over 3 km**, which is not measured — see
[Ride limits](#ride-limits). Such a ride records only the track it can
believe and reports `"waypoints_partial"`. If your users' rides are coming
back partial, the fix is to upload fixes more often: the leg cap is about
the gap between consecutive waypoints, not about how far anyone rode.

### Export geometry

A GeoJSON `LineString` needs at least two positions. A ride with no
waypoints has an empty `polyline`, so the export builds its geometry from
the ride's start and end coordinates instead. A ride with neither (an
active ride with no end yet) exports with `"geometry": null`, which is
valid GeoJSON — its properties still export. The export never emits an
empty `LineString`, which QGIS, GDAL and geojson.io all reject.

### Paginating waypoints

Waypoints come back oldest first, so the cursor that pages **forward** is
`after`: pass the `waypoint_at` of the last waypoint you received to get
the next page, and stop when `count` is 0.

`before` pages **backward** — the last `limit` waypoints *older* than the
cursor, still returned oldest-first. Passing both narrows to an open
interval and takes the newest rows in it.

Both cursors require an explicit UTC offset (a trailing `Z` is fine); a
naive timestamp is a `400`.

### Status

`active` → `completed`, or `active` → `expired`.

| Status | Meaning |
|---|---|
| `active` | Started, not yet ended. Accepts waypoints. |
| `completed` | You reported an end. Counts toward badges. |
| `expired` | Left active for 24 hours with no end report — see below. |

You may have only one `active` off-feed ride at a time, enforced by the
database. This is independent of tracked rides — the two mechanisms don't
know about each other.

### The 24-hour window

An active ride that is never ended is closed out automatically 24 hours
after it was created, and becomes `expired`.

This exists because "one active ride at a time" is enforced by a unique
index: without expiry, a rider whose phone died mid-ride is `409`'d out of
`POST /api/v1/rides/start` **forever**, and the only escape is `DELETE`,
which destroys the ride and its whole track. Expiry frees the slot instead
of trading it for data loss. The clock runs from when the server created
the ride, not from your `started_at` — you may backdate a start, so it
can't be what a lifetime is measured against.

What an expired ride looks like, which is exactly what an
[expired tracked ride](#statuses) looks like:

- `ended_at`, `duration_s`, `end_lat`, `end_lon` stay **`null`**. We never
  saw an end and won't invent one. It is an incomplete record, not a
  completed ride with a guessed ending.
- `distance_m` / `distance_source` keep whatever your uploaded waypoints
  already measured — start → your last fix. That number is real; it is
  just missing its final leg, exactly as it was while the ride was live.
- **It earns no mileage and feeds no streak.** The badges count rides
  someone ended. A ride nobody ended isn't evidence of a distance ridden,
  however far the waypoints got.
- The ride and every waypoint are **kept**. It appears in
  `GET /api/v1/rides` (filter with `?status=expired`), in the export, and
  is deleted only when you delete it.
- `GET /api/v1/rides/active` returns `null` and a new `start` succeeds.
- `PATCH .../end` returns `409 {"error": "ride_expired"}`. An end reported
  a day late would attach an invented duration to a ride nobody was on.

Privacy commitment, unchanged from when this table held the old ride log:
route polylines are the most sensitive data this system holds. There is no
soft-delete, no tombstone, and no analytics use of ride routes — ever.
Deleting a ride cascades to its waypoints. See `/api/v1/meta/privacy`.

---

## Tracked rides (GBFS-detected)

Bearer required, open to any signed-in rider. Use this when the vehicle is
a Veo scooter in the GBFS feed; use [Off-feed rides](#off-feed-rides) when
it isn't.

How it works: you declare a ride start against a specific vehicle. The
server adds that device to a watch list and compares it against **every
GBFS ingest cycle for 3 hours**, detecting when it is checked out (a ride
began) and when it becomes available again (it ended, and where). You
separately report your own end. The two accounts of the ride are then
comparable.

"Checked out" means the vehicle either drops out of the feed *or* stays
listed with `is_reserved` true — Veo does the latter, keeping a rented
vehicle in the feed and broadcasting its live position for the whole
rental. The field names still say `gbfs_left_feed_at` / `gbfs_reappeared_at`
(they are on the wire and in the schema); read them as "checked out at" and
"available again at". Nothing about the response shape changes.

### The redaction rule — read this before designing a summary screen

**Every `gbfs_*` field reads as `null` until you have reported your own
end.** That covers `gbfs_left_feed_at`, `gbfs_reappeared_at`,
`gbfs_end_lat`, `gbfs_end_lon`, and `gbfs_end_battery_percent`.

This is deliberate anti-fraud design: the rider must commit to their own
numbers before seeing the server's, so the server's observation can't be
copied. The underlying columns are untouched — only the API response is
redacted.

Practically, a ride detail has two distinct presentations: before the end
report (your data only) and after it (both, comparable). A summary screen
that assumes GBFS fields are populated at ride end will render nulls.

### Statuses

`watching` → `left_feed` → `completed`, or `expired`.

| Status | Meaning |
|---|---|
| `watching` | Ride started; the device is still visible in the GBFS feed. |
| `left_feed` | The device disappeared from the feed — consistent with a ride in progress. |
| `completed` | You reported your end. Set by `PATCH …/end`, regardless of what GBFS saw. |
| `expired` | The 3-hour watch window elapsed without an end report. |

A ride counts as **active** while it has no end report, no GBFS
reappearance, and an unexpired watch window. You may have only one at a
time.

### `POST /api/v1/tracked-rides`

```json
{ "vehicle_identifier": "8c4a1f0d2e9b7a35", "start_lat": 39.7392, "start_lon": -104.9876,
  "reported_start_battery_percent": 87.5,
  "ride_options": { "cost_hud": true, "speedometer": "digital", "theme": "auto",
                    "navigation": true, "save_tracks": true, "battery_modeling": true,
                    "nav_improvement": true, "end_survey": true, "own_device": false } }
```

`vehicle_identifier` is exactly 16 lowercase hex chars. → the created
ride (shape below). Limit 20/hour per account.

`reported_start_battery_percent` (optional, 0–100) is what you read off the
vehicle's own display. The server independently derives
`feed_start_battery_percent` and a feed-anchored start position from the
vehicle's newest fresh GBFS observation; those are stored, not returned.

`ride_options` (optional) is a **client-owned** object: stored and handed
back verbatim, with the server reading only the flags it gates on
(`save_tracks` gates track donation; `battery_modeling`, `nav_improvement`
and `end_survey` gate their awards). Keys this version does not know are
accepted and stored untouched, so the client can add options without
waiting on an API deploy. The listed keys are type-checked: the nine above
are booleans except `speedometer` (`classic|digital|none`) and `theme`
(`light|dark|auto`). Cap 4 KB, measured on the serialized JSON.

- `404` — unknown `vehicle_identifier`.
- `409` — `"an active ride already exists"`. Resolve by ending or
  deleting the existing ride; `GET /api/v1/tracked-rides/active` tells
  you which. (Concurrent starts are serialized server-side, so exactly
  one of two simultaneous requests wins.)
- `413` — `ride_options` is larger than the 4 KB limit.
- `422` — `{"error": "bad_ride_options", "detail": "..."}`: a known option
  carried the wrong type or a value outside its list.

The start response additionally carries **`plate_display_code`** — a
short cosmetic code for the vehicle, so the rider can confirm on-screen
that they're tracking the scooter in front of them. It is a display aid,
**not** a privacy control and not the plate itself.

### Ride object

```json
{
  "id": "3f2a…-uuid",
  "status": "watching",
  "started_at": "2026-07-27T16:20:00+00:00",
  "start_lat": 39.7392,
  "start_lon": -104.9876,
  "watch_expires_at": "2026-07-27T19:20:00+00:00",
  "gbfs_left_feed_at": null,
  "gbfs_reappeared_at": null,
  "gbfs_end_lat": null,
  "gbfs_end_lon": null,
  "gbfs_end_battery_percent": null,
  "user_reported_ended_at": null,
  "end_lat": null,
  "end_lon": null,
  "reported_battery_percent": null,
  "total_cost_cents": null,
  "metadata": {},
  "vehicle_identifier": "8c4a1f0d2e9b7a35",
  "created_at": "2026-07-27T16:20:00+00:00",
  "updated_at": "2026-07-27T16:20:00+00:00",
  "distance_meters": null,
  "distance_source": null,
  "reported_minutes": null,
  "reported_plan": null,
  "ride_options": {},
  "validation": { "status": "pending", "reasons": [] },
  "track_signing": { "alg": "HS256", "key_id": "3f2a…-uuid",
                     "key": "<base64url 32 bytes>",
                     "nonce": "<32 hex chars>",
                     "issued_at": "2026-07-27T16:20:00+00:00" },
  "path_polyline": null,
  "path_geojson": null
}
```

`path_polyline` is a Google encoded polyline (precision 5) rebuilt from
your waypoints on each append; `path_geojson` is the same path already
decoded to a `LineString` so clients needn't decode it. Both are `null`
until the first waypoint lands. List responses omit both.

`reported_minutes` and `reported_plan` are what you told us at
`PATCH .../end`: the duration the operator's app showed you, and the
rate-plan tier you rode under. Both are stored **as reported** —
`reported_minutes` is deliberately never reconciled against
`user_reported_ended_at - started_at`, because a reported field exists
precisely so it can differ from what we observed. `reported_plan` reuses
the `resident|visitor|equity` vocabulary from your profile's `rate_plan`,
and may legitimately differ from it on any given ride.

`ride_options` is the client-owned options object you passed at start,
echoed back verbatim (`{}` on a ride that sent none).

`validation` is the ride's contribution eligibility: `status` is one of
`pending` (nothing decided), `pending_feed` (waiting on the live feed to
show where the vehicle reappeared), `eligible`, `ineligible` (with
`reasons` from a fixed vocabulary — e.g. `tracking_not_opted`) or `error`.
`PATCH .../end` sets a provisional status; it is finalised later.

`track_signing` is the **per-ride** HMAC-SHA256 key and nonce for signing
locally recorded track batches, with `key_id` = the ride id. It is
**owner-only and returned by three responses only**: the start call,
`GET .../active` and `GET .../{id}` — so a client that reloaded mid-ride
can resume signing the same chain. It is **never** present in the list
response. It is `null` on rides that predate the feature. Treat the key as
a secret: anyone holding it can mint batches this ride will accept.

### Distance, and how much to trust it

`distance_meters` is the ridden distance, and `distance_source` says how
it was measured. **Always read them together** — the two sources are not
equally good:

| `distance_source` | Meaning |
|---|---|
| `"waypoints"` | Measured along your **whole** path: the ride's start point → every GPS fix you uploaded → the end you reported. Good. Still a slight undercount: sampling measures each curve as a chord. |
| `"waypoints_partial"` | Same, except at least one leg was over the 3 km [leg cap](#ride-limits) and was left out. A lower bound over a path with a hole in it — not equivalent to `"waypoints"`. |
| `"straight_line"` | Start → your reported end, as the crow flies. This is the fallback when you uploaded **no** waypoints at all, and it undercounts badly on any route that isn't a straight line. |
| `null` | Not computed yet — the ride hasn't ended and has no waypoints. |

`distance_clamped_from_m` accompanies these: `null` normally, and when set,
what was measured before the 80 km [ride cap](#ride-limits) clamped it.
Like `distance_meters` it is derived from your own data, so it is **not**
part of the `gbfs_*` redaction.

Distance appears as soon as your first waypoint lands and updates on every
append, so an in-progress ride shows a live figure — covering start → your
last fix, because that is all we know yet.

**Reporting your end re-measures the ride**, adding the leg from your last
fix to where you actually parked. Both end legs are real distance and both
are measured: dropping the first would undercount every ride by the
opening sampling gap, and dropping the last is worse, because a phone that
backgrounds, saves battery or loses signal in a tunnel stops producing
fixes long before you stop riding — a ride with one early fix would
otherwise be recorded as a few metres. `path_polyline` is re-encoded over
exactly the waypoints the final distance was measured over, so path and
distance can never disagree.

A ride that uploaded no waypoints keeps `path_polyline: null` rather than
gaining a two-point line we never observed; only its distance falls back
to `straight_line`.

Unlike the `gbfs_*` fields, distance is **not** redacted before you report
your end: it is derived entirely from your own waypoints and your own
reported end, so it reveals nothing you didn't tell us.

If you show the number to riders, a `straight_line` ride is worth marking
as an estimate — and it's the honest place to explain that uploading
waypoints makes it accurate.

### Endpoints

| Endpoint | Notes |
|---|---|
| `GET /api/v1/tracked-rides?limit=&before=&status=` | Owner-only, newest first. → `{ count, rides: [...] }`. `before` is an ISO timestamp and **must carry a timezone**. Never carries `track_signing` — the key is not read here at all. |
| `GET /api/v1/tracked-rides/active` | → `{ "active": <ride> }`, or `{ "active": null }` when there is none — always wrapped. Call on load to restore a ride that survived a reload: this response carries `track_signing`, so signing resumes on the same chain. |
| `GET /api/v1/tracked-rides/{id}` | Full detail incl. `path_geojson` and `track_signing`. `404` if it isn't yours. |
| `PATCH /api/v1/tracked-rides/{id}/end` | See below. |
| `POST /api/v1/tracked-rides/{id}/track` | Bulk track donation + verification. See below. |
| `POST /api/v1/tracked-rides/{id}/waypoints` | **Deprecated** — see below. |
| `GET /api/v1/tracked-rides/{id}/waypoints?limit=&after=&before=` | → `{ count, waypoints: [ { id, waypoint_at, lat, lon, metadata, created_at } ] }`, oldest first. Page **forward** with `after` (the `waypoint_at` of the last waypoint you received) and backward with `before` (the last `limit` waypoints older than the cursor). Both cursors need an explicit UTC offset. Identical contract to `GET /api/v1/rides/{id}/waypoints`. |
| `DELETE /api/v1/tracked-rides/{id}` | **Immediate hard delete**, cascades to waypoints, the watch list, and (if not yet de-identified) any track donation. → `{ "deleted": true }` |
| `DELETE /api/v1/tracked-rides` | **Immediate hard delete of every tracked ride you own.** → `{ "deleted_count": n }` |

Privacy commitment, stated here on purpose: route polylines are the most
sensitive data this system holds. There is no soft-delete, no tombstone,
and no analytics use of ride routes — ever. See `/api/v1/meta/privacy`.

### `PATCH /api/v1/tracked-rides/{id}/end`

```json
{ "ended_at": "2026-07-27T16:45:00Z", "end_lat": 39.7501, "end_lon": -104.9990,
  "reported_battery_percent": 62.5, "total_cost_cents": 415, "metadata": {},
  "reported_minutes": 24, "reported_plan": "resident" }
```

`ended_at`, `end_lat`, `end_lon` required; `ended_at` **must include a UTC
offset** (`400` otherwise). Sets `status` to `completed` and un-redacts
the `gbfs_*` fields. → the full ride object.

`reported_minutes` (0–1440, i.e. capped at 24 h) and `reported_plan`
(`resident|visitor|equity`) are optional and inert — nothing in the
close-out logic reads them; see [Ride object](#ride-object). This call also
sets a provisional `validation.status`. It does **not** return
`track_signing`: the ride is over, and the three owner-only reads above are
where a client that still needs the key finds it.

**Single-shot** — a second call returns `409 "this ride's end has already
been reported"`. There is no un-end and no edit. Confirm before sending.

It also never fails on a distance cap: an implausible final leg is dropped
and an over-cap distance is clamped, but the ride always completes.

**No longer credits points**, as of the ride-mode overhaul: the old
`waypoint` (2/waypoint) and `gbfs_trip_validated` (20, GBFS reappearance
within 20 m) awards are **superseded**. GBFS reappearance is now an
*eligibility gate* feeding `validation.status`, not an award in itself;
the reshaped ride-mode awards (`battery_contribution`, `nav_distance_bonus`,
etc. — see [Award table](#award-table)) are credited from
`POST .../track` and `POST .../survey` instead. `waypoint` and
`gbfs_trip_validated` remain valid historical actions on old ledger rows —
history is never rewritten — they simply stop being newly granted.

### `POST /api/v1/tracked-rides/{id}/track`

Bulk track donation: uploads the locally-recorded, hash-chained,
HMAC-signed waypoint chain for server verification, in **one shot** — this
is the *only* way a track ever reaches the server; ride mode sends nothing
mid-ride. Owner-only. Limit **6/hour per account**. Body cap **2 MB**; at
most **600 batches** per request.

```json
{ "batches": ["<compact JWS>", "<compact JWS>", "..."] }
```

Each string is one HS256-signed batch, sealed client-side at ≤25 waypoints
or ≤60 s. Signing key/nonce come from `track_signing` (above); see
`RIDE_MODE_OVERHAUL_PLAN.md` Part 2 for the exact wire shape and hash
chain. Raw batches are verified once and then **discarded** — only the
verification summary and the decoded waypoints persist.

→

```json
{
  "donation_id": "9c1e…-uuid",
  "verification": { "chain": "ok", "monotonic": "ok", "speed": "ok",
                    "gbfs_start": "ok", "gbfs_end": "ok", "volume": "ok" },
  "validation": { "status": "eligible", "reasons": [] },
  "distance_meters": 4312.5, "waypoint_count": 512,
  "points": [ { "action": "battery_contribution", "points": 14 } ]
}
```

`verification` shows each of the six server-side checks (signature/chain
integrity share one `chain` key). `validation.status` is one of `eligible`,
`ineligible` (with `reasons` — `start_mismatch`, `end_mismatch`,
`too_few_waypoints`, `trip_too_short`, `chain_invalid`, `internal_error`),
or `pending_feed` — GBFS hasn't yet told us where the vehicle reappeared,
so eligibility can't be decided yet. A `pending_feed` donation is still
**accepted** (it counts as your one donation for this ride) with its
distance-dependent points **held**; an hourly/per-cycle background job
settles it to `eligible`/`ineligible` once the feed resolves or the watch
window elapses, with no action from you. `points` lists only what was
actually credited — empty on anything but an immediate `eligible` outcome
(and even then, empty if a fast-segment ratio flagged the donation for
review).

Preconditions, in order:

- `404` — no such ride (or not yours).
- `409` `{"error": "ride_not_ended"}` — report your end first (`PATCH
  .../end`).
- `409` `{"error": "already_donated"}` — one donation per ride, ever.
- `422` `{"error": "tracking_not_opted"}` — this ride's `ride_options.save_tracks`
  was off, so there was never anything to donate.
- `422` `{"error": "chain_invalid", "failing_check": "chain", "batch_seq": n}`
  — the chain failed signature or integrity verification (bad signature,
  wrong ride binding, or a broken hash chain). The submission is rejected
  outright — nothing is stored and the donation slot is **not** consumed,
  so a client that hit a genuine upload bug can retry.
- `413` — over the 2 MB body cap, or over 600 batches.
- `429` — over 6/hour.

Award gates: `battery_contribution` requires `ride_options.battery_modeling`,
not an own-device ride, and both a start and end battery percent known.
`nav_distance_bonus` requires `ride_options.nav_improvement` and a stored
route (`POST /api/v1/ride-routes`) — see [Points](#points).

### `POST /api/v1/tracked-rides/{id}/waypoints`

**Deprecated.** Superseded by `POST .../track` above — ride mode never
streams a track mid-ride. Retained one release purely as caution for any
unknown external caller; it earns **no points** as of the ride-mode
overhaul (the per-waypoint award always lived at `PATCH .../end`, which no
longer grants it — see above). It is not the transport for ride-mode
tracks and should not be used by new clients.

```json
{ "waypoint_at": "2026-07-27T16:31:02Z", "lat": 39.7450, "lon": -104.9910, "metadata": {} }
```

`waypoint_at` must include a UTC offset. → the created waypoint. Appending
recomputes the ride's `path_polyline`.

Limit **600/hour per account** — roughly one fix every 6 seconds
sustained. Buffer and flush on an interval rather than posting every GPS
fix.

- `404` — no such ride (or not yours).
- `409` `{"error": "ride_not_active"}` — the ride has ended, been detected
  as reappeared, or its watch window expired.
- `422` `{"error": "waypoint_too_far"}` — this fix is more than 3 km from
  its neighbour on the path. Not stored; the ride carries on.
- `422` `{"error": "ride_distance_cap_reached"}` — this fix would push the
  ride past 80 km. Not stored; end the ride and start another.

Both are described in [Ride limits](#ride-limits) and behave identically on
off-feed rides.

### Ride transaction screenshots

Evidence of what Veo actually charged, for the cost audit. Stored in a
**private** bucket, re-encoded on ingest (EXIF/GPS destroyed, not hidden).

| Endpoint | Notes |
|---|---|
| `POST /api/v1/tracked-rides/{id}/screenshots?screenshot_type=overview\|receipt` | `multipart/form-data`. ≤10 MB. Each type is **one slot per ride** — re-uploading replaces it and deletes the old object. → `{ id, ride_id, screenshot_type, created_at, updated_at, replaced_previous }`. 20/hour per account. |
| `GET /api/v1/tracked-rides/{id}/screenshots` | Owner-only. → `{ ride_id, screenshots: [ { id, screenshot_type, url, created_at, updated_at } ] }`. `url` is a **presigned, expiring** link — fetch it fresh, don't persist it. |

### End-of-ride survey

Screen 9's post-ride feedback — the scooter-condition pane and the
navigation-feedback pane, submitted together as one request. Owner-only,
single-shot: a ride can be surveyed once (`ride_surveys.tracked_ride_id`
is `UNIQUE`, sql/052). Source of three point awards — see
[Points](#points).

### `POST /api/v1/tracked-rides/{id}/survey`

```json
{
  "would_ride_again": true,
  "was_perfect": false,
  "issues": ["battery", "brakes"],
  "model_bonus": { "apollo_top_speed_mph": 18.5 },
  "nav_route_rating": 8,
  "nav_deviated": false,
  "nav_deviated_needs_improvement": null,
  "nav_nps": 9,
  "nav_qualitative": "The route through the park was great, thanks!",
  "ride_route_id": "…-uuid|null"
}
```

→ echoes every field back plus:

```json
{
  "id": "…-uuid", "ride_id": "…-uuid", "vehicle_model": "Apollo",
  "created_at": "...",
  "points": [ { "action": "ride_survey", "points": 4 },
              { "action": "nav_route_feedback", "points": 4 },
              { "action": "nav_qualitative_feedback", "points": 6 } ]
}
```

Every field is optional — submit only the panes you have answers for.
`vehicle_model` is stamped **server-side** from
`device_state.current_vehicle_model_name` for the ride's vehicle
(`Astro`/`Cosmo`/`Apollo`/`Rover`, or `null` if unconfirmed) — never the
client's own claim. Only the first three have `model_bonus` keys today; a
Rover ride's survey simply has none to send.

`issues` is validated against a fixed 16-item vocabulary: `app_veo`,
`acceleration`, `basket`, `battery`, `bell`, `brakes`, `connectivity`,
`customer_service`, `dirty`, `kickstand`, `pedals`, `phone_holder`,
`price`, `speedometer`, `scooterfyi_issue`, `vandalized`.

`model_bonus` keys are validated against the ride's stamped
`vehicle_model`: `cosmo_front_basket` (bool, Cosmo only),
`apollo_top_speed_mph` (number 0–40, Apollo only),
`astro_landscape_holder` (bool, Astro only). A key present for the wrong
model — or when the model is unconfirmed — `422`s, same as an
unrecognized key.

**`cosmo_front_basket` also feeds the map's crowdsourced
[device features](#post-apiv1reportsdevice-features).** The answer is
filed as a basket-only feature report (no plate needed — the ride itself
proves the rider was on that exact vehicle) and folded into the vehicle's
consensus by the same ten-minute processor as a modal confirmation. It
abstains on the three features the survey never asks about, so it can
never conflict with the stored consensus except over an **opposite basket
answer** (or basket condition — a survey listing `basket` among `issues`
while confirming the basket exists reports it present-but-poor). It earns
no `device_features_*` points — the `ride_survey` award already pays for
this answer.

`ride_route_id`, when set, must name a `POST /api/v1/ride-routes` row you
own that is either unlinked or already linked to this ride; submitting
the survey is what stamps the link (Screen 4 runs before Screen 6 start,
so the row predates the ride it's about). A row linked to a *different*
ride, or one that doesn't resolve to your account at all (including a
de-identified route — its account link is already gone by the 28h
sweep), `422`s the same way: a stale id and a guessed one are
indistinguishable on purpose, so both fail identically.

Award gates (amounts in [Points](#points)):

- `ride_survey` (4) — any scooter-feedback field present
  (`would_ride_again`, `was_perfect`, `issues`, or `model_bonus`),
  `ride_options.end_survey` was on for this ride, and it isn't an
  own-device ride (defensive — an own-device ride never has a
  `tracked_rides` row to survey in the first place).
- `nav_route_feedback` (4) — `nav_route_rating` present and
  `ride_route_id` resolves.
- `nav_qualitative_feedback` (6) — `nav_qualitative`, trimmed, is at
  least 20 characters. "Meaningful" isn't machine-checkable; length is
  the whole check.

- `404` — no such ride (or not yours).
- `409` `{"error": "ride_not_ended"}` — report your end first (`PATCH
  .../end`) before surveying it.
- `409` `{"error": "survey_already_submitted"}` — one survey per ride,
  ever.
- `422` `{"error": "bad_issue", "detail": "..."}` — an `issues` entry
  outside the 16-item vocabulary.
- `422` `{"error": "bad_model_bonus", "detail": "..."}` — an unrecognized
  key, a key for the wrong (or unconfirmed) vehicle model, or a value
  outside that key's type/bounds.
- `422` `{"error": "bad_ride_route_id", "detail": "..."}` —
  `ride_route_id` doesn't resolve to a route you own, or is already
  linked to a different ride.

### `POST /api/v1/route-feedback`

The navigation half of the survey, for rides the survey can't reach: a "My
own Device" or guest ride is private — no `tracked_rides` row, no ride id —
but the rider still chose a route and rode it, and their opinion of the
routing is exactly as real. Same navigation vocabulary as the survey, with
the route described inline (there is no `ride_routes` row to link):

```json
{ "route_profile": "shade", "distance_m": 3200.5, "duration_s": 840,
  "nav_route_rating": 8, "nav_deviated": true,
  "nav_deviated_needs_improvement": true, "nav_nps": 9,
  "nav_qualitative": "Great until the staircase." }
```

`route_profile` is required; everything else is optional, but at least one
actual answer (rating, deviation, NPS, or non-empty qualitative text) must
be present — a bare profile name is a `422`. The deviation follow-up is
silently dropped unless `nav_deviated` is `true`, mirroring the pane that
only asks it after a Yes.

Anonymous is allowed (5/hour per IP; 20/hour with a bearer token) and
**nothing is awarded either way** — private rides are never
points-eligible, and this endpoint requires no proof a ride happened.

→ `{ "id": 17, "created_at": "..." }`

## Ride routes

Screen 4's chosen route (one of `safe`/`range`/`shade`/`express`, from
`GET /api/v1/route/profiles`), stored so the end-of-ride survey above can
rate the leg it names and `nav_distance_bonus` can confirm a route exists
for the ride. The client calls this **only** when
`ride_options.nav_improvement` is on.

### `POST /api/v1/ride-routes`

```json
{
  "tracked_ride_id": null,
  "profile": "safe",
  "origin": [39.7450, -104.9910],
  "destination": [39.7020, -104.9550],
  "route_polyline": "<precision-5 encoded polyline>",
  "distance_meters": 2450.0,
  "duration_seconds": 620.0,
  "battery_percent_estimate": 4.5
}
```

→ `{ "ride_route_id": "…-uuid" }`

`tracked_ride_id` is `null` in the normal wizard flow — Screen 4 precedes
ride start, and the survey links the row to a ride later. When non-null
(the New-Destination loop re-running Screen 4 mid-ride), it must resolve
to a ride **you own**, else `404` — same no-existence-oracle rule as
every other tracked-rides sub-resource. No uniqueness on
`tracked_ride_id`: a ride can accumulate more than one stored route (one
per deliberate Screen-4 selection); `nav_distance_bonus` is still awarded
at most once per ride regardless of row count.

Owner-only. Limit **30/hour per account** (`ride_route_account`).

- `400` `{"error": "unknown_profile", "profiles": [...]}` — `profile` isn't
  a live `GET /api/v1/route/profiles` key.
- `400` `{"error": "bad_polyline"}` — `route_polyline` doesn't decode
  (precision 5) to at least 2 points.
- `400` `{"error": "out_of_coverage", "graph_bbox": [...]}` — `origin` or
  `destination` falls outside the routing graph, the same rejection
  `GET /api/v1/route` uses.
- `404` — `tracked_ride_id` is set and isn't a ride you own.
- `422` — `distance_meters` outside 0–80 000, `duration_seconds` outside
  0–10 800, or `battery_percent_estimate` outside 0–100. These are your
  own client-side estimates, stored as reported; no award ever reads them
  directly (`nav_distance_bonus` reads the *verified* donation distance
  instead).
- `429` — over 30/hour.

---

## Points

A score-only ledger: points accumulate for contributing data. **Nothing
spends them** — there is no redemption endpoint.

### `GET /api/v1/points?limit=100&before=<ISO>`

Bearer required. Owner-only, newest first. `limit` 1–1000 (default 100);
`before` must include a timezone (`400` otherwise).

```json
{
  "total_points": 134,
  "entries": [
    { "id": 42, "created_at": "2026-07-27T16:45:00+00:00", "action": "qr_scan",
      "points": 100, "vehicle_identifier": "8c4a1f0d2e9b7a35", "status": "confirmed" }
  ]
}
```

`total_points` sums **confirmed** entries only, and is the total across
the whole ledger — not just the returned page.

### Award table

| Action | Points | Earned by |
|---|---|---|
| `qr_scan` | 100 | First scan of a given device by you |
| `device_features_review` | 14 | A valid `POST /reports/device-features` on a device whose `feature_status` is `needs_review` |
| `device_features_first` | 12 | A valid `POST /reports/device-features` on a device nobody has confirmed before |
| `report_not_rideable` | 10 | `not_rideable` device report |
| `report_vehicle_issue` | 10 | `damaged` device report |
| `report_improper_parking` | 10 | `improperly_parked` device report |
| `profile_completion` | 10 | One-time, on completing your profile |
| `device_features_reconfirm` | 6 | A valid `POST /reports/device-features` on a device already `up_to_date` |
| `device_photo` | 6 | Each accepted `POST /api/v1/devices/{vid}/photos` upload |
| `report_not_found` | 4 | `not_found` device report |
| `battery_contribution` | `8 + 2 × ⌈km / 2⌉` | A verified, `eligible` `POST .../track` donation, `ride_options.battery_modeling` on, not an own-device ride, both start/end battery known |
| `nav_distance_bonus` | `2 × ⌈km / 3⌉` | Same donation, `ride_options.nav_improvement` on, and a stored `POST /api/v1/ride-routes` row linked to the ride |
| `ride_survey` | 4 | `POST .../survey`, any scooter-feedback field present, `ride_options.end_survey` on, not an own-device ride |
| `nav_route_feedback` | 4 | Same survey, `nav_route_rating` present and its `ride_route_id` resolves |
| `nav_qualitative_feedback` | 6 | Same survey, `nav_qualitative` trimmed to ≥20 characters |

**Ceiling: 100 points per ride**, summed across every ride award. When the
ceiling binds, the ledger entry records the **granted** amount — `points`
in `GET /api/v1/points` is always what you actually received, never a
pre-cap figure — and an award with no headroom left writes no entry at
all.

`qr_scan` is exempt: a device scan is not a ride award, and it is worth
100 on its own. `profile_completion`, report credits and the three
`device_features_*` tiers are likewise per-account and per-report, not
per-ride.

The `device_features_*` tiers all require a **valid plate** — a wrong one
is accepted and stored but pays nothing — and are limited to **one award
per account per vehicle per 24 h**. A same-day second opinion still votes
in the consensus; it just doesn't pay twice. `device_features_review` pays
slightly more than a first confirmation on purpose: clearing the
`needs_review` queue is the scarcer act, and it is the one thing only the
crowd can unblock.

The ceiling is forward-only; entries predating it were not adjusted. See
[Ride limits](#ride-limits).

`dead_battery` reports and device recommendations deliberately award
nothing. Credits are idempotent per source row, so retries don't
double-credit.

**Superseded** (ride-mode overhaul): `waypoint` (2/waypoint, credited at
`PATCH .../end`) and `gbfs_trip_validated` (20, GBFS reappearance within
20 m) are no longer newly granted — GBFS reappearance is now an
*eligibility gate*, not an award, and the reshaped `battery_contribution`
above pays for verified distance instead. Both actions remain valid on
historical ledger rows; nothing is rewritten.

### `GET /api/v1/points/schedule`

Public — no bearer. The authoritative action → award map, generated from
`src/points.py` at request time. Rider-facing copy is interpolated from this,
so a hardcoded "+5 points" string in a client can never contradict what the
ledger pays. `Cache-Control: public, max-age=3600`.

The body **is** the map — no envelope. Index it by action name.

```json
{
  "qr_scan": { "points": 100 },
  "gbfs_trip_validated": { "points": 20 },
  "waypoint": { "points": 2 },
  "profile_completion": { "points": 10 },
  "report_not_rideable": { "points": 10 },
  "report_not_found": { "points": 4 },
  "report_vehicle_issue": { "points": 10 },
  "report_improper_parking": { "points": 10 },
  "battery_contribution": { "base": 8, "per_step": 2, "step_km": 2 },
  "nav_route_feedback": { "points": 4 },
  "nav_qualitative_feedback": { "points": 6 },
  "nav_distance_bonus": { "base": 0, "per_step": 2, "step_km": 3 },
  "ride_survey": { "points": 4 }
}
```

Two entry shapes, and only two:

| Shape | Fields | Award |
|---|---|---|
| flat | `points` | that many points, once |
| formula | `base`, `per_step`, `step_km` | `base + per_step * ceil(km / step_km)` — a **started** step counts, so 2.1 km pays two |

`nav_distance_bonus` carries `base: 0` because it is purely per-step; it is
stated rather than omitted so `base + per_step * steps` is a number for every
formula entry. An action you don't recognise is still renderable (the shapes
are stable); an action that is **absent** means this API predates it — fall
back to a baked default rather than showing nothing.

Every published value is **even** — the operator's even-points invariant. It
holds for formula outputs too: an even base plus an even per-step increment
cannot sum to an odd award.

The five ride-mode actions (`battery_contribution`, `nav_route_feedback`,
`nav_qualitative_feedback`, `nav_distance_bonus`, `ride_survey`) were published
**before** anything awarded them, because the ride wizard's info copy needs the
numbers the day it ships. Phases A2 (`POST /api/v1/tracked-rides/{id}/track`)
and A3 (`POST /api/v1/tracked-rides/{id}/survey`) have now wired all five —
the Award table above is what the ledger pays today.

---

## Device engagement

### `POST /api/v1/devices/qr-scan`

Bearer required. Validates a scanned QR against the device you claim it
belongs to, and awards the largest single bonus in the system.

```json
{ "vehicle_identifier": "8c4a1f0d2e9b7a35",
  "qr_raw_value": "https://…scanned…", "lat": 39.7392, "lng": -104.9876 }
```

→

```json
{ "vehicle_identifier": "8c4a1f0d2e9b7a35", "scan_count": 3,
  "first_scanned_at": "2026-07-10T12:00:00+00:00",
  "points_awarded": 100, "already_scanned_by_you": false }
```

`points_awarded` is `0` and `already_scanned_by_you` is `true` on a
repeat scan of the same device by the same account — the 100 points are
per device, once. `scan_count` counts scans by everyone.

`400` when the QR payload doesn't resolve to the claimed
`vehicle_identifier`, or when the device is unknown. Limit 20/hour per
account.

### `POST /api/v1/devices/{vehicle_identifier}/recommend`

Bearer required. `{ "recommend": true }` — a simple thumbs up/down.

**`403` unless you have a `completed` tracked ride on that device started
within the last 24 hours.** You can only recommend a scooter you actually
rode, recently. In practice this belongs on the ride summary screen; it
will 403 from a device popup. Awards no points.

Upsert — one row per (account, device); re-posting flips your existing
vote. → `{ id, vehicle_identifier, recommend, created_at, updated_at }`.
Limit 30/hour per account.

---

## Device photos

Rider-contributed photos of a specific scooter — **public content**,
attributed to the uploader's public username, re-encoded on ingest
(EXIF/GPS destroyed).

| Endpoint | Notes |
|---|---|
| `POST /api/v1/devices/{vehicle_identifier}/photos` | Bearer required. `multipart/form-data` with a `photo` part, plus optional `lat`/`lng`. → `{ id, vehicle_identifier, photo_url, created_at, points_awarded }`. 20/hour per account. |
| `GET /api/v1/devices/{vehicle_identifier}/photos` | Bearer required. → `{ vehicle_identifier, count, photos: [ { id, photo_url, created_at, uploaded_by } ] }`, oldest first. |
| `POST /api/v1/photos/{photo_id}/reports` | Bearer required. `{ "reason": "wrong_device" \| "inappropriate" \| "other", "comment"?: string }` (≤2000 chars). Repeat reports of the same photo by you return `{ photo_id, deduped: true }`. 10/hour per account. |
| `GET /api/v1/photos/mine` | Bearer required. Everything you've uploaded — see below. |

**Cap: 3 photos per device**, across all users. The 4th upload returns
`409`. Other upload failures: `413` over 10 MB, `422` if the `photo` part
is missing, `503` if photo storage isn't configured.

**Each accepted upload earns `device_photo` points** (see
[Points](#points)) — one credit per photo, no per-account cooldown. It
needs none: a vehicle pays **at most 3 of these awards, ever**, counted
from the ledger rather than from how many photos it currently holds (so
a photo hidden by a future moderator workflow does not free a slot to be
paid for again), and the 20/hour account limit bounds the rest. Uploading is bearer-only, so a credited photo always has
a real account behind it; **points are never anonymous** even when the
uploader hides their `public_username` (that only nulls `uploaded_by` in
the public listing).

The optional `lat`/`lng` parts are where the photo was taken — the
location the ledger row records. Our own client sends the **vehicle's**
position rather than the rider's fix, since that is what the photo is
of; either is a fair answer. They are optional and forgiving: malformed, out of
range, or half-supplied coordinates are dropped rather than failing the
upload, and the server then falls back to the vehicle's last known
position. If neither resolves, the photo is still stored and
`points_awarded` is `0` — a ledger row requires a real location, so the
award is skipped rather than filed against a fabricated one.

`uploaded_by` is the uploader's `public_username`, joined at **read**
time and `null` when that user has `show_public_username` off — so a
username change or privacy flip is reflected immediately, retroactively,
everywhere.

Listing requires a session like every rider endpoint. The photos are
nonetheless "public" in the sense that matters: `photo_url` needs no
credentials of its own, so it works anywhere once you have it — drop it
straight into an `<img>`.

It is always an absolute `https://` URL, in one of two forms depending on
deployment, and clients should treat it as opaque and re-read it from the
listing rather than storing it:

- **Static**, when `r2.public_base_url` is configured — permanent and
  cacheable. This is the preferred form, but it requires the object store
  to have a public origin, which is only appropriate for a bucket that
  holds nothing but public objects.
- **Presigned**, otherwise — signed per response and **valid for one
  hour**, so it is not a durable handout of an object we may later hide.

### `GET /api/v1/photos/mine`

Two different content models kept as two keys, both scoped to you:

```json
{
  "device_photos": [
    { "id": 12, "vehicle_identifier": "8c4a…", "photo_url": "https://…",
      "created_at": "2026-07-20T10:00:00+00:00", "status": "visible" }
  ],
  "ride_transaction_screenshots": [
    { "id": 4, "ride_id": "3f2a…-uuid", "screenshot_type": "receipt",
      "url": "https://…presigned…", "created_at": "2026-07-21T09:00:00+00:00" }
  ]
}
```

Device photos are public and carry a `status`; screenshot `url`s are
always presigned and expire in ten minutes. "Private" means "not visible
to other accounts," which is why your own screenshots appear here.

---

## Meta

### `GET /api/v1/meta/privacy`

Machine-readable retention policy — the frontend privacy page renders
this, so the published policy and the enforced one can't drift:

```json
{ "updated": "2026-07-04", "contact": "zneill@gmail.com",
  "retention": [ { "data": "sessions", "retention": "30 days idle", "detail": "…" } /* … */ ] }
```

Current `data` keys: `sessions`, `magic_link_tokens`, `receipts`, `rides`,
`reports`, `accounts`, `user_preferences`, `tracked_rides`,
`donated_tracks`, `user_points`, `device_photos`, `model_reports`,
`ride_transaction_screenshots`, `telemetry_events`, `request_metrics`,
`analytics_rollups`. Render the list as served — don't hardcode
it, since new data classes get appended here first.

`donated_tracks` (added with track donation, above): a donated ride track
loses its account link 4 hours after points settle, with a hard floor of
28 hours after donation even if points never settle — a `POST .../track`
response is never the last word on that ride's linkage. `user_points`
(a pre-existing gap this endpoint now closes): ledger rows keep account,
h3 cell and ride-start coordinates indefinitely — they're the leaderboard
record — and are removed only by account deletion.

### `GET /api/v1/meta/pricing`

Public. The sales-tax rate Ride Mode's cost breakdown applies, from the
`config.json` `"pricing"` block. Veo's rate **plans** stay client-side; the tax
rate does not, because it changes when a ballot measure passes and every
installed client would otherwise be wrong until it updated.

```json
{ "tax_rate": 0.0915, "currency": "USD", "as_of": "2025-01-01" }
```

`tax_rate` is a **fraction, not a percentage** — 0.0915, never 9.15. Multiply
the pre-tax fare by it directly. The default is Denver's combined rate
(2.90 % state + 1.00 % RTD + 0.10 % SCFD + 5.15 % city), effective 2025-01-01;
`as_of` is that **effective date**, not when the response was generated. A
configured value outside `[0, 1)` is refused (logged, default served) rather
than shown to a rider as a hundredfold tax.

Clients bake the same default for offline use, so this endpoint is a refresh,
never a dependency. `Cache-Control: public, max-age=3600`.

---

## Telemetry

### `POST /api/v1/telemetry/events`

First-party usage-analytics ingest for the official frontend. Anonymous,
no auth. Third-party clients have no reason to call this — events from
unknown names are dropped, and there is nothing to read back.

```json
{
  "v": 1,
  "page": { "vp": "md", "dc": "mobile", "os": "ios",
            "ref": "google.com", "theme": "dark", "auth": false },
  "events": [
    { "n": "page_load", "t": 1754400000000, "sid": "k3f9x2ab41mz",
      "p": { "first_of_day": true } }
  ]
}
```

Behavior:

- Always `204` — malformed bodies, unknown event names, and oversized
  payloads are **dropped silently**, never `4xx`. A stale cached bundle
  must not error-spam, and there is no useful client reaction to a
  rejected analytics batch.
- Caps: ≤ 50 events per batch, body ≤ 32 KB, ≤ 12 props per event,
  prop values truncated at 120 chars, `sid` at 16 chars. Timestamps
  outside ±1 h of server time are replaced with arrival time.
- Event names come from a fixed allowlist (mirrored in the frontend's
  `src/telemetry.ts`); `page.dc` / `page.os` / `page.vp` are vocabulary-
  checked and fall back to `other`.
- Rate limit: 120 batches per IP per hour → `429` with `Retry-After`.
- Privacy: no account id is ever accepted or stored; visitor counting
  uses a daily-salted hash of IP + user-agent whose salt is destroyed
  after 2 days (see `GET /api/v1/meta/privacy`, keys `telemetry_events`,
  `request_metrics`, `analytics_rollups`).

## Layer reference

The twelve layers, their `region_type` values (used in `layer=` query
params), and the naming convention for `region_name` (used in the
trend endpoint and as the keys of `regions` in spatial-snapshot).

| `region_category` | `region_type` | # of regions | `region_name` examples |
|---|---|---|---|
| `equity_areas` | `equity` | 30 | `EQ_001`, `EQ_002`, … `EQ_030` (ordinal, zero-padded to 3 digits) |
| `disadvantaged_areas` | `v1` | 34 | `V1_001`, `V1_002`, … `V1_034` (ordinal, zero-padded to 3 digits) |
| `disadvantaged_areas` | `v2` | 65 | `V2_080010001001`, `V2_080010002003`, … (US Census Block Group GEOID20) |
| `disadvantaged_areas` | `er1` | 34 | `ER1_080310043081`, … (US Census Block Group GEOID20; `EquityGroupRank == 1`, highest need) |
| `disadvantaged_areas` | `er2` | 58 | `ER2_...` (`EquityGroupRank == 2`) |
| `disadvantaged_areas` | `er3` | 157 | `ER3_...` (`EquityGroupRank == 3`) |
| `disadvantaged_areas` | `er4` | 93 | `ER4_...` (`EquityGroupRank == 4`) |
| `disadvantaged_areas` | `er5` | 114 | `ER5_...` (`EquityGroupRank == 5`) |
| `disadvantaged_areas` | `er6` | 116 | `ER6_...` (`EquityGroupRank == 6`, lowest need) |
| `council_districts` | `council_district` | 11 | `CD_1`, `CD_2`, … `CD_11` (Denver City Council district numbers) |
| `community_networks` | `community_network` | 13 | `CN_Central`, `CN_East`, `CN_EastCentral`, `CN_FarNortheast`, `CN_FarSoutheast`, `CN_North`, `CN_Northeast`, `CN_Northwest`, `CN_ParkHill`, `CN_SouthCentral`, `CN_Southeast`, `CN_Southwest`, `CN_West` |
| `neighborhoods` | `neighborhood` | 78 | `NB_AthmarPark`, `NB_Auraria`, `NB_Baker`, `NB_Barnum`, `NB_CBD`, `NB_CapitolHill`, `NB_CherryCreek`, `NB_FivePoints`, `NB_Highland`, `NB_SloanLake`, `NB_WashingtonPark`, `NB_Westwood`, … (Denver Statistical Neighborhood names with non-alphanumerics stripped) |

### Notes on the layers

- **`equity` is the official map.** In August 2026 the city clarified which polygon the Veo license agreement's Equity Area Deployment target (Exhibit B: 30% of the active fleet, averaged over the 6–9 AM window) is actually measured against. That map is this layer: 30 polygons, `EQ_001`–`EQ_030`. `percent_all_devices_equity` on `/api/v1/snapshots/latest` and `avg_percent_all_devices_equity` / `compliance_equity_pass` on the daily-SLA endpoints are the **contractually binding** figures. Everything below is retained history — still computed, still returned, no longer the answer.
- **v1 vs v2** are two distinct versions of the city's original Equity / Opportunity Areas polygon. Both exist because Denver's contract negotiations referenced both; `percent_all_devices_v1` was the canonical compliance metric until the `equity` layer above superseded it, with `v2` tracked in parallel throughout. They are not nested or disjoint — a device can be in both, neither, or one or the other.
- **`er1`–`er6`** are Denver DOTI's newer, authoritative census-block-group Equity Index, split into one layer per exact `EquityGroupRank` tier (`er1` = highest need, `er6` = lowest). Unlike v1/v2 they **partition** the scored area — every scored block group falls in exactly one `erN` layer, never two. They're tracked individually (not pre-combined into a cutoff) in both `/api/v1/snapshots/latest` and `/api/v1/compliance/daily/latest` so that whatever cutoff DOTI confirms as contractually authoritative can be reconstructed from history later (e.g. a "rank ≤ 2" metric = `er1 + er2`). **No individual `erN` layer is a compliance boundary** — and the question they were tracked to answer is now settled by the `equity` layer above, so they are historical. See API_REQUIREMENTS.md §1.1a.
- **At-Large council districts** (Gonzales-Gutierrez and Parady, which cover the entire city) are **excluded** from `council_district` rows to avoid double-counting. Only the 11 numbered districts appear.
- **Neighborhoods** uses Denver's Statistical Neighborhood Boundaries (DOTI). Spaces and punctuation are stripped from names: `Athmar Park` → `NB_AthmarPark`, `Park Hill` → `NB_ParkHill` (note: there are also separate `NB_NortheastParkHill`, `NB_NorthParkHill`, `NB_SouthParkHill` neighborhoods).
- **Community Networks** are Denver's 13 official planning regions, broader than neighborhoods.

### Full neighborhood enumeration

The 78 neighborhood region names, alphabetical:

```
NB_AthmarPark, NB_Auraria, NB_Baker, NB_Barnum, NB_BarnumWest,
NB_BearValley, NB_Belcaro, NB_Berkeley, NB_CBD, NB_CapitolHill,
NB_CentralPark, NB_ChaffeePark, NB_CheesmanPark, NB_CherryCreek,
NB_CityPark, NB_CityParkWest, NB_CivicCenter, NB_Clayton, NB_Cole,
NB_CollegeViewSouthPlatte, NB_CongressPark, NB_CoryMerrill,
NB_CountryClub, NB_EastColfax, NB_ElyriaSwansea, NB_FivePoints,
NB_FortLogan, NB_GatewayGreenValleyRanch, NB_Globeville,
NB_Goldsmith, NB_Hale, NB_Hampden, NB_HampdenSouth, NB_HarveyPark,
NB_HarveyParkSouth, NB_Highland, NB_Hilltop, NB_IndianCreek,
NB_JeffersonPark, NB_Kennedy, NB_LincolnPark, NB_LowryField,
NB_MarLee, NB_Marston, NB_Montbello, NB_Montclair,
NB_NorthCapitolHill, NB_NortheastParkHill, NB_NorthParkHill,
NB_Overland, NB_PlattPark, NB_Regis, NB_Rosedale, NB_RubyHill,
NB_Skyland, NB_SloanLake, NB_SouthmoorPark, NB_SouthParkHill,
NB_Speer, NB_Sunnyside, NB_SunValley, NB_UnionStation,
NB_University, NB_UniversityHills, NB_UniversityPark, NB_Valverde,
NB_VillaPark, NB_VirginiaVillage, NB_WashingtonPark,
NB_WashingtonParkWest, NB_WashingtonVirginiaVale, NB_Wellshire,
NB_WestColfax, NB_WestHighland, NB_Westwood, NB_Whittier,
NB_Windsor
```

---

## Common patterns

### Compliance gauge ("are we above 30%?")

For an **at-a-glance current reading**, use the every-10-min snapshot:
```javascript
const r = await fetch("https://data.scooter.fyi/api/v1/snapshots/latest");
const s = await r.json();
const v1Pct = s.percent_all_devices_v1;            // may be null
const compliant = v1Pct !== null && v1Pct >= 30;
document.querySelector("#gauge").textContent =
  v1Pct === null ? "no data" : `${v1Pct.toFixed(1)}%`;
document.querySelector("#status").textContent =
  compliant ? "✅ compliant" : "⚠️ below threshold";
```

For the **contractually-binding daily reading** (License Exhibit B: "Daily deployment average during the 6am-9:00am window"), use the daily SLA endpoint instead:
```javascript
const r = await fetch("https://data.scooter.fyi/api/v1/compliance/daily/latest");
const d = await r.json();
const v1Pct = d.avg_percent_all_devices_v1;       // 6-9 AM Denver mean
document.querySelector("#sla-gauge").textContent =
  v1Pct === null ? "pending" : `${v1Pct.toFixed(1)}% (SLA)`;
document.querySelector("#sla-date").textContent = `for ${d.sla_date}`;
document.querySelector("#sla-status").textContent =
  d.compliance_v1_pass ? "✅ daily SLA met" : "⚠️ daily SLA missed";
```

For a **rolling compliance dashboard** (e.g. last 30 days):
```javascript
const since = new Date(Date.now() - 30 * 86_400_000).toISOString().slice(0, 10);
const r = await fetch(`https://data.scooter.fyi/api/v1/compliance/daily/range?start=${since}`);
const { rows } = await r.json();
const passing = rows.filter(d => d.compliance_v1_pass).length;
const pct = rows.length ? Math.round(passing / rows.length * 100) : 0;
document.querySelector("#thirty-day").textContent = `${passing} / ${rows.length} days passed (${pct}%)`;
```

### Live choropleth (color neighborhoods by device density)

```javascript
async function refreshMap() {
  const r = await fetch("https://data.scooter.fyi/api/v1/spatial-snapshot?layer=neighborhood");
  const { snapshot_time, regions } = await r.json();
  for (const [name, counts] of Object.entries(regions)) {
    // map your layer's polygon for `name` to a color based on counts.total
    setPolygonColor(name, scaleColor(counts.total));
  }
  document.querySelector("#updated-at").textContent =
    `as of ${new Date(snapshot_time).toLocaleString()}`;
}
refreshMap();
setInterval(refreshMap, 60_000);     // poll every minute
```

### Sparkline for one region over 24h

```javascript
const url = "https://data.scooter.fyi/api/v1/analytics/trend"
          + "?layer=neighborhood&name=NB_FivePoints&range=24h";
const r = await fetch(url);
const { points } = await r.json();
const xs = points.map(p => new Date(p.snapshot_time));
const ys = points.map(p => p.count_total);
drawSparkline(xs, ys);
```

### "Top N regions right now"

```javascript
const r = await fetch("https://data.scooter.fyi/api/v1/spatial-snapshot?layer=neighborhood");
const { regions } = await r.json();
const top10 = Object.entries(regions)
  .sort(([,a], [,b]) => b.total - a.total)
  .slice(0, 10);
// top10 == [["NB_CBD", {total: 312, ...}], ["NB_CapitolHill", ...], ...]
```

### Cross-layer comparison (bike share inside vs outside equity areas)

```javascript
const s = await (await fetch("https://data.scooter.fyi/api/v1/snapshots/latest")).json();
const bikeShareV1     = s.percent_bikes_v1;
const bikeShareDenver = s.percent_bikes_denver;
const delta = bikeShareV1 - bikeShareDenver;
// positive delta = bike-skewed inside v1 vs citywide average
```

---

## Error reference

| Code | Meaning | When |
|---|---|---|
| `200` | OK | Normal response. |
| `202` | Accepted | Magic-link request accepted (says nothing about account existence). |
| `304` | Not modified | `If-None-Match` matched the current ETag — empty body, keep what you have. See [Caching & compression](#caching--compression). |
| `400` | Bad query/body | Malformed `time`/`range`/`ranks`/`include` parameter, bad signature, unreadable receipt image. |
| `401` | Unauthenticated | Missing/invalid/expired bearer token, failed Google credential, dead magic link. Treat as signed out. |
| `403` | Forbidden | Valid session but not on the admin allowlist, or an action you haven't earned — e.g. recommending a device you have no recent completed ride on. |
| `404` | No data | Requested layer has no snapshots (cold start), an unknown `vehicle_identifier`, or the resource isn't yours. |
| `409` | Conflict | State says no: a second active tracked ride, a re-reported ride end, or a 4th photo on a device. Not retryable — resolve the conflict first. |
| `413` | Too large | Receipt, device photo, or ride screenshot over 10 MB; a preference/Usual blob over 16 KB; `ride_options` over 4 KB; a track donation over 2 MB or 600 batches. |
| `422` | Unprocessable | A required multipart part is missing, routing found no path (`no_route`, `no_route_from_location`), a client-asserted ride isn't [plausible](#is-this-ride-possible) (`implausible_speed`, `distance_exceeds_polyline`), a waypoint breaks a [ride limit](#ride-limits) (`waypoint_too_far`, `ride_distance_cap_reached`), or a track donation's chain failed verification (`chain_invalid`) or was never opted into (`tracking_not_opted`). |
| `429` | Rate limited | A bucket is full — honor the `Retry-After` header (seconds). Mostly POST buckets, but three public GETs are limited too: `/api/v1/route` (30/min/IP), `/api/v1/route/profiles` (60/min/IP) and `/api/v1/geocode/search` (20/min/IP). |
| `502` | Upstream failure | Email provider rejected a magic-link send. Retry in a minute. |
| `503` | Service unavailable | No snapshots exist yet, or the feature isn't configured on this deployment (Google/magic-link/receipts/photo storage/router). |
| `5xx` (other) | Server error | Worker or Postgres failure. Logged in Sentry; transient — retry. |

Error responses are JSON: `{ "detail": "human-readable message" }`.

**`detail` is not always a string.** A few endpoints return a structured
object instead, so a client that assumes `string` will render
`[object Object]`. Each carries an `error` key with a stable machine code.
Branch on `typeof detail === "object" ? detail.error : detail`.

| `error` | Status | Where |
|---|---|---|
| `unknown_profile`, `out_of_coverage`, `no_route`, `no_route_from_location`, `router_unavailable` | `422` / `503` | `/api/v1/route` |
| `ride_not_active` | `409` | Waypoint append, both ride mechanisms |
| `ride_expired` | `409` | `PATCH /api/v1/rides/{id}/end` |
| `waypoint_too_far` | `422` | Waypoint append, both ride mechanisms |
| `ride_distance_cap_reached` | `422` | Waypoint append, both ride mechanisms |
| `implausible_speed`, `distance_exceeds_polyline` | `422` | `POST /api/v1/rides` |
| `bad_ride_options` | `422` | `POST /api/v1/tracked-rides` |
| `ride_not_ended` | `409` | `POST /api/v1/tracked-rides/{id}/track` |
| `already_donated` | `409` | `POST /api/v1/tracked-rides/{id}/track` |
| `tracking_not_opted` | `422` | `POST /api/v1/tracked-rides/{id}/track` |
| `chain_invalid` (carries `failing_check`, `batch_seq`) | `422` | `POST /api/v1/tracked-rides/{id}/track` |
| `bad_batches`, `too_many_batches`, `donation_too_large` | `422` / `413` | `POST /api/v1/tracked-rides/{id}/track` |
| `bad_bias`, `bad_query`, `geocoder_unavailable` | `400` / `422` / `503` | `/api/v1/geocode/search` |

**Rate limits are per-account and tight** — most write buckets are 10–30
per hour. The exception is tracked-ride waypoints at 600/hour; track
donation is the tightest, at 6/hour, since one ride only ever needs one.
Every POST path needs a `429` path with `Retry-After`; treat that as part
of wiring the endpoint, not as polish.

---

## Caching & compression

Explicit `Cache-Control` headers, per endpoint:

| Endpoint | Cache-Control | ETag |
|---|---|---|
| `/api/v1/devices/current` | `public, max-age=30` | weak, keyed on `(cycle_id, include tokens)` |
| `/api/v1/h3/aggregates` | `public, max-age=600` | weak, keyed on `(res, cycle_id)` |
| `/api/v1/equity-estimate` | `public, max-age=60` | weak, keyed on `(cycle_id, ranks)` |
| `/api/v1/compliance/calendar` | `public, max-age=300` | — |
| `/api/v1/boundaries` | `public, max-age=3600` | — |
| `/api/v1/boundaries/{layer}` | `public, max-age=86400, stale-while-revalidate=604800` | — |
| `/api/v1/reports/summary` | `public, max-age=600` | — |
| `/api/v1/reports/export/monthly.csv` | `public, max-age=600` | — |
| `/api/v1/meta/privacy` | `public, max-age=3600` | — |
| `/api/v1/meta/pricing` | `public, max-age=3600` | — |
| `/api/v1/points/schedule` | `public, max-age=3600` | — |
| `/api/v1/leaderboard/map` | `public, max-age=30` | weak, content-only (`sha256(canonical cells)[:16]`) -- computed live, so there is no run id to key on and `computed_at` moves every request |
| `/api/v1/leaderboard/regional` | `public, max-age=30` | weak, content-only (`sha256(leaders)[:16]`) -- same reason |
| `/api/v1/private/area-leaders` | none (admin) | -- |
| `/api/v1/private/regional-leaders` | none (admin) | -- |

Endpoints not listed set no cache headers; caching those for ≤30 s is
safe in practice (a new snapshot lands at most every 10 minutes).

Where an ETag is served, poll with `If-None-Match` — an unchanged
resource returns `304` with no body. All ETags are cycle-keyed: they
change when a new ingest cycle lands, not before.

Responses ≥1 KB are gzip-compressed at the origin
(`Accept-Encoding: gzip`); behind Cloudflare the edge re-encodes to
brotli for clients that prefer it.

---

## Stability commitments

- **Field names in `snapshot_metadata_core`** are stable — these are the 22 RFP-mandated metrics and won't be renamed.
- **`region_name` strings** are stable per layer. Adding a new neighborhood (rare — last city update was years ago) would add a key; existing keys won't move.
- **New optional fields** may be added to responses without notice. Clients should ignore unknown fields, not error.
- **Breaking changes** (removed fields, renamed endpoints) will go through a versioned path (`/api/v2/...`) with the previous version kept live for at least 90 days.
- **Update cadence** may shift from 10 minutes to faster as we tune, but never slower than 15 minutes.

---

## Reporting issues

This is an open-source compliance audit tool. Issues, schema requests,
and PRs are welcome at:

- Source: <https://github.com/z280/scooter-fyi-api>
- Operator: <zneill@gmail.com>

If a metric looks wrong, include the `cycle_id` from
`/api/v1/snapshots/latest` in your report — that lets us trace it back
to the exact upstream GBFS payload and spatial-join inputs.
