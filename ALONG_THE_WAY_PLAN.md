# Along the Way — program plan (master) + API lane

Planned 2026-08-29 against `main` (3e0236d). Branch:
`claude/along-way-upgrades-feature-piml2p`.
**Revision 2** — scope expanded: the spec becomes a rider-facing "ideal
scooter" that applies to the map in one tap (§5.5), and **My Scooters**
(favourite individual vehicles, gated behind a QR scan) joins as Phase 4 (§8).
Changed decisions are marked **REVISED** in §3.

This is the **master** document for the program: the vision, the vocabulary,
the decisions, the phasing, and the risks. The second half is the **API lane**
(this repo). The frontend lane is
`denver-scooter-fyi/docs/ALONG_THE_WAY_PLAN.md` — a companion, not a
duplicate; where the two must agree, this file is the one that is right.

Nothing here is built yet. This is a plan to be argued with.

---

## 1. What we are building

A rider says **what they like to ride** and **where they are going**. The app
finds the vehicle that gets them there best, claims dibs on it, walks them to
it — and when somebody takes it out from under them, finds the next one
**along the route to their destination**, claims that instead, and tells them
without the rider having to take the phone out of their pocket.

Five parts, in the order they matter:

1. **The ideal scooter.** Kind of device, required features, minimum quality,
   minimum battery — stated once, as requirements rather than as map filters,
   and applied to the map in one tap whenever the rider wants to *look* at
   only the ones that qualify.
2. **The corridor search.** Rank candidates by *how long the whole trip
   takes* — walk to the vehicle **plus** ride to the destination — not by how
   far the vehicle is from the rider. That single change is what "along the
   way" means: a scooter 500 m further *towards* where you are going beats one
   300 m in the wrong direction.
3. **The swap.** Dibs on the chosen vehicle; watch it; when it goes, release,
   re-search from where the rider is *now*, claim the replacement, and say so
   in one message.
4. **My Scooters.** A rider who has physically stood at a vehicle and scanned
   its QR code can keep it — name it, find it again later, be told when it is
   free. Gated on the scan, and deliberately blind while somebody is riding
   it (§3, §8.4).
5. **Cost.** Later and separately: route the trip to cost less, by starting it
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
| **Camera QR scanner + server-side scan validation + a +100 pt first-scan bonus** | `qr-scan.ts`, `qr-zxing.ts`, `src/api_qr.py`, `src/qr.py`, `sql/032` |
| Saved *places* (local, named, capped at 12) | `favorites.ts` |
| Equity Area geometry + the discount's meaning | `data/equity.geojson`, `equity-areas.ts`, `ride-cost.ts` |
| Rider preference blobs (opaque, server-stored, capped) | `sql/043`, `sql/050`, `src/api_preferences.py` |
| Server-side per-cycle watcher pattern | `src/ride_watch.py` |
| SMS out, with consent and quota handled upstream | `src/comms.py` |

**The gap this program fills, precisely.** Today, when `device-watch.ts` fires
`onGone`, `main.ts` clears the walk line and puts a sentence in the arrival
panel (`main.ts:3257`). That is the whole recovery story: the rider is told
their scooter is gone and handed back a map. Everything below exists to
replace that dead end with the next scooter — and, now, to let a rider keep
the ones they liked.

---

## 2. Vocabulary

**Ideal scooter** (rider-facing) / **Spec** (in code) — a rider's stated
requirements for a vehicle, with each requirement marked **must** or
**prefer**. Distinct from a *filter*, which decides what is drawn on the map.
A filter hides; a spec disqualifies and ranks. They are different objects with
a one-tap bridge between them (§5.5).

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
browser; Phase 6 asks whether it should live on the server.

**Favourite / My Scooters** — a specific vehicle a rider has kept, after
proving at the kerb that they were standing at it. Not a claim, not a
reservation, and not a subscription to where it goes.

---

## 3. Decisions already taken

| Question | Decision | Why |
|---|---|---|
| Rank by walk distance, or by whole-trip time? | **Whole-trip time.** `walk(P→v) + ride(v→D)`. | It is the definition of "along the way", and it subsumes the reach question for free — a vehicle that cannot reach `D` has no finite ride leg. |
| One endpoint for "find me one" and "find me another"? | **One.** `POST /api/v1/trip/candidates` with an `exclude` list. | A replacement search is the first search from a new position with one vehicle struck out. Two endpoints would be the same code twice, drifting. |
| Route every candidate? | **No.** Two Valhalla *matrix* calls rank the whole corridor exactly; a full route is computed only for what the rider is actually shown. | Routing 40 candidates individually is 40 calls against an endpoint rate-limited at 30/min. `sources_to_targets` is one call for many pairs. |
| Is the spec a new kind of saved filter preset? | **REVISED — still a separate object, but with a first-class two-way bridge to the map filters.** See §5.5. | The reasons for separateness hold (presets are localStorage-only, carry map-only state, and have no place for must/prefer). But "these are my requirements" and "show me only those" are the same thought ten seconds apart, and making the rider re-enter it in a second UI was the wrong call. The bridge is one tap each way and lossy in one stated direction. |
| Does a swap auto-claim, or ask? | **Auto-claim inside a defined envelope, ask outside it.** See §7.3. | The rider is walking with the phone away. A question they cannot see is not a safer default than an action they can undo in one tap. |
| Does the swap raise a second notification after "it's gone"? | **No — one message, or two, never both.** | `dibs-notify.ts` caps itself at four alerts per claim on purpose. A swap that buzzes twice in three seconds spends the budget that protects "RUN!". |
| Does the certificate change? | **It gains a chain link** (`replaces_dibs_id`), nothing else. | The certificate is an assertion about one vehicle at one time. A swap makes a *new* claim; it does not extend the old one. |
| Persist the trip plan server-side in v1? | **No.** Phase 3 is client-only. | A live position + destination stored server-side is a new retention rule (three-address rule, §10) and a much larger privacy conversation than the feature needs to prove itself. |
| Proactive "upgrade" offers (a better vehicle appears mid-walk)? | **Behind a gate, in Phase 3b, off by default.** | The feature is named "upgrades" and the machinery is identical, but an app that renegotiates the plan while you walk is an app you stop trusting. |
| **What does a QR scan actually prove?** | **NEW — plate knowledge, not presence.** So favouriting requires a valid scan **and** a GPS fix within **75 m** of the device's last known position. | `src/qr.py:validate_scan` checks `hash_plate(payload) == vehicle_identifier`. That proves the scanner has the plate; nothing in `api_qr.py` or `credit_qr_scan_points` compares the submitted `lat`/`lng` to anything. 75 m is the radius the "Unlock in Veo" gate already uses for "physically at the scooter". |
| **Can you watch a favourite move?** | **NEW — no. Position is withheld while `is_reserved` is true.** | See §8.4. This is the single most important rule in Phase 4 and the one most likely to be lost in implementation. |
| **Do we store where the rider was standing when they favourited?** | **NEW — no.** Check the 75 m at write time, then discard the fix. | Storing it buys nothing any feature reads, and every stored position is a retention obligation across three files. The cheapest privacy decision available is not to have the data. |
| **How many favourites?** | **NEW — 10 per account.** | A rider with fifty kept scooters is not keeping favourites, they are running a tracker. Ten is more than anyone needs and few enough to be a list rather than a search. |
| Equity stopover for the `equity` (Access) rate plan? | **Never offered.** | Access is 60 free min/day then 15¢/min with no unlock. The Equity Area rate is $1 + 13¢/min. Whether the two interact is *not stated anywhere in the contract we have* (`config.ts`'s own note), and the plausible readings include ones where the advice costs the rider money. |

---

## 4. Phasing

Each phase is independently mergeable and useful on its own.

| Phase | Ships | API lane | Frontend lane |
|---|---|---|---|
| **1 — The ideal scooter** | Requirements stated once, saved to the account, synced, and **applied to the map in one tap** | `sql/080`, `/api/v1/profile/ride-specs` | `ride-spec.ts`, spec sheet, the map bridge |
| **2 — Along the way** | Corridor ranking; "best vehicle for *this trip*" replaces "nearest vehicle" | `valhalla.matrix()`, `src/trip_candidates.py`, `POST /api/v1/trip/candidates` | `along-the-way.ts`, wired into the home bar's plan flow |
| **3 — Claim & swap** | Auto-dibs, loss detection → replacement → one message | `sql/081` (`replaces_dibs_id`), `replaces` on `POST /dibs` | `trip-plan.ts`, `arrival-panel.ts` swap face, `dibs-notify.ts` 5th alert |
| **3b — Upgrades** *(optional)* | Mid-walk offer when a materially better vehicle appears | — | gate in `trip-plan.ts` |
| **4 — My Scooters** | Keep a vehicle you scanned; find it again; be told when it's free | `sql/082`, `/api/v1/profile/favorite-devices`, availability watch | `my-scooters.ts`, popup action, map layer |
| **5a — Start in an Equity Area** | "Walk 2 min further, save $1.80" | equity flag + cost on candidates | `equity-savings.ts`, candidate chips |
| **5b — Stopover** | Break the trip at an Equity Area when the arithmetic says to | `src/equity_savings.py`, stopover search | two-leg cost UI |
| **6 — Pocket-proof** *(not committed)* | Swap works with the app closed | server-side trip plan + `ride_watch`-style job + Web Push / SMS | service worker |

**Phase 4 has no dependency on 1–3** — it needs only the QR scanner, which
already exists — and could ship at any point after Phase 1. It is listed here
rather than first because it pays off most once the corridor scorer exists to
prefer a rider's own scooters (§8.6), and because Phase 1's spec panel is the
drawer it naturally lives beside. If the goal is something in riders' hands
quickly, **Phase 4 is the cheapest useful thing in this document.**

Phases 1, 2 and 4 are all useful without Phase 3. Phase 3 is the feature the
program is named for.

---

## 5. Phase 1 — The ideal scooter

### 5.1 The object

```jsonc
{
  "models":       ["cosmo", "rover"],   // null = any model
  "features":     ["basket"],           // consensus must be TRUE (null/unknown does not match)
  "min_battery":  40,                   // percent
  "min_quality":  "no-risk",            // "any" | "no-risk" | "ok-only"
  "must_reach":   true,                 // disqualify anything that cannot reach the destination
  "max_walk_minutes": 12,               // <= 15 whenever auto-dibs is on; see 7.2
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
Phase 3 is signed-in anyway. Phases 1, 2 and 5 are not; Phase 4 is, because
the QR scan endpoint already is.)

### 5.5 The map bridge — **"Show me only these"**

*(This section is the revision. The first draft kept the spec and the map
filters strictly apart and made the rider express the same thing twice.)*

They stay **two objects**, because they answer to different owners: the map
filters carry `area`, `hideUnavailable` and `rideTypes`, live only in
`localStorage`, and change constantly as a rider pans and pokes around. A spec
is an account-level statement about what you will ride. Fusing them would mean
narrowing the map to look at something quietly changes what the app walks you
to two minutes later.

But the bridge is one tap in each direction, and it is a first-class part of
the feature rather than an export button:

- **Spec → map.** A toggle on the Filters drawer *and* on the spec sheet:
  **"Show only my ideal scooters."** Projects the spec onto the live filter
  state, carrying `area` and `rideTypes` through untouched and forcing
  `hideUnavailable` ON — availability is the one requirement a spec never
  relaxes, so a view under that label containing a scooter somebody is riding
  is a false label. (That corrects this document's first draft, which carried
  `hideUnavailable` through with the rest.)

  The projection is **lossy in two directions**, and only the first is worth a
  rider's attention: the map has no way to draw "preferred", so **musts and
  prefers both become plain filters**, which is what the toggle's helper line
  says — *"the map can only show or hide; your preferences are treated as
  requirements here."* The second is that the result is a **superset** of what
  the spec accepts, because a model filter keeps mystery hardware visible
  while the spec rejects it; that one lives in the code's own doc comment.
- **Map → spec.** From the Filters drawer: **"Save these as my ideal
  scooter."** Seeds a new spec from the current filter state, drops the
  map-only fields, and opens the sheet with everything marked *prefer* — the
  rider promotes what is actually non-negotiable. Defaulting to `must` would
  put a hard requirement on the rider's behalf that they never stated.
- **Attachment and detachment,** the standard preset pattern: while the toggle
  is on, the drawer shows which spec is driving it; any manual filter change
  detaches, says so, and offers one tap back. A filter silently claiming to be
  a spec it no longer matches is the bug this rule exists to prevent.

Nothing about `filter-presets.ts` changes. Saved filter presets and saved
specs coexist, and a rider who never opens the spec sheet sees no difference.

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
    "favorite": { "nickname": "My Rover" },       // Phase 4; null otherwise
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

The `favorite` block is populated only for a session-authed caller, and only
from that caller's own favourites. It never says a vehicle is *somebody
else's* favourite — that is a fact about a person, not about a scooter.

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
             - bonus_favorite         (Phase 4, §8.6)
             - bonus_equity           (Phase 5a, in seconds-equivalent of money saved)
```

Everything is in **seconds**, including the money and the sentiment, so there
is exactly one scale and no weight-tuning folklore. `recommend.ts`'s current
normalized-score approach (`PRIORITY_WEIGHT = 15`, `OTHER_WEIGHT = 0.5`) stays
where it is — that drawer answers "which of these is best from here", a
different question — but the two must not disagree about which vehicle is
*unrideable*, so the disqualification predicates are shared, not
reimplemented.

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

## 8. Phase 4 — My Scooters

A rider who has physically stood at a vehicle can **keep** it: name it, see
where it is later, and be told when it comes free. Ten per account, gated on a
QR scan at the kerb, and blind while somebody is riding it.

### 8.1 Why it is worth building

Riders already have opinions about individual scooters — a particular Rover
whose basket is not bent, the Cosmo at the end of the block that always
starts. The app can currently express *none* of that: every vehicle is
interchangeable, identity is a 16-hex string, and the only per-device memory
anywhere is a dibs claim that dies in 25 minutes. Meanwhile the fleet-level
data this app is built on — features, reliability, battery history — is
exactly what makes one scooter genuinely different from another.

It also completes a loop the app already half-runs: the QR scan pays +100
points once per device (`credit_qr_scan_points`), and Confirm Features needs
the plate under the same sticker. Giving the scan a *lasting* result, instead
of a one-off payout, is the cheapest way to make scanning worth doing twice.

### 8.2 The gate, and what it actually proves

**`validate_scan` proves plate knowledge, not presence.** It computes
`hash_plate(extract_plate(payload)) == vehicle_identifier` and nothing else;
neither `api_qr.py` nor `credit_qr_scan_points` compares the submitted
`lat`/`lng` to the device's position. Anyone who learns a plate can produce a
valid "scan" from their sofa.

That is tolerable for a points bonus. It is not tolerable for a feature whose
whole premise is "you were there", so favouriting requires **both**:

1. a payload that passes `validate_scan` for that `vehicle_identifier`, and
2. a GPS fix within **75 m** of the device's last known position.

75 m is the radius the "Unlock in Veo" gate already uses for "physically at
the scooter", and it is generous for a reason: GBFS positions are up to two
minutes stale, and consumer GPS in a street canyon is routinely 20–30 m out.
The errors do not cancel. A tighter radius rejects honest riders standing with
a hand on the handlebar.

**The gate is anti-abuse and quality, not privacy.** It stops idle
favouriting and bot enumeration; it does **not** stop somebody scanning the
scooter parked outside a person's house. Conflating the two is the mistake
this section exists to prevent — the privacy control is §8.4, and it is a
different mechanism entirely.

Worth noting as a hardening opportunity while this is being built: the
existing `POST /api/v1/devices/qr-scan` could take the same proximity check.
Out of scope here, but it is the same three lines.

### 8.3 `sql/082_favorite_devices.sql`

```sql
CREATE TABLE IF NOT EXISTS favorite_devices (
    id                  BIGSERIAL PRIMARY KEY,
    account_id          BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    vehicle_identifier  TEXT NOT NULL,
    -- The rider's own name for it. Null is fine: the vehicle already has a
    -- name (vehicle_identity.display_name), and "My Rover" is a nicety, not
    -- a requirement.
    nickname            TEXT
                        CONSTRAINT favorite_devices_nickname_length
                        CHECK (nickname IS NULL OR (length(nickname) BETWEEN 1 AND 40)),
    -- THE GATE. When they last proved they were standing at it. Stored as a
    -- TIME, never as a PLACE: the fix is checked against the device's
    -- position at write time and then discarded. See §3.
    verified_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- "Tell me when it's free again." Off by default: a favourite is a
    -- memory, and turning one into a notification is a second decision.
    notify_on_available BOOLEAN NOT NULL DEFAULT FALSE,
    -- Housekeeping, not a feature: set when the vehicle stops appearing in
    -- the feed, so a retired scooter ages out of somebody's list instead of
    -- sitting there as a permanent "gone".
    last_seen_at        TIMESTAMPTZ,
    UNIQUE (account_id, vehicle_identifier)
);

-- "This rider's list, newest first" — the only read the panel makes.
CREATE INDEX IF NOT EXISTS idx_favorite_devices_account
    ON favorite_devices (account_id, created_at DESC);

-- The availability watch's query: every favourite of a vehicle that just
-- became free. Partial, because notify_on_available is opt-in and expected
-- to be a minority.
CREATE INDEX IF NOT EXISTS idx_favorite_devices_notify
    ON favorite_devices (vehicle_identifier)
    WHERE notify_on_available;
```

A dedicated table rather than a `user_preferences` blob: this one has real
cardinality rules, a foreign-key-shaped relationship to a vehicle, and is read
by a per-cycle job. `user_preferences` is for opaque client state the server
never interprets, and its own header says so.

Cap of **10** enforced in code, following `MAX_RIDE_USUALS`'s precedent (and
its reasoning), not in the migration.

**Note on the existing `favorites.ts`:** that module is saved *places* —
"Home", "Work", "the gazebo" — local, capped at 12, no account needed. It is
untouched. The frontend module for this is `my-scooters.ts`, and the two must
not be merged just because the word "favourite" appears in both; one is a
point on a map the rider typed, the other is a vehicle they stood at.

### 8.4 **The rule that matters: you cannot watch a favourite move**

`src/ride_watch.py` records the measurement: *a rented Veo vehicle stays in
the feed for the whole rental, at 2-minute granularity, broadcasting its live
moving position, with `is_reserved` flipping true for the duration.* And
`/api/v1/devices/current` publishes `is_reserved` and the position side by
side, publicly (`src/api_public.py:535`).

So the underlying capability already exists for anyone with a script. What a
favourite would add is a one-tap, persistent, targeted subscription to one
specific vehicle that the rider physically located — which is the difference
between a public dataset and a tool for following a person. Scan the sticker
on the scooter parked outside somebody's house, keep it, watch where it goes
next.

**Therefore: while a favourite is `is_reserved`, its position is not
returned.** Not fuzzed, not delayed — absent.

```jsonc
{ "vehicle_identifier": "…", "nickname": "My Rover",
  "state": "available" | "in_use" | "unavailable" | "gone",
  "lat": …, "lon": …,          // present ONLY when parked (available/unavailable)
  "battery_percent": 71,       // same gate: it is a ride-progress signal
  "last_seen_at": "…",
  "position_withheld": true }  // explicit, so a client cannot read absence as a bug
```

Enforced **server-side**, in the endpoint, so a client bug or a hand-rolled
request cannot get around it. `position_withheld` is an explicit field rather
than a silent omission because a null the client has to guess about is how
this gets "fixed" by somebody six months from now.

**Where the line is, and honestly what it does not cover.** A parked
favourite's position *is* returned, and where a scooter is parked can be
somebody's home. That is already fully public on the map, and hiding it would
delete the feature. The line is drawn at the thing that is both new and
cheaply removable: you may know where it is standing, you may not follow it.
Say that plainly in the UI rather than implying more.

The availability notification carries **no location** for the same reason —
`🛴 My Rover is free again` and nothing else. The rider opens the app to see
where, which they were going to do anyway.

### 8.5 API — endpoints

```
GET    /api/v1/profile/favorite-devices          the list, with live state per §8.4
POST   /api/v1/profile/favorite-devices          keep one — requires a fresh scan
PATCH  /api/v1/profile/favorite-devices/{vid}    nickname, notify_on_available
DELETE /api/v1/profile/favorite-devices/{vid}
```

`POST` takes **the scan payload itself**, not a "I already scanned this" flag:

```jsonc
{ "vehicle_identifier": "…", "qr_raw_value": "…",
  "lat": 39.7392, "lng": -104.9903, "nickname": "My Rover" }
```

Identical in shape to `POST /api/v1/devices/qr-scan`, and deliberately so — it
reuses `validate_scan` and adds the 75 m check from §8.2. A client-asserted
"already verified" boolean is a gate that lives on the wrong side of the
network.

It also runs the same `credit_qr_scan_points` path, which is already
once-per-(account, vehicle) and advisory-locked: favouriting a device the
rider has never scanned earns the +100 exactly once, favouriting one they have
already scanned earns nothing, and there is no way to double-pay. (100 is
even; the even-points invariant holds.)

Session-authed throughout. Rate-limited per account on the existing
`enforce()` bucket pattern — the QR endpoint's 20/hour is the right
neighbourhood.

Errors worth naming explicitly in `API.md`: `qr_mismatch` (400),
`too_far_from_device` (403, with the metres), `unknown_device` (400),
`favorite_limit_reached` (409, naming the cap), `already_favorited` (200,
idempotent — re-scanning an existing favourite refreshes `verified_at` rather
than failing, because a rider standing at their own scooter pressing the
button again has not made a mistake).

### 8.6 What favourites do elsewhere

- **Corridor ranking (§6.3).** `bonus_favorite` — a modest one. Starting
  figure: **90 seconds**, i.e. a rider will walk about a minute and a half
  further for a scooter they already like. Big enough to break a tie, small
  enough that it never beats a genuinely better trip. It is a preference, not
  a filter: a favourite that fails a `must` is still disqualified.
- **The map.** A "My Scooters" filter chip, and favourites drawn with a
  distinct marker whether or not the chip is on — the whole point is being
  able to spot yours.
- **The device popup.** A ⭐ action beside ☑️ Confirm Features, which opens the
  scanner. Offered again at the end of a successful QR scan and a features
  confirmation ("Keep this one?"), because those are the two moments the rider
  is already standing there with the camera open.
- **Dibs.** No change. A favourite can be claimed like anything else, and a
  favourite is not a claim.

### 8.7 The availability watch

`notify_on_available` needs a per-cycle job, and `src/ride_watch.py` is the
pattern to copy exactly: called from `cycle.py:run_once()` after
`device_state.update_for_cycle`, wrapped by the caller in try/except (a
failure here must never fail the cycle), and driven by a **targeted indexed
query, not a full table scan** — the partial index in §8.3 exists for this.

The transition is narrow: a vehicle that was `is_reserved`/absent last cycle
and is available this cycle, which somebody has favourited with
`notify_on_available`. Delivery in Phase 4 is the same in-app + Notification
API path `dibs-notify.ts` already uses; SMS via `comms.py` is a Phase 6
question and carries its own consent and quota conversation.

Caps, so this cannot become a firehose: **at most one availability alert per
favourite per 6 hours**, and none at all between 22:00 and 07:00 Denver time.
A scooter that gets ridden four times a day must not buzz somebody four times.

New `crontab` comment block if any part of this ends up scheduled separately
rather than riding the cycle (house rule).

---

## 9. Phase 5 — Cost-aware routing through Equity Areas

### 9.1 The arithmetic, stated plainly

Exhibit A §5.2 obliges Veo to discount *"any trip that starts or ends within a
designated Equity Area"*; Exhibit C prices that at **$1 + $0.13/min**. Against
the rider's own tier (`config.ts`):

| Tier | Base | 15-min ride, base | 15-min ride, Equity Area rate |
|---|---|---|---|
| Resident | $1 + 25¢/min | $4.75 | $2.95 |
| Visitor | $1 + 39¢/min | $6.85 | $2.95 |

**Two different moves, and they are not equally good.**

**5a — start inside an Equity Area.** One unlock, no split, no extra risk:
walk a little further to a vehicle that is already inside the polygon and the
*whole trip* is discounted. For a resident on a 15-minute ride that is **$1.80
for a couple of extra minutes of walking**, and it falls straight out of the
Phase 2 scorer as a `bonus_equity` term — money converted to
seconds-equivalent, so the ranking stays one number. This is the safe, large,
obvious win and it should ship first.

Note what needs no work at all: a trip whose *destination* is already inside an
Equity Area is discounted however it starts. The optimizer must recognize that
and stay quiet.

**5b — stop over inside an Equity Area.** End the ride inside the polygon,
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

### 9.2 Four things this must be honest about

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

### 9.3 API shape

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

## 10. House duties this program owes

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
  retention rule. Both `sql/081` (`release_reason`, `replaces_dibs_id`) and
  **`sql/082` in full** need `src/cli.py` (cleanup/de-id), `src/api_meta.py:
  _PRIVACY`, and `src/templates/legal/privacy_policy.html` updated
  **together**. `favorite_devices` is the more consequential of the two: it is
  a durable, account-linked record of *which specific vehicles a named person
  has physically stood at*, which is a stronger statement than anything else
  in the database. Deciding not to store the scan position (§3) is what keeps
  it from being stronger still. Phase 6, if it ever stores a live rider
  position, is a much bigger version of this conversation and should not be
  started casually.
- **Telemetry allowlist is mirrored by hand** in two repos —
  `denver-scooter-fyi/src/telemetry.ts`'s `TELEMETRY_EVENTS` and
  `src/api_telemetry.py`'s `ALLOWED_EVENTS`. New events (`trip_plan_start`,
  `trip_candidates`, `trip_swap`, `trip_swap_offer`, `trip_exhausted`,
  `spec_applied_to_map`, `spec_saved_from_map`, `favorite_added`,
  `favorite_removed`, `favorite_available_alert`, `equity_savings_shown`,
  `equity_savings_taken`) must land in both, in the same PR, and carry no free
  text — the existing contract is a fixed name plus enumerated props. **No
  `vehicle_identifier` in any of them**: that would attach a device to a
  session in the one system deliberately built to hold no persistent
  identifier.
- **Even-points invariant:** Phase 4 awards points only through the existing
  `credit_qr_scan_points` (100, even). Any new award must be even —
  `CHECK (points % 2 = 0)` on `user_points`, the assertion in
  `credit_points()`, and the sweeping unit test.
- **Tests:** fake-cursor unit tests by default; `*_pg.py` are integration tests
  gated on `VEO_TEST_PG_DSN`; one test file per module.

---

## 11. Risks, in the order they are likely to bite

| # | Risk | Mitigation |
|---|---|---|
| 1 | **A favourite becomes a way to follow a person.** In-use vehicles broadcast a live moving position on a public endpoint; a targeted subscription to one is a different thing from a public map. | §8.4: position withheld server-side whenever `is_reserved`, an explicit `position_withheld` flag so nobody "fixes" it later, no location in the availability alert, a 10-favourite cap, and the QR gate on top. Write the rule into the endpoint's docstring the way `sql/076` writes down what dibs is not. |
| 2 | **The QR gate proves less than it looks like it proves.** `validate_scan` is a plate-knowledge check; nothing today compares position. | §8.2: require the 75 m proximity check as well, and say in the code comment why the scan alone is not enough — otherwise the next feature to reuse the gate inherits the wrong assumption. |
| 3 | **The phone is in a pocket and the tab is throttled.** The whole swap runs client-side in Phase 3. | Ship Phase 3 knowing it: the feature works while the app is open, which is the case for a rider actively walking with the arrival panel up. Say so in the UI. Phase 6 (server-side plan + Web Push, or SMS via `comms.py`, which already has consent and quota) is the real fix and should be scoped on Phase 3's measured swap rate. |
| 4 | **Auto-dibs makes dibs worse for everyone.** Dibs' own rules exist to stop hoarding; a feature that claims automatically is exactly the pressure they were written against. | The swap always releases before it claims, so a trip holds at most one claim ever. The two-swap budget bounds the total. Watch the ratio of claims to rides in telemetry, and be willing to turn auto-claim off. |
| 5 | **Valhalla has no matrix, or its matrix disagrees with its routes.** The two-call design is load-bearing for Phase 2's cost. | Verify against the deployed image **before** building the endpoint. Fallback is the 4-worker `ThreadPoolExecutor` fan-out already used by `_score_alternates`, with `limit` cut to 3. |
| 6 | **The corridor search is expensive and rate-limited.** | Two Valhalla calls per search, `limit ≤ 5`, the client's straight-line tier carrying the interactive list, and the server call reserved for the moment a decision is made. |
| 7 | **A swap chain walks somebody in a circle.** | Re-search from current position, permanent `exclude`, and the two-swap budget. Telemetry on total walk metres per trip is the check. |
| 8 | **Spec too tight = nothing found**, and "no scooters match" reads as "no scooters". | The published relaxation ladder, `relaxed` on every response, and an EXHAUSTED state that says what was tried and offers the one-tap loosening. |
| 9 | **The map bridge desynchronizes.** A filter set that still claims to be "my ideal scooter" after the rider changed it is a lie the UI is telling. | §5.5's attach/detach rule, and the lossy direction stated on the toggle rather than discovered. |
| 10 | **Availability alerts become a firehose.** A popular scooter turns over several times a day. | One alert per favourite per 6 hours, none 22:00–07:00 Denver, opt-in per favourite and off by default. |
| 11 | **Equity advice that costs money.** Wrong tier, unmodelled Pass, a discount Veo does not apply. | Never for Access; price the worse VeoPlus reading; carry the screenshot caveat at the point of advice; never advise a split whose saving is under $0.50. |
| 12 | **Notification fatigue kills the alert that matters.** | Swap messages *replace* `taken`, never stack with it. One-tick hold. Same four-per-claim ceiling. Availability alerts are a separate, capped, opt-in channel. |
| 13 | **`recommend.ts` and the new scorer disagree in front of the rider.** | They answer different questions and may differ in order. They share disqualification predicates and must never differ on what is rideable. Consider folding the drawer onto the corridor scorer once Phase 2 is proven. |

---

## 12. What "done" looks like per phase

- **1.** A rider can write down what they like to ride, name it, have it on
  their other phone — and see only those on the map with one tap, with the
  drawer honest about the fact that it is showing them as requirements.
- **2.** Planning a trip returns vehicles ranked by when they will get you
  there, and the list changes correctly when you change the destination — a
  scooter behind you drops down it.
- **3.** A rider walks to a scooter, somebody takes it, and before they notice
  they are walking to a different one, told once, with the difference named.
  The dibs chain in the database can say how often that happened and whether
  it worked.
- **4.** A rider standing at a scooter can keep it in two taps, find it again
  a week later, and be told when it comes free — and cannot, by any request
  the API will answer, see where it is while somebody is riding it.
- **5a.** A rider who would save real money by starting inside an Equity Area
  is told, in dollars, next to the extra walking minutes it costs.
- **5b.** A long trip that already crosses an Equity Area offers the split,
  with the second unlock, the re-rent risk, and the screenshot caveat all on
  the same card as the saving.
