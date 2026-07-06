# veo-audit Public API

Read-only REST API serving Denver Veo micromobility fleet data. Polls the
upstream GBFS feed every 10 minutes, geo-tags each device against five
spatial layers (Disadvantaged Areas v1/v2, Council Districts, Community
Networks, Neighborhoods), and exposes both citywide summary metrics and
per-region breakdowns.

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
map and compliance dashboards is unauthenticated. Accounts exist for the
cost ticker's rate choice, report attribution, and supporter features —
see [Accounts & sessions](#accounts--sessions). Authenticated endpoints
take `Authorization: Bearer <token>`. The `/admin/*` routes (not
documented here) require GitHub OAuth and are intended for operators
only.

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
| **Spatial filtering** | A device is `denver_core` only if its coordinates fall **inside the actual Denver city polygon** (union of all 78 official neighborhood boundaries). A rough lat/lon bounding box is used as a fast first-pass; final classification uses the polygon. Devices in the bbox but outside the polygon (Aurora, Lakewood, the Veo repair shop, etc.) are tagged `other_outlier` and excluded from all citywide metrics. `total_not_in_denver` exposes the count of excluded devices (China factory glitches + outside-city-limits combined). |

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
| `total_devices_denver` | int | All devices (bikes + scooters + unknown form factors) located **inside the actual Denver city polygon** (union of the 78 official neighborhood boundaries). Excludes devices in the rough bbox but outside the city limits, such as Veo's repair facility. |
| `total_devices_v1` | int | Devices located inside the **Disadvantaged Areas v1** boundary. |
| `total_devices_v2` | int | Devices located inside the **Disadvantaged Areas v2** boundary. |
| `total_bike_denver` | int | `form_factor == "bicycle"` devices inside Denver. |
| `total_bike_v1` | int | Bicycles inside v1. |
| `total_bike_v2` | int | Bicycles inside v2. |
| `total_scooter_denver` | int | `form_factor == "scooter"` devices inside Denver. |
| `total_scooter_v1` | int | Scooters inside v1. |
| `total_scooter_v2` | int | Scooters inside v2. |
| `total_not_in_denver` | int | Devices reporting coordinates outside the actual Denver city polygon. Includes both obvious outliers (China factory glitches, devices in transit) and adjacent-jurisdiction false positives (Aurora, Lakewood, repair shops just over the city line). Excluded from all `*_denver`/`*_v1`/`*_v2` counts. |
| `percent_all_devices_v1` | float \| null | `total_devices_v1 / total_devices_denver * 100`. **This is the primary RFP §3.0 compliance metric — Denver requires ≥30%.** |
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
`er1`–`er6` is a confirmed compliance boundary today — `percent_all_devices_v1`
remains **the** primary RFP §3.0 metric until DOTI confirms otherwise;
see API_REQUIREMENTS.md §1.1a.

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

**Layer values:** `v1`, `v2`, `neighborhood`, `council_district`, `community_network`.

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

### `GET /api/v1/devices/current`

GeoJSON FeatureCollection of every device's current position from the
most recent successfully-completed cycle. Suitable for direct ingestion
into map libraries (Mapbox GL JS, MapLibre GL JS, Leaflet, OpenLayers).

By default returns **only devices inside the actual Denver city polygon**
(`spatial_status='denver_core'`) — China-factory glitches and devices
located outside the city limits (Aurora, Lakewood, repair shops) are
hidden unless explicitly requested.

**Query parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `form_factor` | string | (all) | Filter by `bicycle` or `scooter`. |
| `spatial_status` | string | (default below) | Filter by `denver_core`, `china_glitch`, or `other_outlier`. Explicit value overrides `include_outliers`. |
| `include_outliers` | bool | `false` | When true, returns devices regardless of envelope. Ignored if `spatial_status` is set. |
| `bbox` | string | (none) | `min_lon,min_lat,max_lon,max_lat` WGS84 bounding box. Useful for viewport-level queries. |

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
    }
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
        "propulsion_type": "electric",
        "number_failed_starts": 0,
        "first_observed_at_location": "2026-05-30T16:10:09+00:00",
        "reliability_tier": "ok",
        "vehicle_use_type": "standing",
        "vehicle_model_name": "Astro"
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
        "current_range_meters": 38110,
        "propulsion_type": "electric",
        "number_failed_starts": 0,
        "first_observed_at_location": "2026-05-30T17:40:12+00:00",
        "reliability_tier": "unknown",
        "vehicle_use_type": "sitting",
        "vehicle_model_name": "Apollo"
      }
    }
    /* … ~1,866 more features … */
  ]
}
```

#### Feature property reference

| Field | Type | Description |
|---|---|---|
| `device_id` | string | The upstream Veo `bike_id` from GBFS `free_bike_status`. **Rotates per trip** by GBFS spec mandate — do not treat as stable. |
| `form_factor` | string | `"bicycle"`, `"scooter"`, or `"unknown"`. Not taken as-given from Veo's upstream `vehicle_types.json` — corrected against direct visual confirmation where the upstream registry is known to be wrong (see `vehicle_model_name` below). |
| `spatial_status` | string | `"denver_core"`, `"china_glitch"`, or `"other_outlier"`. |
| `vehicle_identifier` | string \| null | 16-hex-character stable per-scooter identifier (e.g. `"8c4a1f0d2e9b7a35"`). Persistent across trips, unlike `device_id`. Computed as `HMAC-SHA256(server_salt, visible_plate)[:16]`. This is the stable key for reports and cross-cycle joins. May be null if the upstream payload omits a plate. **The raw plate is NOT exposed on this public endpoint** — it's served only by the bearer-gated `/api/v1/private/*` endpoints. |
| `is_disabled` | bool \| null | `true` when the scooter is out of service (low battery, mechanical fault, impound). Disabled devices still count toward fleet totals because they occupy space. |
| `is_reserved` | bool \| null | `true` when a rider has the scooter on hold (typically a 5–10 min reservation window before unlock). |
| `current_range_meters` | int \| null | Estimated remaining range from upstream, in meters. Pair with `propulsion_type` and the per-type `max_range_meters` to derive battery % — pedal-bike (`"human"`) entries have no battery. |
| `propulsion_type` | string \| null | `"electric"`, `"electric_assist"` (pedal-assist), or `"human"` (pedal-only). Splits the `form_factor: "bicycle"` bucket into throttle e-bikes vs pedal-assist vs acoustic. |
| `h3_8_index` / `h3_9_index` / `h3_10_index` | int \| null | [Uber H3](https://h3geo.org/) hexagonal cell IDs at resolutions 8 (~750m wide), 9 (~210m), and 10 (~75m). 64-bit integers. Same value across resolutions for stationary devices; change when the scooter moves. Useful for spatial aggregation client-side. |
| `range_percentile_by_type` | string \| null | One of `"0"`, `"25"`, `"50"`, `"75"`. Which quartile of unique `current_range_meters` values **within the same `form_factor`** this scooter falls into. `"75"` = top quartile (most range). |
| `range_rank_unique_by_type` | string \| null | `"x/y"` where `x` is the rank of this scooter's range value among the `y` *distinct* range values within its form_factor (ascending; ties share a position). |
| `range_rank_all_by_type` | string \| null | `"x/y"` where `y` is the count of scooters of this form_factor and `x` is this scooter's rank ascending (1 = lowest range). **Ties get the highest position in the tied group**: 20 scooters tied for the top range in a fleet of 100 all show `"100/100"`. |
| `range_rank_all_devices` | string \| null | Same as above but `y` = all eligible scooters across types. |
| `range_rank_h3_8_peers` / `range_rank_h3_9_peers` / `range_rank_h3_10_peers` | string \| null | Range rank within the same h3 cell at the given resolution. A scooter alone in its cell shows `"1/1"`. |
| `has_negative_report` | bool | `true` when ≥1 citizen-submitted report has been filed against this `vehicle_identifier` at this exact `h3_10_index` cell within the last 24h. Becomes `false` automatically when the scooter moves to a different h3_10 cell. Submit reports via `POST /api/v1/reports`. |
| `quality_designation` | string | One of `"poor"`, `"acceptable"`, `"good"`, `"great"`, or `"N/A"`. Composite score from range, dwell time, failed-start count, and active negative reports. `"N/A"` for disabled, reserved, or rangeless devices. See README / src/quality.py for the rule set. |
| `number_failed_starts` | int \| null | How many times the upstream `bike_id` rotated (someone started a rental) **without the scooter moving** since it arrived at its current location. Resets to 0 when the scooter moves. Null when the device isn't state-tracked (no plate in the upstream payload). |
| `first_observed_at_location` | string \| null | UTC ISO 8601 timestamp of when we first observed the scooter at its current location. `now - first_observed_at_location` = dwell time. Resets when the scooter moves. Null when the device isn't state-tracked. |
| `reliability_tier` | string | `"ok"`, `"unknown"`, or `"high_risk"` — a single "will it actually unlock?" signal. `high_risk`: an active negative report, ≥2 failed starts, 1 failed start + ≥24 h dwell, or ≥96 h dwell. `unknown`: device not state-tracked, or `quality_designation` is `"N/A"` (disabled/reserved/rangeless). `ok`: everything else. Formula lives in `src/quality.py` (`compute_reliability_tier`) so the audit stays reproducible. Unlike `quality_designation`, battery range never affects this field. |
| `vehicle_use_type` | string \| null | `"sitting"` or `"standing"` — whether a rider sits or stands to operate the vehicle. Independent of `form_factor`: this is the accessibility-relevant distinction for compliance purposes, tracked as its own axis in case a future vehicle class doesn't follow the current pattern (every bicycle sits, every scooter stands, as of everything observed so far). Null for a `vehicle_type_id` we haven't classified in any way. See [Tracked equity groups](#tracked-equity-groups-v1-v2-er1er6) for how this feeds the compliance snapshot. |
| `vehicle_model_name` | string \| null | Veo's own in-app display name for the physical vehicle model — `"Astro"` (kick scooter), `"Cosmo"` (throttle e-bike, no pedals), or `"Apollo"` (two-person pedal e-bike, seated, ~18mph). Visually confirmed per `vehicle_type_id`, not read from any upstream field (Veo's GBFS feed doesn't expose model names). Null for a `vehicle_type_id` not yet confirmed — absence doesn't imply anything about the vehicle, just that nobody's looked yet. |

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
- Typical response sizes:
  - All Denver devices (~5,900 features): ~470 KB JSON, ~95 KB gzip
  - Filtered to scooters (~1,870 features): ~150 KB / ~32 KB gzip
  - bbox-filtered (downtown ~500 features): ~40 KB / ~10 KB gzip
- Recommended polling: **60–120 seconds**. The upstream cycle only fires every 10 minutes, so faster polling wastes bytes.
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

### `GET /api/v1/compliance/daily/latest`

Most recent daily 6 AM – 9 AM Denver SLA window. **This is the contractually-correct compliance metric per License Exhibit B** — the every-10-min `/snapshots/latest` value is informational, but the binding SLA is the morning-window daily average. Computed once per day at 9:00 AM Denver time.

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
{ "sla_date": null, "window_start_ts": null, "window_end_ts": null, "snapshot_count": 0, "avg_percent_all_devices_v1": null, /* … all other avg_* fields null, including er1..er6 … */ "compliance_v1_pass": null, "compliance_v2_pass": null, "computed_at": null }
```

#### Field reference

| Field | Type | Description |
|---|---|---|
| `sla_date` | string (date) \| null | Denver-local date the window covers (YYYY-MM-DD). `null` in the pending response. |
| `window_start_ts` | string \| null | 6:00 AM Denver expressed as UTC. `null` in the pending response. |
| `window_end_ts` | string \| null | 9:00 AM Denver expressed as UTC. `null` in the pending response. |
| `snapshot_count` | int | Number of cycles whose `snapshot_time` fell inside the window. Typically 18 (3 hours × 6 cycles/hour). Lower values indicate cycle misses; 0 means no data. |
| `avg_*` fields | float \| null | Arithmetic mean of the corresponding `snapshot_metadata_core` field across all snapshots in the window, **for every tracked group** (`v1`, `v2`, `er1`–`er6` — see [Tracked equity groups](#tracked-equity-groups-v1-v2-er1er6)). Null when `snapshot_count == 0`. |
| `compliance_v1_pass` | bool \| null | `avg_percent_all_devices_v1 >= 30`. The primary SLA boolean. Null when no data. |
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

Days without any computed row are simply omitted from `rows` — don't expect dense coverage immediately after deploy or during pipeline outages.

---

## Accounts & sessions

Two sign-in doors, one session model. Session-minting endpoints return
exactly `{ "token": "...", "expires": "<ISO 8601>" }`; store the token
and send it as `Authorization: Bearer <token>`. Tokens are opaque
(256-bit random) and stored server-side only as hashes.

**Scopes:** every session has `rider`. `admin` is granted only on Google
sign-in for allowlisted operator emails — magic-link sessions never carry
it. `supporter` appears automatically while the account has a live
supporter payment (see the Stripe webhook).

**Expiry:** rider sessions last 30 days and slide — call
`POST /api/v1/auth/refresh` any time to rotate the token and get a fresh
30 days (the old token is revoked). Admin sessions last a fixed 24 h;
refresh rotates without extending.

| Endpoint | Body / notes |
|---|---|
| `POST /api/v1/auth/google` | `{ "credential": "<Google ID token>" }` from Google Identity Services / One Tap. Verified locally (signature, audience, expiry, `email_verified`). → `{token, expires}` |
| `POST /api/v1/auth/magic-link` | `{ "email": "you@example.com" }` → always `202 { "sent": true }` (no account-existence oracle). Emails a single-use link (15-min TTL). Limits: 3/hour per email, 10/hour per IP. `502` if the email provider fails, `503` if unconfigured. |
| `POST /api/v1/auth/redeem` | `{ "token": "<from the emailed link>" }` → `{token, expires}`. Single-use; `401` if invalid, expired, or already used. |
| `POST /api/v1/auth/refresh` | Bearer required. → `{token, expires}` (new token; old one revoked). |
| `GET /api/v1/auth/session` | Bearer required. → `{ email, scopes, supporter, expires }`. `401` when invalid/expired — treat as signed out. |
| `POST /api/v1/auth/signout` | Bearer required. Revokes the token. → `{ "revoked": true }` |

### `GET /api/v1/profile` / `PUT /api/v1/profile`

Bearer required. GET returns:

```json
{
  "email": "you@example.com",
  "rate_plan": "resident",
  "theme": null,
  "favorites": [],
  "supporter": false,
  "badges": [ { "id": "first_report", "label": "Filed a report", "earned_at": "2026-07-01T18:00:00+00:00" } ]
}
```

PUT accepts any subset of the client-writable fields — omitted fields are
untouched, `"theme": null` clears the theme:

| Field | Type | Notes |
|---|---|---|
| `rate_plan` | `"resident" \| "visitor" \| "equity"` | Drives the frontend cost ticker. |
| `theme` | string \| null | Free-form, ≤64 chars. |
| `favorites` | array | Opaque JSON, ≤100 entries — shape TBD by the frontend. |

`supporter` and `badges` are server-computed and ignored if sent. Badge
ids: `first_report`, `reporter_10`, `ghost_hunter` (one of your reports
corroborated by a different reporter within 7 days), `discount_watchdog`,
`miles_10`, `miles_100`, `streak_7` (rides on 7 consecutive days),
`supporter`. Badges are recomputed on every read, so new thresholds apply
retroactively.

---

## Rider reports

### `POST /api/v1/reports/device`

Report a scooter that failed you. Anonymous is fine (1/hour per IP);
sending a bearer token links the report to your account (10/hour) and
weighs it double in the public aggregates.

```json
{ "vehicle_identifier": "8c4a1f0d2e9b7a35", "report_type": "failed_unlock",
  "observed_at": "2026-07-04T16:20:00Z", "lat": 39.7392, "lng": -104.9876 }
```

`report_type`: `failed_unlock` | `dead_battery` | `damaged`. `observed_at`,
`lat`, `lng` optional — without coordinates the report is anchored to the
scooter's last known cell. → `{ "id": 17, "reported_at": "...", "deduped": false }`.
An identical (vehicle, type, reporter) report within 30 minutes returns
the existing row with `"deduped": true` instead of creating a new one.

Reports feed `has_negative_report` and `reliability_tier` on
`/api/v1/devices/current` for 24 h or until the scooter moves, whichever
comes first.

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

## Supporter: ride history

`POST /api/v1/rides` requires the `supporter` scope (pay-what-you-want
via the Stripe Payment Link; the webhook flips the account flag).
Reading and deleting your rides needs only a signed-in session — a lapsed
supporter can always export and wipe their own data.

| Endpoint | Notes |
|---|---|
| `POST /api/v1/rides` | `{ started_at, ended_at, duration_s, distance_m, est_cost_cents?, rate_plan, started_in_zone, ended_in_zone, polyline }`. `polyline` is a Google encoded polyline (precision 5), validated at ingest. → the stored ride incl. `id`. |
| `GET /api/v1/rides?limit=50&before=<ISO>` | Owner-only, newest first. → `{ count, rides: [...] }` |
| `GET /api/v1/rides/export?format=geojson\|csv` | Owner-only full export. GeoJSON decodes each polyline to a `LineString`. |
| `DELETE /api/v1/rides/{id}` | **Immediate hard delete.** → `{ "deleted": true }` |
| `DELETE /api/v1/rides` | **Immediate hard delete of everything.** → `{ "deleted_count": n }` |

Privacy commitment, stated here on purpose: route polylines are the most
sensitive data this system holds. There is no soft-delete, no tombstone,
and no analytics use of ride routes — ever. See `/api/v1/meta/privacy`.

### `POST /webhooks/stripe`

Operator plumbing (not for frontend use): Stripe webhook with signature
verification. Handles `checkout.session.completed` (sets `supporter`,
keyed by `client_reference_id` = account id) and `charge.refunded`
(clears the flag on full refund only).

---

## Meta

### `GET /api/v1/meta/privacy`

Machine-readable retention policy — the frontend privacy page renders
this, so the published policy and the enforced one can't drift:

```json
{ "updated": "2026-07-04", "contact": "zneill@gmail.com",
  "retention": [ { "data": "sessions", "retention": "30 days idle", "detail": "…" } /* … */ ] }
```

---

## Layer reference

The eleven layers, their `region_type` values (used in `layer=` query
params), and the naming convention for `region_name` (used in the
trend endpoint and as the keys of `regions` in spatial-snapshot).

| `region_category` | `region_type` | # of regions | `region_name` examples |
|---|---|---|---|
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

- **v1 vs v2** are two distinct versions of the city's original Equity / Opportunity Areas polygon. Both exist because Denver's contract negotiations referenced both; the canonical compliance metric (`percent_all_devices_v1` on `/api/v1/snapshots/latest`) is computed against `v1` specifically, with `v2` tracked in parallel. They are not nested or disjoint — a device can be in both, neither, or one or the other.
- **`er1`–`er6`** are Denver DOTI's newer, authoritative census-block-group Equity Index, split into one layer per exact `EquityGroupRank` tier (`er1` = highest need, `er6` = lowest). Unlike v1/v2 they **partition** the scored area — every scored block group falls in exactly one `erN` layer, never two. They're tracked individually (not pre-combined into a cutoff) in both `/api/v1/snapshots/latest` and `/api/v1/compliance/daily/latest` so that whatever cutoff DOTI confirms as contractually authoritative can be reconstructed from history later (e.g. a "rank ≤ 2" metric = `er1 + er2`). **No individual `erN` layer is a confirmed compliance boundary today** — `percent_all_devices_v1` remains the primary RFP §3.0 metric. See API_REQUIREMENTS.md §1.1a.
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
| `400` | Bad query/body | Malformed `time`/`range` parameter, bad signature, unreadable receipt image. |
| `401` | Unauthenticated | Missing/invalid/expired bearer token, failed Google credential, dead magic link. Treat as signed out. |
| `403` | Forbidden | Valid session but missing scope (`admin`, `supporter`). |
| `404` | No data | Requested layer has no snapshots (cold start), or the resource isn't yours. |
| `413` | Too large | Receipt image over 10 MB. |
| `429` | Rate limited | POST buckets are full — honor the `Retry-After` header (seconds). |
| `502` | Upstream failure | Email provider rejected a magic-link send. Retry in a minute. |
| `503` | Service unavailable | No snapshots exist yet, or the feature isn't configured on this deployment (Google/magic-link/Stripe/receipts). |
| `5xx` (other) | Server error | Worker or Postgres failure. Logged in Sentry; transient — retry. |

Error responses are JSON: `{ "detail": "human-readable message" }`.

---

## Caching

The pipeline doesn't yet set explicit `Cache-Control` headers. In
practice you can safely cache responses for 30 s without missing fresh
data. If you're proxying through Cloudflare or another CDN, set the
edge cache TTL to ≤ 60 s.

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

- Source: <https://github.com/z280/veo-audit>
- Operator: <zneill@gmail.com>

If a metric looks wrong, include the `cycle_id` from
`/api/v1/snapshots/latest` in your report — that lets us trace it back
to the exact upstream GBFS payload and spatial-join inputs.
