# Fleet analytics: plan

**Owner's request (2026-10-07):** analytics phase 2 is not "rental outcomes" only. It should be a broad picture of how Veo performs in Denver. The house rules from the frontend's `docs/ANALYTICS_PLAN.md` still apply:
- every figure carries its window and sample;
- scooter.fyi reports, it does not argue;
- an unsourced boundary is never drawn.

## The charts, and where each one comes from

| # | Chart (owner's list) | Source | History | How it is served |
|---|---|---|---|---|
| 1 | Rides, as a histogram by hour / day / week / month, by device model | `trip_events` (model, start point, `detected_at`) | since 2026-07-05 | rollup `analytics_rides_hourly` |
| 2 | Failed starts per day, by device model | `device_history.dwell_failed_starts`, attributed when the stop closes | since 2026-05-31 | rollup `analytics_failed_starts_hourly` |
| 3 | Charts 1 and 2 for a selectable neighbourhood or city region | the same rollups, keyed by region | as above | `region_type`, `region_name` |
| 4 | Devices by neighbourhood | `regional_metrics_narrow.count_total` | since 2026-05-30 | live query |
| 5 | Equity-area compliance % of the fleet, by hour | `snapshot_metadata_core.percent_all_devices_equity`, against Exhibit B's 30% | since 2026-05-30 | live query |
| 6 | Average dwell time per device type, by city region | `device_history` (arrival `snapshot_time` → `departed_at`) | since 2026-05-31 | rollup `analytics_dwell_daily` |
| 7 | Hourly lines: available, in use, out of service, off-map | `device_status_snapshots` (+ new `off_map`) | since 2026-09-07; off-map from deploy | live query |
| 8 | Number cards: devices visible now; devices ever seen, by model | latest `device_status_snapshots`; `device_state` | now / all time | live query |

**Regions:** `city` (all of Denver), `neighborhood` (78), `council_district` (13) and `community_network` (13). These are the official layers already loaded as boundaries; no new boundary is drawn.

## Definitions the charts must state

- **A ride** is a `trip_events` row: a vehicle that moved from one stop to another. It is placed by where it **started** and bucketed by when the move was **detected**, which is within one 2-minute cycle of the drop.
- **A failed start** is a rental that ended where it began (the failed-start rule in `device_state.py`). It is counted when its stop closes. **Caveat:** failed starts have been under-reported since 2026-08-10 (in-place releases plus GPS jitter; a fix is in progress), so the chart marks that date rather than presenting the drop as real.
- **"Devices on the map" by neighbourhood** counts every vehicle the feed shows in the neighbourhood, whatever its status. Veo keeps rented and out-of-service vehicles in the feed. A per-status, per-neighbourhood split was never recorded.
- **Equity compliance** is the share of the Denver fleet inside the official Equity Areas, per cycle, averaged per hour. The line marks Exhibit B's 30%. The *daily* compliance verdict (6–9 AM) stays the existing `/compliance/daily`; this chart is context for it, not a second verdict.
- **Dwell** is how long a vehicle stayed at a stop before it moved, from arrival to departure. Stops still open are not counted. The average is per model and region over the window, with its sample size.
- **Off-map** is vehicles seen in the last 7 days but not in the current feed: rented out past the feed, in the shop, or gone. It is recorded per cycle from this deploy on; earlier hours are null, not zero.
- **Time buckets** are Denver local time (`America/Denver`) for day, week (Monday start) and month. Hours are stored in UTC and shown local.

## Rollups (sql/094)

These are incremental and idempotent, refreshed at the end of every ingest cycle. A refresh failure is logged and never fails the cycle. A one-time `analytics_backfill` CLI fills the history.
- **`analytics_rides_hourly`** (hour, region_type, region_name, model): rides. Watermark: `trip_events.id`, which is append-only with a single writer.
- **`analytics_failed_starts_hourly`** (same key): failed starts, plus the stops that had any. Watermark: `departed_at`, processed with a **6-hour lag**, because the absent rule backdates `departed_at` by up to `ABSENT_STOP_AFTER` (1 h).
- **`analytics_dwell_daily`** (Denver day, region_type, region_name, model): dwells and the sum of dwell seconds. Same watermark and lag. Dwells over 30 days are excluded as data errors.
- **`analytics_rollup_state`**: one watermark row per rollup.

A row's regions come from its point, via `geo.region_for_point` for each layer, at write time. The `city` row counts everything.

## API (all public, cached 5 min)

Common parameters are `days` (1–120), `granularity` (`hour|day|week|month`), `region_type`, `region_name` and `tz` (fixed to Denver). Every response carries `window_start`, `window_end`, `data_through`, `granularity`, `region`, its definitions and its sample sizes.
- `GET /api/v1/analytics/rides`
- `GET /api/v1/analytics/failed-starts`
- `GET /api/v1/analytics/devices-by-region`
- `GET /api/v1/analytics/equity-compliance`
- `GET /api/v1/analytics/dwell`
- `GET /api/v1/analytics/fleet-status`
- `GET /api/v1/analytics/fleet-counts`

## Frontend

A dedicated **Analytics page** (`/analytics`), linked from the stats drawer:
- the number cards across the top;
- then the charts;
- one shared control bar: window, granularity, and region type plus region.

uPlot (small and fast; one dependency) draws the lines and bars. Every chart shows its window, its sample and its caveat line. It is mobile-first: one chart per row on a phone.

## Order

1. API: sql/094, the rollups and their refresh, the backfill CLI, the seven endpoints, tests. Deploy, then run the backfill.
2. Frontend: the page, the charts and the controls, against the live endpoints.
3. Follow-ups:
   - fold the rental-outcome figures (`/fleet/outcomes`, `/fleet/outcomes/equity`) into the same page;
   - a per-status, per-neighbourhood count going forward, if "available by neighbourhood" is wanted strictly.
