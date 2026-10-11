# Servicing plan: swaps, depot visits, settled battery, and what riders see

Owner, 2026-10-10: "Do all this please, good ideas here. Plan time!"

This plan follows from the telemetry work in #150 and #151 and from the service analysis the same day. The fleet now shows us two service processes we never recorded:

- **In-field battery swaps.** About 2,400 a day. 85% happen in place, at a median 4% battery. They peak at 1–4 am and 8–11 pm.
- **A depot** at about 39.8135, −105.0185 (Federal Blvd and W 72nd Ave).
  - 12,512 visits by 7,157 vehicles since May 31.
  - Median stay 10.8 h; 8% stay 3 days or more.
  - 569 vehicles were last seen there and never came back.

This plan records both, fixes the readings they exposed, and puts the results in front of riders and advocates.

## Decisions

The owner said "do all this". Where a choice remained, the recommended option is taken below and marked **(default)**. Each one is a single constant to change.

| # | Decision | Default |
|---|---|---|
| D1 | Does a depot stay clear a negative report? | **Yes.** A depot visit that *began after* the report (or its re-baseline) and has *ended* (the vehicle is back on the street) clears every report on it. Length doesn't matter: a vehicle was taken in and brought back. |
| D2 | "Fresh battery" | Serviced within the last **12 h**, **not rented since**, and reading **≥ 80%**. |
| D3 | "Back from the shop" | A depot stay of **≥ 72 h** that ended within the last **7 days**. |
| D4 | "About to be hidden" warning | Settled battery **≤ 8%**. Veo hides vehicles at about 4–5%, and a rider needs time to get there. |
| D5 | Rider alerts | **Revised in Phase 2:** rider watches are deliberately short-lived (a dib ≤ 25 min, the scooter you just rode for a few hours) so the app cannot be used to follow a vehicle parked outside someone's home. A long-lived "back on the street" alert would break that. So riders get **in-app** alerts inside those existing watches (the client reads `last_serviced_at` / `fresh_battery`), and the operator-style events — serviced, taken into the depot, back from the depot — go to the **admin SMS watch**. |
| D6 | Publish effective fleet size in Stats | **Yes,** with its caveat in the payload (as fleet_equity does). |

## Phase 1: Data foundation (API, one PR)

### 1a. Servicing log: `service_events` (sql/107)

- **When a row is written:** one per servicing, written by ingest at the moment it stamps `device_state.last_serviced_at`. The rule is #151's: full after a parked low ≤ 50% since the last full reading.
- **Columns:**
  - `vehicle_identifier`, `observed_at`, `cycle_id`
  - `lat`, `lon`, `h3_9`, `equity_area` (from `_equity_area_of`)
  - `low_range_meters`, `low_at`, `low_lat`, `low_lon`, `full_range_meters`
  - `moved_m` (low spot → full spot), `in_place` (`moved_m < 100`)
  - `after_absence` (the vehicle was out of the feed in between), `at_depot`
  - `vehicle_model_name`
- **device_state additions:** `range_low_at`, `range_low_lat`, `range_low_lon`. They are set with `range_low_since_full`, so the low's time and place are known when the full reading lands.
- **Retention:** kept indefinitely. About 2,500 rows a day, roughly 1 M rows a year.

### 1b. Depot visits: `depot_visits` (sql/107)

- **Depots** are listed in `data/depots.json`: id, name, lat, lon, radius 200 m. One entry today, the discovered depot. Stops drop off sharply beyond 200 m; there are street stops in the 500 m and 1.1 km rings, so the radius must stay tight.
- **Ingest writes visits:**
  - A vehicle's first stop inside a depot geofence *opens* a visit. `picked_up_at`, `pickup_lat` and `pickup_lon` come from its previous non-depot stop.
  - Its first stop outside *closes* it, with `exited_at`, `deploy_lat` and `deploy_lon`.
  - Indoor jitter (many stops inside) stays one visit.
- **Columns:** `went_dark_first` (the previous stop closed `absent`), `vehicle_model_name`, `equity_area` of pickup and of deploy.
- **`discover_depots` CLI** (monthly, report-only): clusters the reappearance points of absences that ended more than 1 km away, and lists any cluster not yet in `depots.json`. A new depot is added by hand after a look.

### 1c. Settled battery reading

The range sags under load and rebounds for minutes after a ride, by up to about 25% of a full charge. Three things currently read that sag:

- the battery shown to riders;
- `range_at_report_meters` on new reports;
- the `rose` clause of the report-clearing rule.

**Changes:**

- **New device_state columns:**
  - `settled_range_meters`: the highest parked reading since the last rental ended.
  - `settling_until`: rental end + 30 min.
  - Rental start or a move resets both.
- **Payload:**
  - `battery_percent` and `estimated_range_meters` use `settled_range_meters` while `now < settling_until`.
  - A new `battery_settling` boolean lets the card say "recovering after a ride".
- **Report baseline:** `range_at_report_meters` records the settled reading.
- **Clear rule:** `rose` compares the settled reading too. The servicing path (#150/#151) already ignores sag.

### 1d. "Hidden for a swap" versus "missing" (census)

85% of feed absences come back at the same spot, average battery 6% → 27%. The census labels them separately:

- **`hidden_low_battery`:** absent, and last settled reading ≤ 8%. Expected back after a swap.
- **`missing`:** everything else past the threshold. Unchanged.
- **`at_depot`:** the last stop is inside a depot. These are not missing.

The admin census pages and the export gain the new labels.

### 1e. Effective fleet

- **`effective_fleet(cur)`** returns:
  - vehicles seen on the street in the last 24 h;
  - vehicles currently inside a depot, split into < 7 d / 7–30 d / ≥ 30 d;
  - long-term inside (≥ 30 d, a proxy for retired).
- **Uses:** the Stats endpoint (2b) and the admin export.

### 1f. Backfill

- **Depot visits:** from `device_history` (May 31 onward). One CLI run, idempotent, because the visit is keyed by `(vehicle_identifier, entered_at)`.
- **Service events:** from the R2 raw-telemetry archives, using `battery_model.backfill_trips_from_archive`'s DuckDB pattern. It replays the #151 rule per vehicle across archive files in time order.
  - Known gap: at archive file boundaries a low and its full can fall in different files. The low/full state is carried across files in memory, per vehicle, rather than dropped.
  - Rows are marked `source = 'backfill'`.
- **Era caveat:** stops before 2026-08-10 were counted under the earlier stop rules (see the counting-eras memory). Depot stays before that date travel with an `era` field, so the Stats panel never compares across it.

### 1g. A depot stay clears a report (D1)

- **`uncleared_negative_sql`:** a report also clears when a `depot_visits` row has `entered_at > n.base_at`, `exited_at IS NOT NULL` and `exited_at <= now`.
- **Scope:** this applies to every negative type. A location report is moot once the vehicle has been taken in.
- **Tests:** the mirror test gains the clause, and `test_negative_report_hold_pg.py` gains the cases (a visit before the report; a visit still open; a completed visit).

**Tests for Phase 1:**

- ingest PG tests for service events, depot open/close with jitter, the settled reading through a ride, and the census labels;
- a backfill test over a synthetic archive file (DuckDB);
- the full suite against Postgres.

**Ingest budget:** cycles take about 16–19 s today. All new writes touch only vehicles whose charge changed or that crossed a geofence, which is a few hundred rows a cycle. Measure on a scratch copy before merging.

## Phase 2: Endpoints (API)

### 2a. `/devices/current` fields

All of these are built in `_build_device_features`, so the precomputed cache serves them.

- `last_serviced_at`, and `fresh_battery` (D2)
- `battery_settling` (1c)
- `back_from_shop_at` (D3)
- `hide_risk` (D4)
- `latest_report.serviced_since`: the servicing or depot visit after the report, if any, for the case where it still stands (for example, serviced but not moved).

### 2b. `GET /api/v1/fleet/service` (public, cached per cycle)

- **Swaps:** per day; share in place; battery at swap (deciles); by hour.
- **Swap wait, the equity number:** the hours a vehicle sits at ≤ 10% before servicing, Equity Areas versus outside.
  - The point is tested, not its hex (fleet_equity's lesson).
  - The response includes n, `excluded.unknown_area` and `data_since`.
- **Depot:** visits a month; the stay distribution; stays by model (share ≥ 3 d); pickup distance; the share redeployed within 300 m of pickup.
- **Effective fleet** (1e).
- **Caveats** travel in the payload:
  - the depot was inferred from the feed;
  - repair and charge can't be told apart;
  - swap history before the archive backfill is missing;
  - the era boundary.

### 2c. Rider alerts (D5)

- `dibs_watch` and `ride_watch` gain two events: **serviced** (a `service_events` row for the vehicle) and **back on the street** (a `depot_visits` row closed).
- Each is opt-in per dib or watch (new boolean preferences, sql/107). It is sent through comms with idempotency keys, and STOP is honoured as today.

### 2d. Export

The advocacy export gains service metrics: swap wait by area, depot stays by model, and effective fleet.

## Phase 3: Frontend (one PR, after Phase 2 is live)

Each claim must be true on production before it ships. zneill-agent holds disclosure PRs until the behaviour is live.

- **Fresh battery:** a 🔋 badge on the card, plus a map filter chip "Fresh battery".
- **Settling:** "Battery recovering after a ride. Reading may be low for a few minutes."
- **About to be hidden:** a card warning. The planner and plan list deprioritise `hide_risk` vehicles and never pick one as the first choice.
- **Details tile:**
  - "Serviced Oct 10, after this report" (`latest_report.serviced_since`);
  - "Back from the shop (4 days)".
- **Stats panel, new Service section:**
  - swaps a day and when they happen;
  - **swap wait, Equity Areas versus elsewhere**;
  - depot stays by model;
  - effective fleet.
  - Copy follows the panel's existing tone (fe#127).
- **Notify preferences** on dibs and watches: "Tell me when it gets a fresh battery" and "Tell me when it's back on the street". These sit beside the existing toggles and are protected with `markUndoFree` (the iOS shake-to-undo bug, fe#129).
- **Legal:** the alert texts are service texts the Terms already cover. The privacy policy needs no change, because no new personal data is collected. The planned wording is checked against both anyway.

## Order and PRs

| PR | Repo | Contents | Depends on |
|---|---|---|---|
| 1 | api | Phase 1a–1g + backfill CLIs | — |
| — | ops | Run the backfills: depot visits from history, service events from R2 | PR 1 live |
| 2 | api | Phase 2a–2d | PR 1 |
| 3 | fe | Phase 3 | PR 2 live |

Every PR gets a zneill-agent review. Merges happen only on a current-head approval with CI green. Each is measured live after deploy: ingest time, payload size, and the new counts against the analysis above.

## Risks

- **The depot geofence picks up street parking nearby.** Keep the radius at 200 m, review `discover_depots` output by hand, and show `at_depot` vehicles in the admin census to catch mistakes.
- **Veo's hiding threshold is inferred** (about 4–5%). D4 uses 8% for margin, and `hidden_low_battery` uses the same constant.
- **Rebound and sag elsewhere.** Any new reader of `current_range_meters` should use the settled reading. Document this in `battery_model`.
- **Payload growth.** The 2a fields are 4 small fields per vehicle, about 3% gzip growth. Measure it.
- **Backfill load.** Run the archive replay off-peak, outside the 02:00 archive and 05:00 Photon windows, and watch ingest cycle times, as was done for the 2026-10-10 analysis.
