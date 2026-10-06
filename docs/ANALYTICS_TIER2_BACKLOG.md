# Analytics tier 2+ — the rollup everything else waits on

Scope: counting what the fleet did. The asking-riders side lives on
`claude/rider-reporting` (denver-scooter-fyi).

The governing plan is `docs/ANALYTICS_PLAN.md` in **denver-scooter-fyi**, on
main. Read its §0.2 first: it records the 25 m radius decision and the two
caveats a published figure still has to carry.

## What exists (tier 1)

`GET /api/v1/fleet/outcomes` — the lifetime no-go rate, fleet-wide and by
model, straight off sql/072's counters. On main (#109). It is deliberately
labelled `window: "lifetime"` because that is all those counters can say, and
its module header records the two caveats that outlive it: the counters span
the 16 m and 25 m definitions, and a no-go is end displacement rather than
maximum, so round trips count as one.

## 1. The hourly rollup — do this first, it gates the rest

**What:** a table written at the moment a rental completes, carrying the same
outcome the counters carry plus WHEN and WHERE.

**Why it cannot wait and cannot be backfilled:** `raw_telemetry_points` is
truncated at ~48 h. Every question below is a time series, and nothing in the
database can answer a time-series question today because `device_state`'s
counters only ever count up. Every day this is not shipped is a day of history
that cannot be recovered. That is the whole argument for its priority — it is
not that the drawer looks better.

**Where:** `src/device_state.py`, in the same transaction that increments
`rentals_observed` / `rentals_no_go` (search `rentals_no_go` — the increment
is the insertion point). A new `sql/0NN_rental_outcomes_hourly.sql`.

**Grain:** hour × h3 cell × model, with observed / no-go counts. Hour because
the headline asks for trips per hour; h3 because the plan asks for a hexagon
breakdown and the devices table is already h3-indexed at several resolutions.

**Record the radius per row.** `claude/radius-25m` changes
`stationary_threshold_meters` from 16 m to 25 m, and the lifetime counters
already span both definitions with no way to tell which is which. Do not
repeat that mistake in a table built from scratch — a `radius_m` column costs
nothing now and is impossible to add retroactively.

## 2. What the rollup then unlocks

In the plan's priority order:

- **Day by day / week by week** — the narrative the owner asked for first.
- **Trips per hour, trips per day** — headline numbers.
- **Breakdown by hexagon region.**
- **Repair time** — how quickly a vehicle in a bad state returns to a good
  one, over a rolling window. Needs a state-transition history, not just
  outcome counts; the plan acknowledged this one is furthest out.
- **The per-place, per-week figure attached to a rider's story** — what turns
  "my scooter died" into a finding. See `claude/rider-reporting` item 4.

## 3. The equity cut

Also listed on `claude/backlog-outstanding`, repeated here because the rollup
is what makes it honest. Splitting a LIFETIME counter by a vehicle's CURRENT
location is a methodological hole. Attributing at increment time — which the
rollup does by carrying the h3 cell — closes it. Build the rollup first and
the equity cut is a query; build the equity cut first and it is a claim that
will not survive being checked.

## The voice, which is not optional

`ANALYTICS_PLAN.md` states it and the shipped tier-1 code follows it:
**same numbers, different verbs. scooter.fyi reports, WSYV argues.** Anything
added here inherits it. A no-go is an attempt that went nowhere; the cause
might be the vehicle, the app, the weather or a rider changing their mind, and
this layer counts rather than attributes. Every published figure states its
window, its sample and its radius — the three things that make a percentage
quotable instead of merely repeatable.
