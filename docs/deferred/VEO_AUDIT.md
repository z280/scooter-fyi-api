# Veo GBFS Compliance Monitor

Polls Veo's GBFS feed every 15 minutes, counts how many vehicles are inside the
City & County of Denver's contractually-binding "Existing Bike and Scooter
Opportunity Areas" polygon, and appends a row to a CSV.

Used to track Veo's adherence to RFP §3.0:
> *"The Operator shall deploy 30% of the total vehicle fleet daily into Equity Areas."*

---

## Required files

| File | Current local path | Purpose | Action |
|---|---|---|---|
| `doti_existing_opportunity_areas.geojson` | `~/Downloads/doti_existing_opportunity_areas.geojson` | Contract polygon (1 MultiPolygon, 34 parts, 15.18 sq mi). Pulled from DOTI's published Feature Service. | Copy to server alongside the script. |
| `poll_gbfs.py` | inline below | The script. | Save to server. |
| `README.md` (this file) | `keepdenverfair/docs/VEO_AUDIT.md` | Reference. | Optional. |

### Polygon source of truth

The polygon was pulled directly from the city's published layer:

```none
https://services1.arcgis.com/zdB7qR0BtYrg0Xpl/arcgis/rest/services/Existing_Bike_and_Scooter_Opportunity_Areas/FeatureServer/0/query?where=1=1&outFields=*&returnGeometry=true&outSR=4326&f=geojson
```

ArcGIS v1: <https://www.arcgis.com/home/item.html?id=9e70d62c5a8345d4885cc5cbe16cd0a8>
Owner: `220181_geospatialDenver` (City and County of Denver, DOTI).

ArcGIS v2 <https://www.arcgis.com/home/item.html?id=af52d0dd532b4e328f7e5bbd1514978e>

---

## Required Python packages

**None.** The script uses only the standard library (`urllib`, `json`, `csv`,
`datetime`, `os`, `sys`). Tested on Python 3.8+. No `pip install` step needed.

If you want a ~10× speedup on point-in-polygon, optionally install Shapely —
the script auto-detects it and falls back to the pure-Python ray-cast if
absent.

```bash
pip install shapely     # optional, not required
```

---

## Layout on server

Recommended structure:

```none
/opt/veo-audit/
├── poll_gbfs.py
├── doti_existing_opportunity_areas.geojson
└── logs/
    ├── snapshots.csv          # one row per run, append-only
    └── poll.log               # stderr from each run, append-only
```

Adjust paths via env vars (see top of script).

---

## The script: `poll_gbfs.py`

```python
#!/usr/bin/env python3
"""
Poll the Veo GBFS feed for Denver, count vehicles inside the city's
contractually-binding Opportunity Areas polygon, and append one row to a CSV.

Usage:
    python3 poll_gbfs.py

Configuration via env vars (all optional):
    VEO_AUDIT_DIR        Directory containing this script + the geojson.
                         Default: directory of this script.
    VEO_AUDIT_POLY       Path to opportunity-areas GeoJSON.
                         Default: $VEO_AUDIT_DIR/doti_existing_opportunity_areas.geojson
    VEO_AUDIT_OUT        Output CSV path.
                         Default: $VEO_AUDIT_DIR/logs/snapshots.csv
    VEO_AUDIT_GBFS_URL   GBFS free_bike_status endpoint. Override only if Veo
                         changes the URL. Default: Denver Veo prod.
    VEO_AUDIT_TIMEOUT    HTTP timeout in seconds. Default: 30.

Exit codes:
    0  Snapshot logged.
    1  Network/parse error (no row written). Safe to retry next interval.
    2  Configuration error (missing polygon file, etc.). Will not self-heal.
"""

import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

# --- Optional shapely for speed -------------------------------------------------
try:
    from shapely.geometry import shape, Point
    from shapely.strtree import STRtree
    HAVE_SHAPELY = True
except ImportError:
    HAVE_SHAPELY = False

# --- Config ---------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("VEO_AUDIT_DIR", HERE)
POLY_PATH = os.environ.get(
    "VEO_AUDIT_POLY",
    os.path.join(ROOT, "doti_existing_opportunity_areas.geojson"),
)
OUT_PATH = os.environ.get(
    "VEO_AUDIT_OUT",
    os.path.join(ROOT, "logs", "snapshots.csv"),
)
GBFS_URL = os.environ.get(
    "VEO_AUDIT_GBFS_URL",
    "https://cluster-prod.veoride.com/api/shares/name/den/gbfs/free_bike_status",
)
TIMEOUT = int(os.environ.get("VEO_AUDIT_TIMEOUT", "30"))

# Denver bounding box — drops the ~16 garbage-coord vehicles (~22°N 114°E, etc.)
DENVER_BBOX = (-105.3, 39.5, -104.7, 40.0)  # (min_lon, min_lat, max_lon, max_lat)

# Contract requirement (RFP §3.0)
CONTRACT_OA_PCT = 30.0

CSV_FIELDS = [
    "ts_utc",
    "fleet_total",
    "fleet_in_denver",
    "scooters_total", "scooters_in_oa", "scooters_pct_in_oa",
    "ebikes_total",   "ebikes_in_oa",   "ebikes_pct_in_oa",
    "all_in_oa",      "all_pct_in_oa",
    "reserved_count", "disabled_count",
    "ok",
]

# --- Pure-stdlib point-in-polygon (fallback when no shapely) --------------------
def _in_ring(x, y, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-18) + xi):
            inside = not inside
        j = i
    return inside

def _point_in_poly(x, y, rings):
    if not rings or not _in_ring(x, y, rings[0]):
        return False
    for hole in rings[1:]:
        if _in_ring(x, y, hole):
            return False
    return True

def _build_index_stdlib(geojson):
    """Return list of (bbox, [outer_ring, ...holes]) for every polygon part."""
    out = []
    for feat in geojson["features"]:
        g = feat["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        for poly in polys:
            xs, ys = [], []
            for ring in poly:
                for x, y in ring:
                    xs.append(x); ys.append(y)
            out.append(((min(xs), min(ys), max(xs), max(ys)), poly))
    return out

def _contains_stdlib(index, lon, lat):
    for (minx, miny, maxx, maxy), rings in index:
        if lon < minx or lon > maxx or lat < miny or lat > maxy:
            continue
        if _point_in_poly(lon, lat, rings):
            return True
    return False

# --- Shapely-backed (if available) ----------------------------------------------
def _build_index_shapely(geojson):
    geoms = [shape(f["geometry"]) for f in geojson["features"]]
    return (STRtree(geoms), geoms)

def _contains_shapely(index, lon, lat):
    tree, geoms = index
    pt = Point(lon, lat)
    for i in tree.query(pt):
        if geoms[i].contains(pt):
            return True
    return False

# --- Main -----------------------------------------------------------------------
def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", file=sys.stderr)

def fetch_gbfs():
    req = urllib.request.Request(GBFS_URL, headers={"User-Agent": "veo-audit/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())

def main():
    if not os.path.exists(POLY_PATH):
        log(f"FATAL: polygon file not found: {POLY_PATH}")
        return 2

    with open(POLY_PATH) as f:
        poly_gj = json.load(f)

    if HAVE_SHAPELY:
        index = _build_index_shapely(poly_gj)
        contains = _contains_shapely
    else:
        index = _build_index_stdlib(poly_gj)
        contains = _contains_stdlib

    try:
        feed = fetch_gbfs()
    except Exception as e:
        log(f"GBFS fetch failed: {e!r}")
        return 1

    ts_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    bikes = feed.get("data", {}).get("bikes", [])
    fleet_total = len(bikes)

    # Drop garbage coords
    min_lon, min_lat, max_lon, max_lat = DENVER_BBOX
    bikes = [b for b in bikes
             if min_lat < b.get("lat", 0) < max_lat
             and min_lon < b.get("lon", 0) < max_lon]
    fleet_in_denver = len(bikes)

    scooters_total = ebikes_total = 0
    scooters_in_oa = ebikes_in_oa = 0
    reserved = disabled = 0

    for b in bikes:
        vt = b.get("vehicle_type_id")
        in_oa = contains(index, b["lon"], b["lat"])
        if b.get("is_reserved"): reserved += 1
        if b.get("is_disabled"): disabled += 1
        if vt == "1":  # stand-up scooter
            scooters_total += 1
            if in_oa: scooters_in_oa += 1
        elif vt == "3":  # e-bike
            ebikes_total += 1
            if in_oa: ebikes_in_oa += 1
        # other types (0=human bike, 2=e-assist bike) intentionally ignored;
        # add columns if they ever appear in the feed.

    def pct(n, d):
        return round(n / d * 100, 2) if d else 0.0

    all_total = scooters_total + ebikes_total
    all_in_oa = scooters_in_oa + ebikes_in_oa

    row = {
        "ts_utc": ts_utc,
        "fleet_total": fleet_total,
        "fleet_in_denver": fleet_in_denver,
        "scooters_total": scooters_total,
        "scooters_in_oa": scooters_in_oa,
        "scooters_pct_in_oa": pct(scooters_in_oa, scooters_total),
        "ebikes_total": ebikes_total,
        "ebikes_in_oa": ebikes_in_oa,
        "ebikes_pct_in_oa": pct(ebikes_in_oa, ebikes_total),
        "all_in_oa": all_in_oa,
        "all_pct_in_oa": pct(all_in_oa, all_total),
        "reserved_count": reserved,
        "disabled_count": disabled,
        "ok": 1,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    new_file = not os.path.exists(OUT_PATH)
    with open(OUT_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)

    log(f"OK fleet={all_total} in_oa={all_in_oa} ({row['all_pct_in_oa']}%) "
        f"contract={CONTRACT_OA_PCT}% gap={round(row['all_pct_in_oa']-CONTRACT_OA_PCT, 1)}pt "
        f"shapely={HAVE_SHAPELY}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

---

## Install on the server

```bash
# 1. Lay out the directory
sudo mkdir -p /opt/veo-audit/logs
sudo chown -R "$USER":"$USER" /opt/veo-audit

# 2. Copy files
scp poll_gbfs.py                              user@server:/opt/veo-audit/
scp doti_existing_opportunity_areas.geojson   user@server:/opt/veo-audit/

# 3. Smoke-test
cd /opt/veo-audit && python3 poll_gbfs.py
cat logs/snapshots.csv
```

Expected output on a healthy run:

```
[2026-05-21T22:00:00+00:00] OK fleet=5713 in_oa=1229 (21.5%) contract=30.0% gap=-8.5pt shapely=False
```

---

## Cron / scheduler

### Linux cron (every 15 min)

```cron
*/15 * * * * cd /opt/veo-audit && /usr/bin/python3 poll_gbfs.py >> logs/poll.log 2>&1
```

### systemd timer (preferred on modern Linux)

`/etc/systemd/system/veo-audit.service`:

```ini
[Unit]
Description=Veo GBFS compliance snapshot
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/veo-audit
ExecStart=/usr/bin/python3 /opt/veo-audit/poll_gbfs.py
StandardOutput=append:/opt/veo-audit/logs/poll.log
StandardError=append:/opt/veo-audit/logs/poll.log
User=veo
```

`/etc/systemd/system/veo-audit.timer`:

```ini
[Unit]
Description=Run Veo GBFS snapshot every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now veo-audit.timer
systemctl list-timers | grep veo
```

### macOS launchd (if running locally)

`~/Library/LaunchAgents/com.local.veo-audit.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.local.veo-audit</string>
  <key>ProgramArguments</key>
    <array>
      <string>/usr/bin/python3</string>
      <string>/Users/neill/veo-audit/poll_gbfs.py</string>
    </array>
  <key>WorkingDirectory</key><string>/Users/neill/veo-audit</string>
  <key>StartInterval</key><integer>900</integer>
  <key>StandardOutPath</key><string>/Users/neill/veo-audit/logs/poll.log</string>
  <key>StandardErrorPath</key><string>/Users/neill/veo-audit/logs/poll.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.local.veo-audit.plist
```

---

## Output schema (`snapshots.csv`)

| Column | Type | Notes |
|---|---|---|
| `ts_utc` | ISO 8601 | UTC timestamp of the snapshot. Convert to MT for daily aggregation. |
| `fleet_total` | int | Total `bikes` rows in feed before bbox filter. |
| `fleet_in_denver` | int | After dropping ~22°N/114°E garbage coords. Should equal fleet_total ± a few. |
| `scooters_total` | int | `vehicle_type_id == "1"` (stand-up electric). |
| `scooters_in_oa` | int | …inside the Opportunity Areas multipolygon. |
| `scooters_pct_in_oa` | float | 2 decimal places. |
| `ebikes_total` | int | `vehicle_type_id == "3"` (electric bicycle). |
| `ebikes_in_oa` | int |  |
| `ebikes_pct_in_oa` | float |  |
| `all_in_oa` | int | scooters + ebikes inside OA. |
| `all_pct_in_oa` | float | **The compliance metric. Contract requires 30%.** |
| `reserved_count` | int | `is_reserved == true`. Useful for utilization. |
| `disabled_count` | int | `is_disabled == true`. Useful for response-time audits. |
| `ok` | 0/1 | Reserved for future error rows. Currently always 1. |

---

## Compliance baseline (as of 2026-05-21 single snapshot)

```
fleet_total           5,714
fleet_in_denver       5,713
scooters_total        1,866    (32.7% of fleet — stand-up cap is ≤50%, OK)
ebikes_total          3,847
all_in_oa             1,229
all_pct_in_oa         21.5%    (contract: 30%, gap: -8.5pt)
```

**Note: 45-day SLA grace period from launch (May 16, 2026) runs through
~July 1, 2026.** Snapshots before then establish baseline but are not
enforceable as violations.

---

## Sanity checks

- If `fleet_in_denver` ever drops below ~3,000 outside of brief outages, the
  feed schema may have changed — inspect a raw response.
- If `all_pct_in_oa` is consistently `0`, the polygon path is wrong or the
  GeoJSON didn't load — check `poll.log`.
- If the script never writes to CSV but cron says it ran, check file
  permissions on `logs/` and that the user has write access.

---

## Useful follow-ups (not in this script)

- **Daily aggregation**: a separate `daily_summary.py` that groups
  `snapshots.csv` by Denver-local date and outputs daily min/median/max % in
  OA. The contract says "daily" deployment — a single 15-min reading isn't
  the legal metric, the daily aggregate is.
- **Per-vehicle raw archive**: optionally gzip the full bike list every Nth
  snapshot (4 / hour is plenty) so you can rebuild any cross-time analysis
  later. ~70KB gzipped per snapshot → ~2.5 GB / year.
- **MDS trip-end pull**: DOTI publishes monthly trip-ends as a Feature
  Service. The August 2025 layer (item id `a93ffbe13f334de8b2a1c578357296f6`)
  has 781,268 points. Pull each month as it's published for a trip-based
  compliance check.
- **Equity-Area pricing**: GBFS `system_pricing_plans` shows only two flat
  $1+$0.39/min plans. The RFP requires a "fare discount for any trip that
  starts or ends within a designated Equity Area." Worth verifying in the
  Veo app directly; not auditable from public feeds.

---

## Source references

- RFP — `~/Downloads/RFP_Shared Bike Scooter Program_Final.pdf`, p. 8
  (Equitable Geographic Deployment, 30% requirement)
- Contract — `~/Downloads/26-0326 Agreement_VEORIDE INC. 202582850-00.pdf`,
  §2.1 (SLA grace period, penalties), §3.2 (fleet caps)
- Polygon layer — <https://www.arcgis.com/home/item.html?id=9e70d62c5a8345d4885cc5cbe16cd0a8>
- GBFS feed root — <https://cluster-prod.veoride.com/api/shares/name/den/gbfs>
