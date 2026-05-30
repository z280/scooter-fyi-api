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

**None.** All endpoints listed in this document are public, read-only,
and unauthenticated. The `/admin/*` routes (not documented here) require
GitHub OAuth and are intended for operators only.

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
| **Device classification** | `form_factor` is one of `"bicycle"`, `"scooter"`, or `"unknown"`. The 22 core metrics count only `bicycle` and `scooter`; `unknown` devices are excluded from per-form-factor totals but included in `total_devices_*`. |
| **Spatial filtering** | Devices outside Denver's bounding envelope (e.g. China-factory glitches) are tagged but excluded from all citywide metrics. `total_not_in_denver` exposes the count of excluded devices. |

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

The full set of 22 citywide compliance metrics from the most recent
cycle. This is the most commonly consumed endpoint — it answers
"what's the fleet doing right now?"

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
  "percent_scooters_v2": 36.36
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
| `total_devices_denver` | int | All devices (bikes + scooters + unknown form factors) located inside Denver's bounding envelope. |
| `total_devices_v1` | int | Devices located inside the **Disadvantaged Areas v1** boundary. |
| `total_devices_v2` | int | Devices located inside the **Disadvantaged Areas v2** boundary. |
| `total_bike_denver` | int | `form_factor == "bicycle"` devices inside Denver. |
| `total_bike_v1` | int | Bicycles inside v1. |
| `total_bike_v2` | int | Bicycles inside v2. |
| `total_scooter_denver` | int | `form_factor == "scooter"` devices inside Denver. |
| `total_scooter_v1` | int | Scooters inside v1. |
| `total_scooter_v2` | int | Scooters inside v2. |
| `total_not_in_denver` | int | Devices reporting coordinates outside the Denver envelope (e.g. factory glitches, devices in transit). Excluded from all `*_denver`/`*_v1`/`*_v2` counts. |
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

---

### `GET /api/v1/spatial-snapshot`

Per-region device counts for a single layer, suitable for rendering a
choropleth map. Returns the latest available snapshot (or the snapshot
nearest to a given timestamp).

**Query parameters:**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `layer` | string | yes | — | One of `v1`, `v2`, `council_district`, `community_network`, `neighborhood`. See [Layer reference](#layer-reference). |
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

By default returns **only devices inside the Denver envelope**
(`spatial_status='denver_core'`) — China-factory glitches and other
outliers are hidden unless explicitly requested.

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
        "spatial_status": "denver_core"
      }
    },
    {
      "type": "Feature",
      "id": "abc124",
      "geometry": { "type": "Point", "coordinates": [-104.9851, 39.7411] },
      "properties": {
        "device_id": "abc124",
        "form_factor": "scooter",
        "spatial_status": "denver_core"
      }
    }
    /* … ~1,866 more features … */
  ]
}
```

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
  "computed_at": "2026-05-30T15:00:08+00:00"
}
```

**Response 503:** No daily row computed yet (first run pending, or pipeline just deployed).
```json
{ "detail": "no daily SLA rows computed yet" }
```

#### Field reference

| Field | Type | Description |
|---|---|---|
| `sla_date` | string (date) | Denver-local date the window covers (YYYY-MM-DD). |
| `window_start_ts` | string | 6:00 AM Denver expressed as UTC. |
| `window_end_ts` | string | 9:00 AM Denver expressed as UTC. |
| `snapshot_count` | int | Number of cycles whose `snapshot_time` fell inside the window. Typically 18 (3 hours × 6 cycles/hour). Lower values indicate cycle misses; 0 means no data. |
| `avg_*` fields | float \| null | Arithmetic mean of the corresponding `snapshot_metadata_core` field across all snapshots in the window. Null when `snapshot_count == 0`. |
| `compliance_v1_pass` | bool \| null | `avg_percent_all_devices_v1 >= 30`. The primary SLA boolean. Null when no data. |
| `compliance_v2_pass` | bool \| null | Same for v2. The contractually-binding map (v1 vs v2) is being confirmed with DOTI; track both for now. |
| `computed_at` | string | UTC timestamp of when this row was computed. |

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

## Layer reference

The five layers, their `region_type` values (used in `layer=` query
params), and the naming convention for `region_name` (used in the
trend endpoint and as the keys of `regions` in spatial-snapshot).

| `region_category` | `region_type` | # of regions | `region_name` examples |
|---|---|---|---|
| `disadvantaged_areas` | `v1` | 34 | `V1_001`, `V1_002`, … `V1_034` (ordinal, zero-padded to 3 digits) |
| `disadvantaged_areas` | `v2` | 65 | `V2_080010001001`, `V2_080010002003`, … (US Census Block Group GEOID20) |
| `council_districts` | `council_district` | 11 | `CD_1`, `CD_2`, … `CD_11` (Denver City Council district numbers) |
| `community_networks` | `community_network` | 13 | `CN_Central`, `CN_East`, `CN_EastCentral`, `CN_FarNortheast`, `CN_FarSoutheast`, `CN_North`, `CN_Northeast`, `CN_Northwest`, `CN_ParkHill`, `CN_SouthCentral`, `CN_Southeast`, `CN_Southwest`, `CN_West` |
| `neighborhoods` | `neighborhood` | 78 | `NB_AthmarPark`, `NB_Auraria`, `NB_Baker`, `NB_Barnum`, `NB_CBD`, `NB_CapitolHill`, `NB_CherryCreek`, `NB_FivePoints`, `NB_Highland`, `NB_SloanLake`, `NB_WashingtonPark`, `NB_Westwood`, … (Denver Statistical Neighborhood names with non-alphanumerics stripped) |

### Notes on the layers

- **v1 vs v2** are two distinct versions of the city's Equity / Opportunity Areas polygon. Both exist because Denver's contract negotiations referenced both; the canonical compliance metric is `percent_all_devices_v1`, but `v2` is tracked in parallel. They are not nested or disjoint — a device can be in both, neither, or one or the other.
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
| `400` | Bad query | Malformed `time` or `range` parameter. |
| `404` | No data | Requested layer has no snapshots (cold start only). |
| `503` | Service unavailable | No snapshots exist yet (very fresh deploy). Retry after a minute. |
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
