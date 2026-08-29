# Along the Way — program plan (master) + API lane

Planned 2026-08-29 against `main` (3e0236d). Branch:
`claude/along-way-upgrades-feature-piml2p` in both repos.

This is the **master** document for the program: the vision, the vocabulary,
the decisions already taken, the phasing, and the risks. The second half is
the **API lane** (this repo). The frontend lane is
`denver-scooter-fyi/docs/ALONG_THE_WAY_PLAN.md` — a companion, not a
duplicate; where the two must agree, this file is the one that is right.

Nothing here is built yet. This is a plan to be argued with.

---

## 1. What we are building

A rider tells the app **what they will ride** and **where they are going**.
The app finds the vehicle that gets them there best, claims dibs on it, walks
them to it — and when somebody else takes it out from under them, finds the
next one **along the route to their destination**, claims that instead, and
tells them, without the rider having to take the phone out of their pocket.

Three parts, in the order they matter:

1. **The Spec.** Kind of device, required features, minimum quality, minimum
   battery — stated once, as requirements rather than as map filters.
2. **The corridor search.** Rank candidates by *how long the whole trip
   takes* — walk to the vehicle **plus** ride to the destination — not by how
   far the vehicle is from the rider. That single change is what "along the
   way" means: a scooter 500 m further *towards* where you are going beats one
   300 m in the wrong direction.
3. **The swap.** Dibs on the chosen vehicle; watch it; when it goes, release,
   re-search from where the rider is *now*, claim the replacement, and say so
   in one message.

Then, later and separately: **route the trip to cost less**, by starting it
inside an Equity Area, or by breaking it at one.

### What already exists (and is therefore not in scope to invent)

This program is mostly wiring things this app already has into a loop it does
not currently close.

| Piece | Where it lives today |
|---|---|
| Device filters — model, min battery, quality tier, features | `denver-scooter-fyi/src/devices.ts`, `filter-presets.ts` |
| Crowdsourced features + consensus (`bell`/`basket`/`cup_holder`/`phone_holder`, poor-condition) | `src/api_device_features.py`, `device_state` |
| Reliability tiers (`ok` / `unknown` / `risk`) | `src/quality.py`, `reliability.ts` |
| "Will this one get me there?" range check | `denver-scooter-fyi/src/reach.ts` (client) + `/route/options`'s `will_make_it` (server) |
| Dibs — local claim, server timestamp, certificate, release, live map of claims | `src/api_dibs.py`, `sql/076`, `dibs.ts` |
| Dibs notifications (4 alerts, lock-screen + in-app) | `dibs-notify.ts` |
| "Your scooter went" detection (`is_reserved` / not rentable / vanished / our own signals) | `device-watch.ts` |
| Routed walk leg + arrival panel | `walk-leg.ts`, `arrival-panel.ts`, `/route/walk` |
| Routed ride, profiles, battery-burn model, arrival battery | `src/api_route.py`, `src/battery_model.py` |
| Ranked recommendations from a start point | `recommend.ts` |
| Equity Area geometry + the discount's meaning | `data/equity.geojson`, `equity-areas.ts`, `ride-cost.ts` |
| Rider preference blobs (opaque, server-stored, capped) | `sql/043`, `sql/050`, `src/api_preferences.py` |
| Server-side per-cycle watcher pattern | `src/ride_watch.py` |
| SMS out, with consent and quota handled upstream | `src/comms.py` |

**The gap this program fills, precisely.** Today, when `device-watch.ts` fires
`onGone`, `main.ts` clears the walk line and puts a sentence in the arrival
panel (`main.ts:3257`). That is the whole recovery story: the rider is told
their scooter is gone and handed back a map. Everything below exists to
replace that dead end with the next scooter.

---

## 2. Vocabulary

**Spec** — a rider's stated requirements for a vehicle, with each requirement
marked **must** or **prefer**. Distinct from a *filter*, which decides what is
drawn on the map. A filter hides; a spec disqualifies and ranks.

**Corridor** — the set of vehicles worth considering for a trip from `P` to
`D`: reachable on foot within the walk cap, and not so far off the line that
riding from them is worse than walking.

**Trip cost** (the ranking scalar, not money) — `walk_seconds(P→v) +
ride_seconds(v→D)`, plus penalties. The whole ranking is this number.

**Claim** — one dibs row. Twenty-five minutes at the outside, per `sql/076`
and `dibs.ts`. Not a reservation, not a hold, and this program must never
describe it as one.

**Swap** — releasing a claim on a vehicle that is gone and claiming the best
remaining candidate, re-searched from the rider's current position.

**Trip plan** — the live document tying a spec, a destination, a current
target, its claim, and the swap history together. Phase 3 keeps it in the
browser; Phase 5 asks whether it should live on the server.

---

## 3. Decisions already taken

| Question | Decision | Why |
|---|---|---|
| Rank by walk distance, or by whole-trip time? | **Whole-trip time.** `walk(P→v) + ride(v→D)`. | It is the definition of "along the way", and it subsumes the reach question for free — a vehicle that cannot make it to `D` has no finite ride leg. |
| One endpoint for "find me one" and "find me another"? | **One.** `POST /api/v1/trip/candidates` with an `exclude` list. | A replacement search is the first search from a new position with one vehicle struck out. Two endpoints would be the same code twice, drifting. |
| Route every candidate? | **No.** Two Valhalla *matrix* calls rank the whole corridor exactly; a full route is computed only for what the rider is actually shown. | Routing 40 candidates individually is 40 calls against an endpoint rate-limited at 30/min. `sources_to_targets` is one call for many pairs. |
| Is the spec a new kind of saved filter preset? | **No — a new object,** stored as `user_preferences.kind = 'ride_spec'`. | Filter presets are localStorage-only and carry map state (`area`, `hideUnavailable`) that means nothing to a trip. Reusing them would make one blob answer to two owners. |
| Does a swap auto-claim, or ask? | **Auto-claim inside a defined envelope, ask outside it.** See §6.3. | The rider is walking with the phone away. A question they cannot see is not a safer default than an action they can undo in one tap. |
| Does the swap raise a second notification after "it's gone"? | **No — one message, or two, never both.** | `dibs-notify.ts` caps itself at four alerts per claim on purpose. A swap that buzzes twice in three seconds spends the budget that protects "RUN!". |
| Does the certificate change? | **It gains a chain link** (`replaces_dibs_id`), nothing else. | The certificate is an assertion about one vehicle at one time. A swap makes a *new* claim; it does not extend the old one. |
| Persist the trip plan server-side in v1? | **No.** Phase 3 is client-only. | A live position + destination stored server-side is a new retention rule (three-address rule, §9) and a much larger privacy conversation than the feature needs to prove itself. |
| Proactive "upgrade" offers (a better vehicle appears mid-walk)? | **Behind a gate, in Phase 3b, off by default.** | The feature is named "upgrades" and the machinery is identical, but an app that renegotiates the plan while you walk is an app you stop trusting. |
| Equity stopover for the `equity` (Access) rate plan? | **Never offered.** | Access is 60 free min/day then 15¢/min with no unlock. The Equity Area rate is $1 + 13¢/min. Whether the two interact is *not stated anywhere in the contract we have* (`config.ts`'s own note), and the plausible readings include ones where the advice costs the rider money. |

---

## 4. Phasing

Each phase is independently mergeable and useful on its own.

| Phase | Ships | API lane | Frontend lane |
|---|---|---|---|
| **1 — The Spec** | Requirements stated once, saved to the account, synced | `sql/080`, `/api/v1/profile/ride-specs` | `ride-spec.ts`, spec sheet UI |
| **2 — Along the way** | Corridor ranking; "best vehicle for *this trip*" replaces "nearest vehicle" | `valhalla.matrix()`, `src/trip_candidates.py`, `POST /api/v1/trip/candidates` | `along-the-way.ts` (client-cheap tier), wired into the home bar's plan flow |
| **3 — Claim & swap** | Auto-dibs, loss detection → replacement → one message | `sql/081` (`replaces_dibs_id`), `replaces` on `POST /dibs` | `trip-plan.ts` state machine, `arrival-panel.ts` swap face, `dibs-notify.ts` 5th alert |
| **3b — Upgrades** *(optional)* | Mid-walk offer when a materially better vehicle appears | — | gate in `trip-plan.ts` |
| **4a — Start in an Equity Area** | "Walk 2 min further, save $1.80" | equity flag + cost on candidates | `equity-savings.ts`, candidate chips |
| **4b — Stopover** | Break the trip at an Equity Area when the arithmetic says to | `src/equity_savings.py`, stopover search | two-leg cost UI |
| **5 — Pocket-proof** *(not committed)* | Swap works with the app closed | server-side trip plan + `ride_watch`-style job + Web Push / SMS | service worker |

Phase 1 and Phase 2 are both useful without Phase 3. Phase 3 is the feature.

---

## 5. Phase 1 — The Spec

### 5.1 The object

```jsonc
{
  "models":       ["cosmo", "rover"],   // null = any model
  "features":     ["basket"],           // consensus must be TRUE (null/unknown does not match)
  "min_battery":  40,                   // percent
  "min_quality":  "no-risk",            // "any" | "no-risk" | "ok-only"
  "must_reach":   true,                 // disqualify anything that cannot reach the destination
  "max_walk_minutes": 12,               // <= 15 whenever auto-dibs is on; see 6.1
  "must": ["features", "must_reach"]    // which of the above are HARD
}
```

Everything not named in `must` is a **preference**: it moves the ranking, and
it is relaxed — in a fixed, published order — before the app tells a rider
there is nothing for them.

**Unknown never satisfies a requirement.** `feature_payload()` already
serializes a feature nobody has confirmed as `null`, and its docstring already
records that a filter must read `null` and `false` identically. The spec
inherits that reading exactly, and the UI must say so: "must have a basket"
means *confirmed* to have one.

### 5.2 The relaxation ladder

The order is fixed, published in the UI, and identical on both sides:

1. **Never relaxed:** availability, anything the rider marked `must`, and
   `must_reach` when set. A vehicle that cannot reach the destination is not a
   worse candidate, it is not a candidate.
2. `min_battery`, down to the reach-feasible floor and no further.
3. Preferred `features`, dropped one at a time, cheapest-signal first.
4. `models`, widened to the same form factor (standing → standing).
5. `min_quality`, but **never below `no-risk` automatically**. Handing a rider
   a vehicle our own signals call high-risk, without asking, is the one
   relaxation that can end a trip worse than not finding anything.

Every response says what it relaxed. Every swap card shows it.

### 5.3 API — `sql/080_ride_specs.sql`

Next free migration number is **080** (highest on `main` is `079`; note `069`
is used twice already — do not add a third).

`user_preferences.kind` carries a named CHECK constraint listing the allowed
kinds. Extend it with the **exact guarded shape `sql/050` established** — read
`pg_get_constraintdef`, test for the new value's presence, drop and re-add
only if absent. Do not use `ADD COLUMN IF NOT EXISTS` with an inline CHECK
anywhere (house rule; silently skipped when the column exists).

```
kind IN ('saved_map_settings', 'find_ride_pref', 'ride_mode_usual', 'ride_spec')
```

Plus a partial unique index on `(account_id, name) WHERE kind = 'ride_spec'`
— load-bearing, because it is the arbiter the upsert's `ON CONFLICT` names,
exactly as `sql/050`'s comment records for Usuals.

Cardinality: **many, addressed by name**, capped at **5** in
`src/api_preferences.py` (not in the migration — product limits are code
changes, per `sql/043`'s header). Five, not ten: a spec is chosen at the top
of a trip from a short list, and a rider with ten of them has built a search
problem.

### 5.4 API — endpoints

```
GET    /api/v1/profile/ride-specs           every saved spec
GET    /api/v1/profile/ride-specs/{name}    one
PUT    /api/v1/profile/ride-specs/{name}    create or replace
DELETE /api/v1/profile/ride-specs/{name}
```

Same handler shapes as the Usuals block in `src/api_preferences.py`, reusing
`_enforce_named_cap` verbatim. The blob stays **opaque to the server** in
storage, per that module's contract — but note the deliberate asymmetry:
`POST /api/v1/trip/candidates` (§6) *does* interpret a spec, because it is
doing the search. The preferences table stores; the trip endpoint reads. Those
are different jobs and it is fine for only one of them to understand the
shape. What must not happen is the preferences module growing validation.

Signed-out riders keep a spec in `localStorage` and lose nothing but sync.
(Dibs itself requires an account — `dibs.ts`'s `signed_out` verdict — so
Phase 3 is signed-in anyway. Phases 1, 2 and 4 are not.)

---

## 6. Phase 2 — The corridor search

### 6.1 `POST /api/v1/trip/candidates`

```jsonc
// request
{
  "from": { "lat": 39.7392, "lon": -104.9903 },
  "to":   { "lat": 39.7508, "lon": -104.9966 },
  "spec": { /* §5.1 */ },
  "exclude": ["<vehicle_identifier>", "..."],   // struck out this trip
  "limit": 5,                                    // hard-capped at 5
  "geometry": true                               // full routed legs for the top result only
}
```

```jsonc
// response
{
  "candidates": [{
    "vehicle_identifier": "…", "device_id": "…", "name": "Lunar 🐸 928",
    "model": "cosmo", "battery_percent": 71, "reliability_tier": "ok",
    "device_features": { "basket": true, "bell": null, … },
    "lat": …, "lon": …,
    "walk":  { "seconds": 214, "meters": 268, "geometry": {…} },
    "ride":  { "seconds": 486, "meters": 1904, "profile": "safe",
               "arrival_percent": 58, "arrival_percent_low": 49,
               "will_make_it": true, "geometry": {…} },
    "trip_seconds": 700,
    "relaxed": [],
    "dibs": null,
    "equity": { "starts_in_area": false, "ends_in_area": true,
                "estimated_cents": 224 }
  }],
  "relaxed": [],            // ladder rungs used to fill the list at all
  "considered": 37,
  "beta_warning": "…"
}
```

**POST, not GET.** The spec is a structured object with three arrays. Encoding
it into a query string is how the fourth serialization of the rider's
requirements gets invented, and the first one to drift silently.

Rate-limited on the same IP bucket as `/route` (`_limit_route_ip`, 30/min): it
is a routing endpoint wearing a different hat, and it must not be a way around
the routing budget.

**`max_walk_minutes` is clamped to 15 when the caller says auto-dibs is on**,
because `DIBS_MAX_WALK_MINUTES = 15` already makes a claim beyond that void
(`dibs.ts`). Offering a candidate that cannot legally be claimed is offering
the rider a plan the next screen refuses. The response echoes the clamp.

### 6.2 How it runs — three stages, two Valhalla calls

1. **Prefilter, in SQL.** Current cycle's devices, `bbox` = the envelope of
   `from` and `to` expanded by the walk cap, minus `exclude`, minus reserved /
   disabled, with the spec's hard predicates pushed down (`battery_percent >=`,
   model, `reliability_tier`, `device_features ->> …`). Cheap, indexed, and it
   is what keeps the matrix small.
2. **Rank on the straight-line proxy.** `walk` at pedestrian pace and `ride`
   at the fleet speed, both through **`DETOUR_FACTOR = 1.35`** — the ratio
   `reach.ts` already carries, measured against donated tracks. Keep the top
   `3 × limit`.
3. **Measure exactly, with two matrix calls.**
   - one pedestrian `sources_to_targets`: rider → every survivor;
   - one bicycle `sources_to_targets`: every survivor → destination.

   Two HTTP calls, whatever the candidate count. Then a full `route()` for the
   winner's geometry only, when `geometry: true`.

**`src/valhalla.py` has no matrix helper today** — `route`,
`trace_attributes`, `status`, and the trip accessors. Adding
`valhalla.matrix(sources, targets, costing_options)` over
`/sources_to_targets` is the single largest efficiency decision in this
program and should land as its own small, tested PR ahead of the endpoint.
**Prerequisite to verify before committing to this design:** that the deployed
Valhalla image serves `/sources_to_targets` and that the matrix honours the
same costing options as `route` — if it does not, fall back to the
`ThreadPoolExecutor` fan-out `_score_alternates` already uses, capped at 4
workers, with `limit` dropped to 3.

**Known, acceptable inaccuracy.** The matrix returns a duration under the
default costing; the route the rider is eventually *shown* comes from
`/route`, which re-ranks alternates by bikeway share and may pick a different
road. So the ranking number and the displayed ETA can disagree by a few
percent. That is fine, and better than the alternative (routing everything),
but it must be true in one direction only: the displayed ETA is the honest
one, and where they differ the response carries the routed figure, not the
matrix figure, for whatever it actually routed.

### 6.3 Scoring

```
trip_seconds = walk_seconds + ride_seconds
score        = trip_seconds
             + penalty_quality        (risk tier, failed starts, negative reports)
             + penalty_preference     (each unmet PREFERRED spec item)
             - bonus_equity           (Phase 4a, in seconds-equivalent of money saved)
```

Everything is in **seconds**, including the money, so there is exactly one
scale and no weight-tuning folklore. `recommend.ts`'s current normalized-score
approach (`PRIORITY_WEIGHT = 15`, `OTHER_WEIGHT = 0.5`) stays where it is —
that drawer answers "which of these is best from here", a different question —
but the two must not disagree about which vehicle is *unrideable*, so the
disqualification predicates are shared, not reimplemented.

Penalties are minutes a rider would plausibly trade. Starting figures, to be
argued with and then measured: `risk` tier +6 min (or disqualify under
`ok-only`), each failed start +90 s, unmet preferred feature +2 min, unknown
model +30 s.

### 6.4 The client's cheap tier

`along-the-way.ts` runs the *same* ranking with straight lines and no network,
over whatever the map already has. It is what renders the list instantly, and
what keeps working offline and past the rate limit. The server endpoint then
corrects it. Both must agree on **disqualification** (a vehicle the client
struck out must not reappear from the server); they are allowed to disagree on
**order**, which is what the correction is for.

---

## 7. Phase 3 — Claim and swap

### 7.1 The state machine (`trip-plan.ts`, frontend)

```
      ┌──────────┐  candidate chosen   ┌──────────┐   arrived
      │ SEARCHING├────────────────────►│ CLAIMED  ├──────────────► HANDED OFF
      └────▲─────┘   + dibs registered └────┬─────┘               (ride mode)
           │                                │ device-watch: gone
           │  replacement found             ▼
      ┌────┴─────┐                     ┌──────────┐
      │RECLAIMING│◄────────────────────┤   LOST   │
      └────┬─────┘                     └──────────┘
           │ nothing meets the spec, even relaxed
           ▼
       EXHAUSTED  (hand back the map, say what was tried)
```

Held in memory, not persisted — the same reasoning `pending-trip.ts` records
for itself, and for the same reason: a trip plan resurrected tomorrow is a bug
nobody reports. `pending-trip.ts` stays what it is (a one-shot intent from the
home bar); `trip-plan.ts` is what that intent becomes once a vehicle is
chosen.

### 7.2 The swap, step by step

On `onGone(reason)`:

1. Add the lost vehicle to `exclude`. It is excluded for the rest of the trip,
   even if it reappears — a scooter that went and came back within four
   minutes is one somebody is riding in a circle, or a feed artefact, and
   either way it has already cost this rider a walk.
2. **Release the claim before claiming anything** —
   `POST /api/v1/dibs/{id}/release`. Order is load-bearing:
   `DIBS_MAX_CONCURRENT = 3` counts the rider's *other* claims too, so a
   claim-then-release swap can be refused at the ceiling by its own
   predecessor.
3. Re-search from the rider's **current** position — not the origin. They have
   been walking; the corridor has moved.
4. Decide: auto-claim, or ask (§7.3).
5. Claim with `replaces: <old_dibs_id>` (§7.4).
6. Fire **one** message.

### 7.3 The auto-accept envelope

Auto-claim only when **all** of these hold:

- every **must** in the spec is met, with nothing relaxed;
- `trip_seconds` is no more than **5 minutes** worse than the plan it replaces;
- the routed walk is within `DIBS_MAX_WALK_MINUTES`;
- this is at most the rider's **second** swap on this trip.

Otherwise: notify, and show the best candidate **pre-selected** with one tap
to accept and one to open the list. A third loss is not a fourth swap — it is
a sign the search is wrong for this corridor, and the app should say so rather
than march the rider to a fourth kerb.

Every auto-swap is undoable for as long as it is on screen, and every swap
card names what changed: *"Cosmo → Astro. No basket (you preferred one).
3 min further."*

**Two ceilings that are easy to conflate.** `DIBS_MAX_TOTAL_MS` (25 min) is
per *claim*, and a fresh claim gets a fresh window. The **trip** has no such
cap today, so a twice-swapped rider can spend 40 minutes not riding. The
two-swap budget above is what bounds it; it is a product rule, not a
consequence of the dibs rules, and it belongs in `trip-plan.ts` where it can
be seen.

### 7.4 API — `sql/081_dibs_swap_chain.sql` and `POST /api/v1/dibs`

```sql
ALTER TABLE dibs
    ADD COLUMN IF NOT EXISTS replaces_dibs_id TEXT REFERENCES dibs(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS release_reason   TEXT;   -- 'taken' | 'swapped' | 'rider' | NULL
```

`POST /api/v1/dibs` accepts an optional `replaces`. When present, the handler
**releases the named claim and inserts the new one in one transaction** —
`expires_at = NOW(), release_reason = 'swapped'` on the old row, exactly the
release semantics `dibs_release` already uses (expire, never delete: the row
is evidence behind a certificate somebody may already have been shown). One
transaction, because a swap that half-applies leaves a rider holding two
claims or none, and both are worse than the failure.

What this buys, beyond tidiness:

- The certificate page can say *"the second scooter on this trip"* — which is
  a better story than the first one, and the certificate is a front door.
- It is the **only** way to answer "does the swap actually work?" — how often
  a claim is taken, how often a replacement is found, how much worse it was.
  Without the link, a swap is indistinguishable from a rider who changed their
  mind.

`release_reason` is free text validated in `src/api_dibs.py`, per the house
convention `sql/043` and `sql/077` both follow (no enums in the schema).

### 7.5 Notifications — the fifth alert, and the one it replaces

`dibs-notify.ts` fires four alerts, at most once each, and its own header
records why there are not five: *"a phone that buzzes five times in twenty-five
minutes about a scooter is a phone that gets its notifications turned off."*

So the swap does not add a fifth buzz to the four. It **replaces** `taken`
when a replacement is in hand:

- replacement found, auto-accepted → `swapped`, and `taken` never fires;
- replacement found, needs a decision → `swap_offer`, and `taken` never fires;
- nothing found → `taken` fires as it does today, and the app hands back the
  map with the search it tried.

Implementation note: the loss and the replacement resolve on different ticks
(the search is a network call). Hold `taken` for **one tick** when a search is
in flight, then fire whichever is true. A one-tick delay is invisible; two
buzzes are not.

Draft copy, in the voice of the existing four:

- `swapped` — `🔁 Someone took Lunar 🐸 928. You're on Cosmic 🦊 214 now — 3 min from you, still gets you there.`
- `swap_offer` — `🔁 Lunar 🐸 928 is gone. Nearest match that fits: Cosmic 🦊 214, 6 min. Tap to take it.`

### 7.6 Phase 3b — the "upgrade", off by default

Same corridor search, run on a slow cadence while walking, offering a swap
*before* anything is lost. Gated hard, or it is nagging:

- only when the current target **fails a must** the new one meets (its battery
  dropped below the floor, a report just landed against it), **or** the new one
  saves ≥ 5 minutes of trip;
- at most **once** per trip;
- never after arrival;
- never within 90 s of a previous card.

This is the sub-feature the program is named after, and also the one most
likely to be wrong. It ships last, off, and behind telemetry that can answer
whether anyone accepts it.

---

## 8. Phase 4 — Cost-aware routing through Equity Areas

### 8.1 The arithmetic, stated plainly

Exhibit A §5.2 obliges Veo to discount *"any trip that starts or ends within a
designated Equity Area"*; Exhibit C prices that at **$1 + $0.13/min**. Against
the rider's own tier (`config.ts`):

| Tier | Base | 15-min ride, base | 15-min ride, Equity Area rate |
|---|---|---|---|
| Resident | $1 + 25¢/min | $4.75 | $2.95 |
| Visitor | $1 + 39¢/min | $6.85 | $2.95 |

**Two different moves, and they are not equally good.**

**4a — start inside an Equity Area.** One unlock, no split, no extra risk:
walk a little further to a vehicle that is already inside the polygon and the
*whole trip* is discounted. For a resident on a 15-minute ride that is **$1.80
for a couple of extra minutes of walking**, and it falls straight out of the
Phase 2 scorer as a `bonus_equity` term — money converted to
seconds-equivalent, so the ranking stays one number. This is the safe, large,
obvious win and it should ship first.

Note what needs no work at all: a trip whose *destination* is already inside an
Equity Area is discounted however it starts. The optimizer must recognize that
and stay quiet.

**4b — stop over inside an Equity Area.** End the ride inside the polygon,
start a new one there. Both legs then start-or-end in an Equity Area, so both
are discounted — at the cost of a second unlock and the restart.

Break-even, with `t` the riding minutes and `d` the minutes added by the
detour and the restart faff:

```
saving = (base_per_min − 13¢) × t  −  13¢ × d  −  $1.00 (second unlock)
```

- Resident (25¢): worth it past **~8.3 riding minutes**, at zero detour.
- Visitor (39¢): worth it past **~3.9 minutes**.
- **Access tier: never offered.** See §3.

And the cheapest case is free: **if the direct route already crosses an Equity
Area, `d = 0`** and the only cost is the second unlock. So the search is two
tiers, and the first is nearly free to compute — sample the route geometry the
app already has against the bundled polygons (`equity-areas.ts`'s
`isInEquityArea`, which is already how the on-screen indicator works) and see
whether it is already inside one. Only if not does it cost a second routing
call to test a detour.

### 8.2 Four things this must be honest about

1. **We cannot promise the discount.** The app's own
   `EQUITY_DISCOUNT_NOTICE` already tells riders to screenshot the receipt if
   they do not see it. A feature that advises a *behaviour change* on the
   strength of that discount inherits the caveat and must state it at the
   point of advice, not in a drawer: **"this should cost $X. If Veo bills you
   the base rate, screenshot it."**
2. **Two rentals is a real risk, not just a fee.** Between ending leg one and
   starting leg two, somebody can take the scooter. Dibs does not prevent
   that — nothing does. The stopover card must say so, and the honest
   mitigation is the Phase 2 search: show whether *another* vehicle meeting
   the spec is standing in that Equity Area before advising the split.
3. **VeoPlus is unmodelled.** Whether the Pass waives the Equity Area's $1
   unlock is not stated in Exhibit C, and `config.ts` deliberately declines to
   infer it. The optimizer must price the **worse** reading (unlock charged)
   and never show a saving that depends on the better one.
4. **Whose discount is it.** The Equity Area rate exists to serve people in
   those areas; a rider detouring through one to shave a fare is not the
   intended beneficiary, though the contract's language ("any trip") plainly
   covers them. Worth a deliberate product decision rather than a default —
   and worth noting the argument on the other side, that routing more trips
   through Equity Areas leaves more vehicles there, which is the thing the
   30% deployment target is chasing anyway. **Flagged for the owner; not
   settled here.**

### 8.3 API shape

`src/equity_savings.py` + a `savings` block on the candidate response, rather
than a new endpoint: the question "what will this cost" is asked about a
candidate, and answering it anywhere else means the answer can disagree with
the vehicle it is about. `/api/v1/trip/candidates` gains:

```jsonc
"savings": {
  "plan": "resident",
  "direct_cents": 475,
  "best": {
    "kind": "start_in_equity_area",     // or "stopover" | "none"
    "cents": 295, "saves_cents": 180, "adds_seconds": 130,
    "stopover": null,                    // { lat, lon, area_id } for a split
    "caveats": ["discount_not_guaranteed"]
  }
}
```

The polygons are already server-side (`data/equity.geojson`, boundary layer
`equity`, `src/equity_groups.py`'s `OFFICIAL_GROUP`) and client-side (bundled
`public/equity-areas.geojson`, geometry-identical by test). Neither side needs
new geometry — which is the whole reason this phase is small.

---

## 9. House duties this program owes

Per `FEATURE_PLAN_2026-07.md` "Sequencing" and the module headers:

- **Every PR:** endpoint-table row in `README.md`, full request/response shapes
  and error codes in `API.md`, a status row in `API_REQUIREMENTS.md`, new env
  vars in **both** `.env.example` and `docker-compose.yml`, a comment block in
  `crontab` for any new job.
- **Migrations:** idempotent, applied in sorted order at boot, recorded in
  `schema_migrations`; never an inline `CHECK` inside `ADD COLUMN IF NOT
  EXISTS` — use the guarded named-constraint shape from `sql/040`–`042` and
  `sql/050`. `tests/test_migration_replay_pg.py` must keep passing.
- **Three-address rule** (`src/api_meta.py` header): any new stored field is a
  retention rule. `sql/081`'s `release_reason` and `replaces_dibs_id` need
  `src/cli.py`, `src/api_meta.py:_PRIVACY`, and
  `src/templates/legal/privacy_policy.html` updated **together**. Phase 5, if
  it ever stores a live rider position, is a much bigger version of this
  conversation and should not be started casually.
- **Telemetry allowlist is mirrored by hand** in two repos —
  `denver-scooter-fyi/src/telemetry.ts`'s `TELEMETRY_EVENTS` and
  `src/api_telemetry.py`'s `ALLOWED_EVENTS`. New events (`trip_plan_start`,
  `trip_swap`, `trip_swap_offer`, `trip_exhausted`, `equity_savings_shown`,
  `equity_savings_taken`) must land in both, in the same PR, and carry no free
  text — the existing contract is a fixed name plus enumerated props.
- **Even-points invariant:** this program awards no points. If it ever does
  (a stand-down-style award for taking a stopover, say), every award must be
  even — `CHECK (points % 2 = 0)` on `user_points`, the assertion in
  `credit_points()`, and the sweeping unit test.
- **Tests:** fake-cursor unit tests by default; `*_pg.py` are integration tests
  gated on `VEO_TEST_PG_DSN`; one test file per module.

---

## 10. Risks, in the order they are likely to bite

| # | Risk | Mitigation |
|---|---|---|
| 1 | **The phone is in a pocket and the tab is throttled.** The whole swap runs client-side in Phase 3; a backgrounded tab may not poll, and a closed app certainly does not. | Ship Phase 3 knowing it: the feature works while the app is open, which is the case for a rider actively walking with the arrival panel up. Say so in the UI. Phase 5 (server-side plan + Web Push, or SMS via the existing `comms.py`, which already has consent and quota) is the real fix and should be scoped on Phase 3's measured swap rate, not before it. |
| 2 | **Auto-dibs makes dibs worse for everyone.** Dibs' own rules exist to stop hoarding; a feature that claims automatically is exactly the pressure they were written against. | The swap always releases before it claims, so a trip holds at most one claim ever. The two-swap budget bounds the total. Watch the ratio of claims to rides in telemetry, and be willing to turn auto-claim off. |
| 3 | **Valhalla has no matrix, or its matrix disagrees with its routes.** The two-call design is load-bearing for Phase 2's cost. | Verify against the deployed image **before** building the endpoint. Fallback is the 4-worker `ThreadPoolExecutor` fan-out already used by `_score_alternates`, with `limit` cut to 3. |
| 4 | **The corridor search is expensive and rate-limited.** A swapping rider re-searches every few minutes; `/route`'s bucket is 30/min per IP, shared with everything else routing. | Two Valhalla calls per search, `limit ≤ 5`, the client's straight-line tier carrying the interactive list, and the server call reserved for the moment a decision is made. |
| 5 | **A swap chain walks somebody in a circle.** Each individual step is locally optimal; three of them may not be. | Re-search from current position (so progress is never thrown away), permanent `exclude`, and the two-swap budget. Telemetry on total walk metres per trip is the check that this is true. |
| 6 | **Spec too tight = nothing found**, and "no scooters match" reads as "no scooters". | The published relaxation ladder, `relaxed` on every response, and an EXHAUSTED state that says what was tried and offers the one-tap loosening. |
| 7 | **Equity advice that costs money.** Wrong tier, unmodelled Pass, a discount Veo does not apply. | Never for Access; price the worse VeoPlus reading; carry the screenshot caveat at the point of advice; never advise a split whose saving is under $0.50. |
| 8 | **Notification fatigue kills the alert that matters.** | Swap messages *replace* `taken`, never stack with it. One-tick hold. Same four-per-claim ceiling. |
| 9 | **`recommend.ts` and the new scorer disagree in front of the rider** — the drawer's top pick is not the trip's top pick. | They answer different questions and are allowed to differ in order. They share disqualification predicates and must never differ on what is rideable. Consider retiring the drawer's ranking onto the corridor scorer once Phase 2 is proven. |

---

## 11. What "done" looks like per phase

- **1.** A rider can write down what they will ride, name it, and have it on
  their other phone. Nothing else changes.
- **2.** Planning a trip returns vehicles ranked by when they will get you
  there, and the list changes correctly when you change the destination — a
  scooter behind you drops down it.
- **3.** A rider walks to a scooter, somebody takes it, and before they notice
  they are walking to a different one, told once, with the difference named.
  The dibs chain in the database can say how often that happened and whether
  it worked.
- **4a.** A rider who would save real money by starting inside an Equity Area
  is told, in dollars, next to the extra walking minutes it costs.
- **4b.** A long trip that already crosses an Equity Area offers the split,
  with the second unlock, the re-rent risk, and the screenshot caveat all on
  the same card as the saving.
