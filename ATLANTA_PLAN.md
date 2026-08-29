# Atlanta — what it would actually take

Status: **assessment**, nothing here is built. Written against `main` at
`sql/079`. Parent doc: [`MULTI_TENANCY_PLAN.md`](MULTI_TENANCY_PLAN.md),
whose three-axis framing (city / provider / deployment) this doc assumes.

Atlanta was proposed with two operator feeds — Bird and Lime. Both were
probed live on 2026-08-29; every number below is measured, not estimated.
Where a number is *not* measured it says so.

---

## 0. The short answer

Atlanta is a **better** city #2 than the multi-tenancy plan assumed, and a
**worse** one than the feeds make it look.

Better, because Denver's Lime/Bird/Lyft probe (`MULTI_TENANCY_PLAN.md` §7b)
found 3 vehicles, 0 vehicles and an abandoned feed. Atlanta has **5,822 live
vehicles across two live operators** — 72% of Denver Veo's 8,084 — with
working `vehicle_types`, live `last_reported`, and in Bird's case a real
`current_fuel_percent` and 65 live geofencing polygons. The City of Atlanta
also publishes, from its own GIS, every boundary layer this pipeline
consumes: city limits, council districts, neighborhoods, NPUs, address
points, and a transportation-equity layer that is a near-exact structural
analogue of Denver's Equity Areas.

Worse, because **neither operator publishes a usable stable vehicle
identifier**, and this is measured rather than assumed (§2). Bird re-mints
every `bike_id` on every ~60 s feed generation. Lime's ids look stable —
~99% persist across cycles, and one was observed surviving a 913 m trip —
but Lime re-mints its **entire** namespace periodically: 2,929 ids replaced
at once while 2,830 of 2,929 coordinates stayed byte-identical. Lime is the
more dangerous of the two, because an adapter that trusts it produces
thousands of phantom vehicles and phantom trips at each rotation instead of
an obviously empty feature.

That is the same wall `MULTI_TENANCY_PLAN.md` §7c drew, and it lands in the
same place: Atlanta gets the fleet-analytics half and none of the
per-vehicle half.

But the ride-mode conclusion is **not** the obvious one. `api_rides.py`'s
off-feed ride path needs no vehicle identity at all, and it already carries
routing, the nav HUD, track donation, distance and export. Atlanta can have
ride mode; what it loses is the *anchor* — "you're on *that* scooter"
becomes "you told us you're on a Bird". The one real gap is that off-feed rides
award no points (§2b), which is a Denver bug as much as an Atlanta blocker.

And the fleet-analytics half is not a consolation prize. Run against
Atlanta's own equity layer it already says something: **1.2% of Bird's
in-city fleet and 10.3% of Lime's sit in a Community of Concern, against
COCs being 16.6% of the city's land area** (§3c-i). That is available on day
one with no per-vehicle identity whatsoever — and it must ship as
*distribution*, never as compliance, because unlike Denver there is no
contract behind it (§3c).

There is no getting to any of it without Phases 1 and 2 of the parent plan.
Those are unbuilt, both sized **L**, and Atlanta is Phase 4. Nothing here
shortens that.

---

## 1. What the feeds actually contain

Probed 2026-08-29 ~11:37–11:42 UTC. Both discovery documents resolve and
every advertised sub-feed answers 200.

| | Bird Atlanta | Lime Atlanta |
|---|---|---|
| Discovery | `mds.bird.co/gbfs/v2/public/atlanta/gbfs.json` | `data.lime.bike/api/partners/v2/gbfs/atlanta/gbfs.json` |
| `system_id` | `bird-atlanta` | `lime_atlanta` |
| GBFS version | 2.3 | 2.2 |
| Vehicles | **2,896** | **2,926** |
| Scooter / bike split | 2,709 scooter / 187 e-bike | 2,222 scooter / 704 e-bike |
| `ttl` | 60 | 60 |
| `last_reported` per vehicle | yes | yes |
| Battery | **`current_fuel_percent`** (0–1 float) | none — range only |
| `current_range_meters` | float | int |
| `is_disabled` / `is_reserved` | proper bools | proper bools |
| `rental_uris` | **absent** | **absent** |
| `system_pricing_plans` | advertised, **`plans: []`** | **404, not advertised** |
| `geofencing_zones` | **65 live MultiPolygons** | not advertised |
| `station_information` | `stations: []` | 1 junk station |
| Timezone declared | `US/Eastern` | `America/New_York` |
| CORS | open (reflects Origin) | **closed** |

Both fleets are metro-wide, not municipal — Lime especially:

```
Bird  bbox  lat 33.6817..33.8736   lon -84.5063..-84.2975
Lime  bbox  lat 33.5610..33.9057   lon -84.5342..-84.1984
```

Lime's envelope runs from south of Hartsfield-Jackson (33.561) up to
Sandy Springs (33.906) — roughly 38 km of latitude, against Bird's ~21 km.

**The bounding boxes are misleading, though, and it is worth saying so
because the Denver-Lime precedent predicts the opposite.** Point-in-polygon
against the City of Atlanta's own limits layer (136.27 sq mi, retrieved
2026-08-29):

| | inside city limits | outside |
|---|---|---|
| Bird | 2,894 / 2,896 — **99.9%** | 2 |
| Lime | 2,876 / 2,926 — **98.3%** | 50 |

So both fleets are effectively municipal, not metro-wide. Lime's wide bbox
is 50 stragglers, not a distribution. `MULTI_TENANCY_PLAN.md` §7d warned to
"expect a much higher `other_outlier` share for metro-wide operators" based
on Denver-Lime, where 2 of 3 vehicles were outside the city — that
generalised from a 3-vehicle sample and does not hold here. Atlanta's
`other_outlier` share will look like Denver-Veo's, not like Denver-Lime's,
and the citywide denominator is well-behaved.

`_refine_spatial_status`'s buffered-polygon pass still does the right thing
with the 52 that are out; it just has far less work to do than expected.

### 1a. Divergences a `GbfsAdapter` must absorb

All observed, none hypothetical:

- **Bird's `current_fuel_percent` is a 0–1 float**, not a percentage. It is
  a genuine state-of-charge reading, so Bird is the **first provider that
  does not need `quality.py`'s reverse-engineered Veo SoC curve** — it
  needs the curve bypassed. The `battery_percent()` adapter hook in the
  parent plan's §7 protocol is exactly right; Bird returns
  `current_fuel_percent * 100` and Lime falls back to
  `current_range_meters / max_range_for_type`.
- **Lime carries a redundant `vehicle_type` string** (`"scooter"`) beside
  `vehicle_type_id`. Trust the id and the registry, not the string.
- **Lime's `vehicle_types` declares 4 types, 2 are in use.** Types `1`
  (24 km scooter) and `4` (human-powered bike) have zero vehicles. Do not
  infer the fleet's composition from the registry.
- **Lime's `station_information` is one junk station** — `station_id:
  "atlanta"`, named "Atlanta", at 33.8133,-84.3066 — 10.4 km north-east of
  downtown, and not a station. Same genre as Denver-Lime's Castle Rock
  station and Veo's phantom 67,000 m `max_range_meters`. Ignore Lime
  stations entirely.
- **Bird's `max_range_meters` is plausible** (24 km scooter, 60 km e-bike)
  but unverified in the field. `_KNOWN_VEHICLE_TYPES` is a
  corrections layer built by standing next to a scooter
  (`MULTI_TENANCY_PLAN.md` §11.5); Atlanta starts with an empty one for
  both operators and no way to fill it remotely.
- **Bird's geofencing zones carry no `name` and no `maximum_speed_kph`** —
  only `ride_allowed` / `ride_through_allowed`. 21 zones are hard no-go,
  41 are no-park/ride-through, 3 are permissive. Useful as a map overlay
  and as Valhalla avoid-polygons; useless as labelled place data.

---

## 2. Vehicle identity — measured, and the answer is no

Neither operator publishes `rental_uris`, so there is no plate, so
`identity.hash_plate()` has no input. That was expected. What was not known
is whether `bike_id` itself is usable, so it was measured over a ~19-minute
window on 2026-08-29 (8 snapshots per operator, ~150 s apart).

**Bird re-mints `bike_id` on every feed generation.** Two polls 191 s apart:

```
t0 = 2,896 vehicles      t1 = 2,897 vehicles
persisted ids: 0  (0.0%)     gone: 2,896      new: 2,897
```

Zero — not partial rotation, total. A control pair fetched 8 s apart inside
one `ttl=60` window, sharing an identical `last_updated`, returned **2,899
of 2,899 ids identical and 2,899 of 2,899 coordinates identical**. So Bird's
ids are stable within a cached generation and re-minted every ~60 s. No
cross-cycle identity, and no partial signal to salvage.

**Lime looked promising and then failed the same way, more slowly.** Across
cycles Lime's ids persist — six of the seven observed steps kept ~99% of
them. Ids shared between consecutive snapshots, both operators, same window:

```
          LIME                    BIRD
step   gap   ids shared        gap   ids shared
  1    209s        2,906       191s           0   <- re-mint
  2     89s        2,920        62s           0   <- re-mint
  3    151s        2,914       136s           0   <- re-mint
  4    151s            0  <--- 162s           0   <- re-mint
  5    152s        2,896       180s           0   <- re-mint
  6    152s        2,898       166s           0   <- re-mint
  7    152s        2,893       142s           0   <- re-mint
                    ^
                    Lime's one global re-mint in the window
```

Bird re-mints at **every** boundary, 7 for 7. Lime re-mints once in ~19
minutes and holds steady across the other six steps.

Two things happened in that window, and they point opposite ways.

**The good one: Lime's id survives a trip.** One id disappeared at L4,
returned at L5 as the same id, **913 m from where it was last seen** and
with `current_range_meters` down from 3,313 to 2,767. That is a completed
rental with a preserved identifier — the exact test GBFS 2.2's
"`bike_id` SHOULD rotate per trip" says should fail, and it passed.

**The fatal one: Lime re-mints the entire namespace at once.** At L2→L3 all
2,929 ids were replaced while **2,830 of 2,929 coordinates stayed byte-
identical**. Same physical fleet, wholly new keys. Both id sets are
well-formed UUIDv4; nothing distinguishes an epoch boundary from the outside
except that every key changes simultaneously.

One rotation was observed in ~19 minutes of continuous polling, with four
consecutive steady steps after it, so the period is bounded above at roughly
that and is otherwise unpinned. It does not matter much whether it is 20
minutes or 6 hours — see below.

**Why the re-mint is worse than having no id at all.** An adapter that trusts
Lime's `bike_id` does not degrade gracefully at an epoch boundary — it
hallucinates. At every rotation `device_state` would see ~2,900 vehicles
disappear and ~2,900 brand-new ones appear at the same coordinates, which
means:

- ~2,900 phantom trip-end events and ~2,900 phantom first-sightings per
  rotation, straight into `trip_events` and the daily rollups;
- `device_history` fragmented into epoch-length shards, so dwell,
  reliability tiers and failed-start detection all reset on a timer;
- the vehicle registry growing without bound — thousands of new
  `vehicle_identifier`s per day, each with minutes of history;
- every per-device report, photo and QR binding orphaned at the next
  rotation.

Bird's honest rotation produces an obviously empty feature. Lime's produces
a plausible-looking one that is quietly wrong, which is the more expensive
failure. **Treat both as `stable_vehicle_id: false`** and do not special-case
Lime on the strength of the 99% steady-state number.

**The one thing worth re-testing** is whether the epoch boundary is
detectable and whether identity is recoverable across it — a global re-mint
with 96.6% coordinate stability is, in principle, re-linkable by position at
the boundary alone. That is a research question, not a launch dependency,
and §2c's warning about position-matching applies to any answer it produces.

### 2a. What this costs, concretely

`MULTI_TENANCY_PLAN.md` §7c already drew this line and it holds exactly:

**Works in Atlanta, both operators, today** — `compute.py`'s DuckDB spatial
join, the `snapshot_metadata_core` metrics, `regional_metrics_narrow`,
`api_h3` aggregates, `equity-estimate`, `spatial-snapshot`,
`analytics/trend`, the boundary overlays, and the whole daily rollup family.
This is the project's origin purpose and it is a counting exercise over
anonymous points.

**Cannot exist in Atlanta without a stable id** — `device_state`,
`device_history`, dwell, reliability tiers, failed-start detection,
`trip_events` and both daily trip rollups, `ride_watch` / `tracked_rides`,
dibs, QR scans, device photos, per-device reports, device features,
recommendations, and the battery model — plus, as currently wired, the
points ledger, because every points hook hangs off `tracked_rides`. That
last one is the only entry on this list that is a wiring accident rather
than a data limit; see §2b.

That second list is not a footnote. It is most of what shipped in the last
year — dibs (`sql/076`), ride mode (`PLAN_RIDE_MODE_API.md`), device
features (`sql/055`), battery (`sql/070`–`071`).

The parent plan's `stable_vehicle_id` capability flag (§7a) is what makes
this shippable rather than embarrassing: one flag gates the second list, the
frontend hides those surfaces wholesale, and the UI says why. The
alternative — a map where every popup is empty — is worse than not shipping
Atlanta. In the frontend that flag reaches **37 of 101 non-test modules**
(`ride-*`, `dibs*`, `qr-*`, `device-*`, `reports`, `recommend`, `track-*`),
which is the real size of item 4 in §5.

### 2b. Ride mode is not actually lost — and this is the important finding

The instinct is that no stable id means no ride mode. That is wrong, and the
codebase already says so. There are **two** ride mechanisms:

- `api_tracked_rides.py` — server-detected, anchored to a GBFS vehicle by
  `vehicle_identifier`. Needs identity. Dead in Atlanta.
- `api_rides.py` — **off-feed rides**, for "vehicles the audit does not
  track… a personal scooter, a competitor's rental, a friend's e-bike."
  The rider describes the vehicle (`vehicle_kind` + free-text operator)
  instead of it being detected. **Needs no identity at all.**

Everything a rider experiences during a ride — destination search, route
selection, the nav HUD, the trail, track donation and verification,
server-side distance from the track, export — hangs off the second path or
off nothing device-specific. So an Atlanta rider can plan a route, ride it,
record it, and keep it. What they lose is the *anchor*: the app can't say
"you're on *that* scooter", only "you told us you're on a Bird".

There is exactly one gap, and it is small and named: **off-feed rides award
no points.** `points.py:38` says so outright — `_RIDE_SOURCE_TABLES` lists
`rides`, but "off-feed rides award nothing today (src/api_rides.py awards no
points)". Every points hook is wired to `tracked_rides`. So in Atlanta today
riders would ride, and the ledger would stay at zero, and the whole
progression/lexicon/royalty layer would be inert.

**Extending the points ledger to off-feed rides is therefore the single
highest-leverage piece of Atlanta work**, and it is a Denver feature too —
Denver riders on a competitor's scooter have the same dead ledger. It is
`M`-sized, it needs no multi-tenancy, and it converts Atlanta from "half the
product" into "most of the product minus per-vehicle intelligence." Do it
before Phase 4, not during.

What stays genuinely gone in Atlanta, with no off-feed equivalent: dibs
(you cannot call dibs on a vehicle you cannot name), QR scan bonuses, device
photos, device features, per-device reports and recommendations, dwell and
reliability tiers, failed-start detection, and the battery model.

### 2c. Do not try to reconstruct identity by position

Bird's ids rotate but its coordinates are stable across a generation, so
nearest-neighbour matching between cycles is superficially tempting. Don't:
it is inference presented as identity, it fails precisely when a vehicle
moves (the only case that matters), and a wrong match silently merges two
vehicles' history, reports and photos — the same corruption
`MULTI_TENANCY_PLAN.md` §5 warns about from hash collisions, arrived at
deliberately instead of by accident. If per-vehicle intelligence in Atlanta
ever matters, the route is an MDS agreement with ATLDOT, not GBFS scraping.

---

## 3. The city axis: Atlanta publishes everything needed

This is the pleasant surprise. `gis.atlantaga.gov/dpcd/rest/services/
OpenDataService1/FeatureServer` carries, on one service:

| Layer | Atlanta | Denver equivalent |
|---|---|---|
| 1 | Atlanta City Limits | union of `NB.geojson` (see §3a) |
| 2 | City Council Districts | `CD.geojson` |
| 3 | Neighborhoods | `NB.geojson` |
| 4 | NPU (25 Neighborhood Planning Units) | `CN.geojson` community networks |
| 0 | SiteAddressPoint | `sql/074` Denver address points |
| 9/30/32 | BicycleFacilities / Bicycle Routes / MultiuseTrails | the bikeway sidecar (`refresh_routing_graph`) |

Plus `GIS_CompositeLocator_2024` and `SiteAddressPoint` GeocodeServers.

`refresh_address_points` already speaks ArcGIS pagination against Denver's
open-data service, so Atlanta's address index is a config change and a
field-mapping, not new machinery.

### 3a. City limits are published directly — better than Denver

`compute.py:_refine_spatial_status` builds Denver's precise polygon by
**unioning every neighborhood polygon**, because Denver's NB layer happens
to tile the city exactly. Atlanta publishes City Limits as its own layer, so
Atlanta should use it directly. That means the "build the city polygon"
step must become per-city strategy (`union_of_layer` vs `explicit_layer`),
not just a per-city file.

### 3b. The hardcoded UTM zone

`compute.py` projects to **`EPSG:26913` (UTM 13N)** to buffer the city
polygon in metres. Atlanta is UTM 16N — **`EPSG:26916`**. This is a
two-line hardcode the parent plan does not mention, and it fails silently
in the worst way: a wrong zone still projects, still buffers, and produces
a subtly wrong boundary rather than an error. It needs to be a `cities` row
column (`projected_srid`), and it needs a test that asserts the buffer
distance round-trips to the requested metres.

### 3c. Equity: Atlanta has a layer, but it has no contract

Atlanta's **Communities of Concern (COC) 2025** is a genuine structural
analogue: 15 Neighborhood Statistical Areas from a City transportation-equity
analysis, tiered **Tier 1 (10, persistent risk) / Tier 2 (5, improving)**,
each carrying component scores for vehicle access, transit commuting,
poverty, disability, age and single-parent households, plus a
`CombinedScore`. `COC_Tier` maps onto `equity_groups.TRACKED_GROUPS` the
way `er1..er6` do, and it is *transportation* equity, which is closer to
the point than Denver's general index.

Retrieved to `data/atlanta_communities_of_concern_2025.geojson`.

**But there is no §3.0 here.** Denver's 30% threshold is a term in Veo's
contract with the city; it is why `daily_sla_compliance` and every
`compliance_<g>_pass` column exist. Atlanta's COC layer is a *planning*
product. Whether ATLDOT's shared-mobility permit imposes any distribution
obligation on Bird or Lime is **unknown and not answerable from open data** —
it needs the permit document read by a person.

Until then Atlanta can compute and publish *distribution* but **must not**
render a pass/fail, a gauge against a threshold, or the word "compliance".
Inventing a threshold would be the exact failure mode
`MULTI_TENANCY_PLAN.md` §8b calls the rack-rate trap: a number that is
citable because it is convenient, not because it binds anyone.
`COMPLIANCE_GROUPS` for Atlanta is the empty tuple, and that has to be
expressible.

### 3c-i. What the distribution actually is, right now

The point of §2a is that the fleet-analytics half of the pipeline works in
Atlanta today. Here is it working — one instant, 2026-08-29, in-city
vehicles only, point-in-polygon against the two committed layers:

| | fleet in city | in Tier 1 COC | in any COC |
|---|---:|---:|---:|
| **Bird** | 2,894 | 32 — **1.1%** | 34 — **1.2%** |
| **Lime** | 2,876 | 222 — **7.7%** | 296 — **10.3%** |
| *Communities of Concern, share of city land area* | | *8.3%* | ***16.6%*** |

Against a land-area denominator both operators under-serve Atlanta's
Communities of Concern, and **Bird does so by roughly fourteen-fold** —
1.2% of its fleet in 16.6% of the city. Lime is at 0.62× parity. This is the
kind of finding the project exists to surface, and it is available on day
one of an Atlanta build with no per-vehicle identity of any kind.

Three caveats, all load-bearing, and all of which have to ship beside the
number if it is ever rendered:

- **Land area is a defensible denominator but not the only one.** Population
  (the COC layer carries 66,956 across the 15 NSAs), jobs, or trip demand
  would each give a different parity line. Denver's §3.0 uses a flat
  percentage-of-fleet target precisely to avoid this argument; Atlanta has
  no such number to hide behind, so whichever denominator is used has to be
  named on the same screen.
- **It is one snapshot**, not a series. Denver's SLA averages a 6–9 am
  window over a day for good reason — a single frame is a photograph of
  rebalancing mid-cycle, and the morning distribution is the one that
  matters for access to work.
- **Nobody is obliged to anything.** Absent a permit term (§3c), this is a
  description of where scooters are, not a finding that anyone is out of
  compliance. Say "under-represented relative to land area", never
  "non-compliant".

### 3d. Pricing: worse than Denver

Denver's §8b trap was "the rates you can cite are the ones nobody paid."
Atlanta is worse — **there are no rates to cite at all.** Bird advertises
`system_pricing_plans` and returns `{"plans": []}`. Lime 404s and does not
advertise it. So there is no cost HUD, no ride-cost estimate, and no
operator price comparison in Atlanta from open data. `known_pricing` is
false for both operators, and the parent plan's move of `RATE_PLANS` out of
`config.ts` and into `provider_rate_plans` (§7e) is a **prerequisite** for
Atlanta rather than a nice-to-have: the frontend currently hardcodes Veo's
Denver plans, and an Atlanta build that inherits them would display Denver
prices for Atlanta scooters.

---

## 4. Infrastructure: the actual wall

Same wall as `MULTI_TENANCY_PLAN.md` §8, now with Atlanta's numbers.

**Routing and geocoding.** Valhalla (3.0 GiB cap, Denver-clipped graph) and
Photon (2.0 GiB, Colorado-scoped index) are ~5 GiB of a 12 GiB box whose
declared ceilings already total ~9.6 GiB. Metro Atlanta's OSM extract is
comparable to Denver's, so option 1 from §8 — one combined graph over both
metros, `graph_bbox` becoming a per-city list — is plausible and is the
thing to **measure before committing**, by building a Denver+Atlanta `.pbf`
and looking at steady-state RSS. Photon likewise needs its index widened to
Colorado ∪ Georgia. If it does not fit, §8 option 2 (routing off the box)
is the answer and it costs money. This is the single largest unknown in
the whole plan and it is not a software question.

**Ingest volume.** Denver Veo is 8,084 vehicles at 2 min ≈ 5.8 M
`raw_telemetry_points` rows/day. Atlanta's 5,822 at the same cadence adds
~4.2 M/day — a 72% increase on a table already cut to a 24 h archive window
because 48 h at 12.5 M rows crowded the archive job's memory ceiling.

Atlanta does not need 2 min. The 2-minute cadence exists to resolve trip
durations to ±2 min for the burn-rate work — and Atlanta has no trips,
because it has no stable ids. At **5 min** Atlanta costs ~1.7 M rows/day;
at 10 min, ~840 k. Per-feed `cycle_minutes` is already in the parent plan's
`feeds` table; Atlanta should launch at 5 and this is a genuine saving, not
a compromise. Partitioning `raw_telemetry_points` by `feed_id` and making
the archive job handle one feed per invocation (§4d) both become required.

**Basemap.** `scripts/build-basemap.sh` is already parameterised on a bbox
and produces a 21 MiB PMTiles extract under the Pages 25 MiB limit. Atlanta
is a bbox change and an R2 upload. Genuinely easy.

**Timezone.** Both operators declare US/Eastern, so Atlanta is one zone and
the cron fan-out §6 describes (run rollups hourly, no-op for cities whose
local midnight has not passed) is needed but not stressed — two zones, two
hours apart.

---

## 5. What Phase 4 actually costs, in order

Phases 1–3 of `MULTI_TENANCY_PLAN.md` are prerequisites and unchanged. What
Atlanta adds on top:

| # | Work | Size |
|---|---|---|
| 1 | `cities`/`feeds` rows for Atlanta ×2 operators; `projected_srid` column and the UTM fix (§3b); city-polygon strategy (§3a) | S |
| 2 | Boundary ingest: city limits, council districts, neighborhoods, NPUs from ATLDOT's FeatureServer; COC as an equity-style group with **no** compliance flag (§3c) | M |
| 3 | `GbfsAdapter` gains Bird + Lime capability declarations; `battery_percent()` for Bird's `current_fuel_percent`; Lime's registry corrections | M |
| 4 | `stable_vehicle_id: false` end to end — capability endpoint, and the frontend actually hiding ~half its surfaces on it | **L**, and mostly frontend |
| 5 | Address index + geocoder: Atlanta ArcGIS field mapping, Photon index widened to GA | M |
| 6 | Routing graph: combined Denver+Atlanta `.pbf`, measured (§4) | **L**, infra-bound, may not fit |
| 7 | Per-feed `cycle_minutes`, `raw_telemetry_points` partitioning, per-feed archive | M |
| 8 | Basemap, Pages project, DNS, CORS pattern, per-origin magic-link | S |
| 9 | Pricing: `provider_rate_plans`, and Atlanta rendering *no* prices (§3d) | M |
| 10 | **Points for off-feed rides** (§2b) — not multi-tenancy work at all, and a Denver feature too | M |

Item 4 is the one that is under-estimated by instinct. It is not a feature
flag; it is auditing 37 of the frontend's 101 modules for an assumption that
a device has history, and deciding per surface whether it hides, degrades,
or explains itself.

Item 10 is the one that is *over*-estimated by instinct, because it looks
like Atlanta work and is not. It has no dependency on any other row in this
table, ships to Denver on its own merits, and is what decides whether an
Atlanta rider's ledger moves.

---

## 6. Recommendation

1. **The identity question is answered — do not reopen it hopefully.** §2
   settles it: both operators are `stable_vehicle_id: false`, and Lime's
   99% steady-state persistence is a trap rather than an opportunity. The
   only follow-up worth running is a longer window to pin Lime's re-mint
   period and confirm no third behaviour exists — a 24-hour poll of
   `free_bike_status` recording epoch boundaries. That is a nice-to-have
   for the adapter's docstring, not a gate on anything.
2. **Read ATLDOT's shared-mobility permit** before writing a line of equity
   UI. §3c is a hard blocker on the compliance surfaces, and it is a
   reading task, not an engineering one.
3. **Measure the combined routing graph** before committing to the phase.
   §4 is the only item that can fail outright rather than merely take time.
4. **Ship points for off-feed rides** (§2b). It is independent of every
   other item here, it is a Denver improvement on its own, and it is the
   difference between an Atlanta rider having a progression and not.
5. Then and only then, do Phases 1–3 of the parent plan. Atlanta does not
   start early; adding a second provider in Denver first (parent §10) still
   flushes out the Veo assumptions more cheaply than Atlanta will.

**Two of these can still change the answer, and neither is a big job.** The
ATLDOT permit is a reading task that decides whether the equity surfaces can
exist at all; the routing measurement is a build-and-look that is the only
item here capable of failing outright rather than merely taking time. Item 4
is worth building now regardless, because it is a Denver improvement that
happens to be Atlanta's foundation.

---

## 7. Artifacts from these probes

Worth having regardless of whether any of the above is built:

- `data/bird_atlanta_geofencing.geojson` — 65 live no-ride / no-through
  polygons, `last_updated` ticking. Unlike Denver's Bird zones these back a
  real 2,896-vehicle fleet. Candidate map overlay and Valhalla
  avoid-polygon source. Bird's encoding of Atlanta's rules, **not** a City
  publication — verify before treating as authority.
- `data/atlanta_city_limits.geojson` — the municipal boundary, published
  directly by the city rather than reconstructed from neighborhoods (§3a).
  The layer the 99.9% / 98.3% in-city shares in §1 were measured against,
  committed so that number is reproducible.
- `data/atlanta_communities_of_concern_2025.geojson` — 15 NSAs, Tier 1/2,
  with full component scores. Provenance and the "planning layer, not a
  permit term" caveat are stamped into its `_source` block.
- `tests/fixtures/atlanta_gbfs_2026-08-29.json` — a dated, trimmed capture
  of both operators' `free_bike_status` plus their registries, and the
  measured identity-rotation result. The golden fixture the `GbfsAdapter`
  needs, at real coordinate distribution, with Bird's float
  `current_fuel_percent` and Lime's redundant `vehicle_type` string in it.
