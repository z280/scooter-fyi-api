# Along the Way — program plan (master) + API lane

Planned 2026-08-29 against `main` (3e0236d). Branch:
`claude/along-way-upgrades-feature-piml2p`.
**Revision 2** — scope expanded: the spec becomes a rider-facing "ideal
scooter" that applies to the map in one tap (§5.5), and **My Scooters**
(favourite individual vehicles, gated behind a QR scan) joins as Phase 4 (§8).

**Revision 3 — Phases 2 and 3 were built on a misreading, and are rewritten.**
Revisions 1–2 had the rider *walking* to the scooter that matched their spec.
The original ask was "plan to **ride** to a device that meets their
specifications": you walk a few seconds onto whatever is near, ride it toward
your destination, and **hand off** to the one you wanted at a point already on
your way. §6 and §7 are new; §6.0 states the correction. Revision 3 also
adds three product rules that were missing — likely-rideable always, cost and
time on every plan, and the Access Program's free-minute budget — and folds
optional Phase 3b into the one re-solve path. Phases 1, 4 and 5 are unchanged
and Phase 1 has since shipped.

Changed decisions are marked **REVISED** in §3; new ones are marked **NEW**.

This is the **master** document for the program: the vision, the vocabulary,
the decisions, the phasing, and the risks. The second half is the **API lane**
(this repo). The frontend lane is
`denver-scooter-fyi/docs/ALONG_THE_WAY_PLAN.md` — a companion, not a
duplicate; where the two must agree, this file is the one that is right.

**Phase 10 reaches a third repository**, `zNeill/keepdenverfair`, which already
runs the advocacy sites and their inbound mail. Nothing before Phase 10 touches
it.

**Phase 1 shipped** (API #92, frontend #82) and **Phase 4 shipped** (#90).
The rest is a plan to be argued with.

---

## 1. What we are building

A rider says **what they like to ride** and **where they are going**. The app
plans the trip that gets them there best — which is usually *not* a long walk
to the perfect scooter, but a short walk onto whatever is closest, a ride
toward the destination, and a **hand-off to the scooter they actually wanted
at a point already on their way**. It claims dibs on that pickup while they
ride toward it, and when the plan is disrupted — the pickup is taken, a better
one appears, the battery is draining faster than estimated — it re-solves the
rest of the route, tells them once, and lets them overrule it from plans it
has already worked out.

Ten parts, in the order they matter:

1. **The ideal scooter.** Kind of device, required features, minimum quality,
   minimum battery — stated once, as requirements rather than as map filters,
   and applied to the map in one tap whenever the rider wants to *look* at
   only the ones that qualify.
2. **The hand-off plan.** The trip is multi-leg: walk a few seconds onto
   whatever is near and rideable, ride it toward the destination, pick up the
   scooter that matches the spec **en route**, ride that the rest of the way.
   The good scooter is a waypoint, not a walk target. Ranked by whole-trip
   generalised cost — time *and* money, including every unlock.
3. **The living plan.** Dibs on the next vehicle while riding toward it, and a
   plan that re-solves itself whenever reality moves: the pickup taken, a
   better option appearing, the battery short, the rider behind schedule. It
   acts, says so once, and offers the runners-up it had already computed.
4. **My Scooters.** A rider who has physically stood at a vehicle and scanned
   its QR code can keep it — name it, find it again later, be told when it is
   free. Gated on the scan, and deliberately blind while somebody is riding
   it (§3, §8.4).
5. **Cost.** Later and separately: route the trip to cost less, by starting it
   inside an Equity Area, or by breaking it at one.
6. **One app, one mode.** The four parts above all add surface. This one
   removes it: the leftover scaffolding of the old mode bar, a second model
   filter that means the opposite of the first, a spec the ride screen never
   reads, and every question still asked per ride whose answer never changes.
   No endpoint, no migration — just the friction between "the map" and "a
   ride" taken out.
7. **The walkthrough.** The intro tour is switched off right now because it
   describes an app that moved. A tour is a description, and you write the
   description once the thing has stopped changing.
8. **The receipt.** Veo is obliged to discount any trip starting or ending in
   an Equity Area, and nobody checks. A rider drops in a receipt, learns
   whether they were charged correctly, and gets a complaint ready to send —
   and, with consent, the answers aggregate into the one question nobody can
   currently answer: *is the discount actually being applied?*
9. **Reaching the rider.** A plan that changes while the phone is in a pocket
   is a plan the rider never hears about. Opted-in SMS for the two things
   worth interrupting someone for, a live check of the handful of vehicles a
   plan actually depends on, and a link that puts them back where they were —
   after signing in again if they have to.
10. **Advocacy.** A rider sending a complaint can ask for somebody in their
    corner. `advocacy@weseeyouveo.com` goes on the CC if they tick the box,
    and replies into a case only ever happen when the case asks for them.

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

### The four things that sound alike, and the one question each answers

This program adds a *spec*, and the app already had *filters*, *presets* and
*Usuals*. Four nouns, all of them some flavour of "what I want", and the only
reliable way to tell them apart is by the question each one answers:

| | The question | Where it lives | Scope |
|---|---|---|---|
| **Filters** | what is drawn on the map *right now*? | the Filters drawer, in memory | this session |
| **Preset** | a filter set worth reusing | `filter-presets.ts`, `localStorage` | this browser |
| **Spec** | what will I **ride**? | `user_preferences` kind `ride_spec` | the account |
| **Usual** | how should the ride **screen** behave while I ride? | `user_preferences` kind `ride_mode_usual` | the account |

Two things follow that are worth saying out loud, because getting either
backwards is how this vocabulary rots:

1. **A preset is not a small spec.** A preset has no `must`, no relaxation
   order and no opinion about whether a vehicle is acceptable — it is a
   remembered *view*. Promoting one to a spec is the map bridge (§5.5), and
   it is lossy in the stated direction.
2. **A Usual is not a spec for the screen.** It never mentions a vehicle. The
   spec chooses the scooter; the Usual dresses the screen you look at once
   you are on it. They are stored apart because riders change them on
   different occasions — the spec when their needs change, the Usual when
   their habits do.

**The retired fifth.** `user_preferences` carried a kind called
`find_ride_pref` from `sql/043`: a single unnamed blob meaning "what am I
willing to ride". That is exactly what `ride_spec` means, and nothing was
ever built on it — three endpoints, no caller, in either repo. Keeping it
would have left the table with two answers to one question and no rule for
which one wins. It is **retired in `sql/082`**, rows and all, and its
endpoints are gone rather than deprecated. Phase 1 therefore ships with
`user_preferences` holding exactly three kinds, each answering a different
question.

**Hand-off** — ending one rental and starting another mid-trip, at a vehicle
that was already on the route. The move this whole program is named for.
Costs an unlock (free on three of the five tiers, §6.3) and a minute or two of
overhead, and buys the rest of the trip on a better scooter.

**Plan** — a sequence of legs: `walk → ride → [hand-off → ride]* → walk`. The
unit the trip search returns and the unit the rider chooses between. A
single-vehicle trip is a plan with one ride leg, competing in the same list.

**Pickup** — a vehicle a plan hands off *to*. It is claimed while the rider
rides toward it, which is what makes the plan trustworthy.

*(**Corridor** was revision 2's word for a walking catchment around the
straight line. It is retired with the misreading that produced it — see
§6.0.)*

**Generalised cost** (the ranking scalar) — every leg's seconds, plus money
converted to seconds, plus preference penalties. One scale for time, money and
taste, so there is no weight to tune. Money is genuinely in it: an unlock fee
is why a hand-off might not be worth taking.

**Claim** — one dibs row. Twenty-five minutes at the outside, per `sql/076`
and `dibs.ts`. Not a reservation, not a hold, and this program must never
describe it as one.

**Re-solve** — recomputing the *remaining* legs from where the rider is now,
and moving the claim to whatever the new plan needs. Triggered by anything
that invalidates the plan, of which "somebody took it" is only one (§7.2).
Never recomputes just the next vehicle: that is what strands a rider on a
route that no longer makes sense.

**Trip plan** — the live document tying a spec, a destination, the remaining
legs, the current claim, the backup plans and the re-solve history together.
Phase 3 keeps it in the browser; Phase 9 asks whether it should live on the
server.

**Favourite / My Scooters** — a specific vehicle a rider has kept, after
proving at the kerb that they were standing at it. Not a claim, not a
reservation, and not a subscription to where it goes.

---

## 3. Decisions already taken

| Question | Decision | Why |
|---|---|---|
| Rank by walk distance, or by whole-trip time? | **REVISED (rev 3) — by whole-trip GENERALISED COST over a multi-leg plan**: every leg's seconds, plus money, plus preference penalties. | Whole-trip time was right and the *trip* was wrong: rev 1–2 modelled one walk and one ride. A plan can hand off, so the scalar has to price an unlock as well as a minute. See §6.0. |
| **Does the rider walk to the scooter that matches their spec?** | **NEW (rev 3) — no. They ride to it.** A short walk onto whatever is near, then a hand-off to the spec-matching vehicle at a point already on the route. | This is the correction that produced revision 3. The original ask said "plan to **ride** to a device that meets their specifications"; rev 1–2 read "ride" as "walk" and built a feature whose answer to "the good one is 14 minutes away" was "then walk 14 minutes". |
| **May a `risk`-tier vehicle appear in a plan?** | **NEW (rev 3) — no, with one narrow escape.** Only when there is no non-risky vehicle within a 5-minute walk, and then the app says that is why. | Rev 2 priced it as a +6-minute penalty, which lets a planner sell the platform's whole proposition for four saved minutes. Rideability is the product. |
| **How many hand-offs may a plan have?** | **NEW (rev 3) — unbounded, limited by cost and time**, never by a hop counter. | The money term limits it by itself, and does so correctly per tier: a `resident` rider pays $1 a hop and will rarely see two; an Access or Pass rider pays nothing and should not be stopped by an arbitrary constant. §6.3. |
| One endpoint for "find me one" and "find me another"? | **One.** `POST /api/v1/trip/candidates` with an `exclude` list. | A replacement search is the first search from a new position with one vehicle struck out. Two endpoints would be the same code twice, drifting. |
| Route every candidate? | **No.** Two Valhalla *matrix* calls rank every leg of every plan exactly; a full route is computed only for what the rider is actually shown. | Routing 40 candidates individually is 40 calls against an endpoint rate-limited at 30/min. `sources_to_targets` is one call for many pairs. |
| Is the spec a new kind of saved filter preset? | **REVISED — still a separate object, but with a first-class two-way bridge to the map filters.** See §5.5. | The reasons for separateness hold (presets are localStorage-only, carry map-only state, and have no place for must/prefer). But "these are my requirements" and "show me only those" are the same thought ten seconds apart, and making the rider re-enter it in a second UI was the wrong call. The bridge is one tap each way and lossy in one stated direction. |
| Does a re-solve auto-claim, or ask? | **REVISED (rev 3) — always auto-claim, always say so, always let the rider overrule it** from backups already computed. The envelope is withdrawn. | The reasoning was right and did not go far enough: the rider is *riding*, not walking. A question they cannot safely read is never the safer default, so there is no bound at which asking becomes correct. §7.1. |
| Does the swap raise a second notification after "it's gone"? | **No — one message, or two, never both.** | `dibs-notify.ts` caps itself at four alerts per claim on purpose. A swap that buzzes twice in three seconds spends the budget that protects "RUN!". |
| Does the certificate change? | **It gains a chain link** (`replaces_dibs_id`), nothing else. | The certificate is an assertion about one vehicle at one time. A swap makes a *new* claim; it does not extend the old one. |
| Persist the trip plan server-side in v1? | **No.** Phase 3 is client-only. | A live position + destination stored server-side is a new retention rule (three-address rule, §15) and a much larger privacy conversation than the feature needs to prove itself. |
| Proactive "upgrade" offers (a better vehicle appears mid-trip)? | **REVISED (rev 3) — not a separate phase. It is one trigger among several** on the same re-solve path (§7.2), and it is safe because the rider can always overrule it. | Rev 2 made it optional Phase 3b because an app that renegotiates unprompted is one you stop trusting. What makes it trustworthy is the undo, not the gate — and once the plan is live for other reasons, gating this one costs a branch and buys nothing. |
| **What does a QR scan actually prove?** | **NEW — plate knowledge, not presence.** So favouriting requires a valid scan **and** a GPS fix within **75 m** of the device's last known position. | `src/qr.py:validate_scan` checks `hash_plate(payload) == vehicle_identifier`. That proves the scanner has the plate; nothing in `api_qr.py` or `credit_qr_scan_points` compares the submitted `lat`/`lng` to anything. 75 m is the radius the "Unlock in Veo" gate already uses for "physically at the scooter". |
| **Can you watch a favourite move?** | **NEW — no. Position is withheld while `is_reserved` is true.** | See §8.4. This is the single most important rule in Phase 4 and the one most likely to be lost in implementation. |
| **Do we store where the rider was standing when they favourited?** | **NEW — no.** Check the 75 m at write time, then discard the fix. | Storing it buys nothing any feature reads, and every stored position is a retention obligation across three files. The cheapest privacy decision available is not to have the data. |
| **How many favourites?** | **NEW — 10 per account.** | A rider with fifty kept scooters is not keeping favourites, they are running a tracker. Ten is more than anyone needs and few enough to be a list rather than a search. |
| **Two account-level "what I want to ride" objects?** | **NEW — no. `find_ride_pref` is retired in `sql/082`**, rows, endpoints and all. `ride_spec` is the one answer. | They meant the same thing. A table with two answers to one question and no rule for which wins is a bug waiting for its first caller — and `find_ride_pref` never had one, in either repo, so retiring it costs nothing and deleting the ambiguity is the whole point. |
| **Do Phases 6 and 7 go before the feature phases?** | **NEW — no, they go last, in that order.** | Phase 7 writes a description of the UI; Phase 6 changes the UI. Doing either earlier means doing it twice, and a walkthrough that is wrong on the day it ships teaches a new rider things they have to unlearn. |
| **Do we model the Access Program's free minutes?** | **NEW (rev 3) — yes: estimate from tracked rides, label the direction of the error, and let the rider correct it.** §6.3.1. | Assuming the free hour is spent is right for a live ticker and wrong for planning — it prices a free trip as a paid one and talks the rider out of the hand-off they should take. |
| Equity stopover for the `equity` (Access) rate plan? | **Never offered.** | Access is 60 free min/day then 15¢/min with no unlock. The Equity Area rate is $1 + 13¢/min. Whether the two interact is *not stated anywhere in the contract we have* (`config.ts`'s own note), and the plausible readings include ones where the advice costs the rider money. |

---

## 4. Phasing

Each phase is independently mergeable and useful on its own.

| Phase | Ships | API lane | Frontend lane |
|---|---|---|---|
| **1 — The ideal scooter** | Requirements stated once, saved to the account, synced, and **applied to the map in one tap** | `sql/080`, `/api/v1/profile/ride-specs` | `ride-spec.ts`, spec sheet, the map bridge |
| **2 — The hand-off plan** | Multi-leg plans: ride a near vehicle, pick up the one you wanted en route. Ranked by time **and** money, every unlock priced | `valhalla.matrix()`, `src/trip_plans.py`, `POST /api/v1/trip/candidates` | `along-the-way.ts`, free-minutes control, wired into the home bar's plan flow |
| **3 — The living plan** | Dibs on the next vehicle while riding to it; the plan re-solves on any disruption, says so once, and offers the backups | **the next free migration** (`replaces_dibs_id`) — check `sql/`, do not trust a number written here; `replaces` on `POST /dibs`, time-to-arrival claim bound | `trip-plan.ts`, `arrival-panel.ts` re-solve face, backups sheet, `dibs-notify.ts` 5th alert |
| **4 — My Scooters** | Keep a vehicle you scanned; find it again; be told when it's free | `sql/081` ✅, `/api/v1/profile/favorite-devices`, availability watch | `my-scooters.ts`, popup action, map layer |
| **5a — Start in an Equity Area** | "Walk 2 min further, save $1.80" | equity flag + cost on candidates | `equity-savings.ts`, candidate chips |
| **5b — Equity hand-off** | Hand off inside an Equity Area when the arithmetic says to — **not a separate search**, just an equity-priced leg in the Phase 2 planner (§9) | `src/equity_savings.py` as a cost term | equity chip on the plan card |
| **6 — One app, one mode** | The seams between "the map" and "ride mode" close; entering a ride stops being a mode change | — (frontend-only) | `#mode-switch` seam removed, one model filter, the spec carried into the HUD |
| **7 — The walkthrough** | A first-time rider is shown the app that actually exists, and the tour auto-shows again | — (frontend-only) | `onboarding.ts` rewritten against the home bar, `ONBOARDING_AUTOSHOW` back on |
| **8 — The receipt** | Was this trip charged per Exhibit C? Complaint ready to send — and, consented, an evidence pile that can answer whether the discount is applied at all | receipt submissions migration, aggregate endpoint, three-address rule in full | on-device OCR, confirm-what-we-read, copy-the-complaint |
| **9 — Reaching the rider** | Opted-in SMS when the plan changes under you; 20-second checks on plan-critical vehicles; a resume link that survives losing your session | trip-alert consent, server-side plan, targeted upstream check, `comms.py` | alert opt-in, resume deep link, foreground-bounded checking |
| **10 — Advocacy** | Opt-in CC to `advocacy@weseeyouveo.com`, a review portal, and replies into a case **only when invited** by `@WSYV` / `@advocacy` | **mostly built** in `zNeill/keepdenverfair` (§14.1): what remains is mention detection, the reply guard, and an operator alert | the CC tick, per complaint |

**Phase 4 has no dependency on 1–3** — it needs only the QR scanner, which
already exists — and could ship at any point after Phase 1. It is listed here
rather than first because it pays off most once the plan search exists to
prefer a rider's own scooters (§8.6), and because Phase 1's spec panel is the
drawer it naturally lives beside. If the goal is something in riders' hands
quickly, **Phase 4 is the cheapest useful thing in this document.**

Phases 1, 2 and 4 are all useful without Phase 3. Phase 3 is the feature the
program is named for.

**Phase 3b is gone** (rev 3). Mid-trip "upgrade" offers were an optional phase
because renegotiating a plan unprompted felt untrustworthy; under §7.1 every
re-solve is announced and reversible, so an upgrade is just one more trigger
on a path that already exists.

**Phase 9 was "Pocket-proof" and is now committed.** The name described the
problem and dodged the mechanism; the mechanism is SMS, and most of it already
exists (§13.1). Phase 10 is new.

**Phases 8, 9 and 10 are the three that add stored data** — receipt
submissions, a live trip plan, and an advocacy mailbox. Between them they more
than double this program's privacy surface, and each carries the three-address
rule in full.

**Phases 6 and 7 are in this order and at this end for one reason.** Phase 7
rewrites a tour that *describes the UI*; Phase 6 *changes the UI*. Writing the
tour first would mean writing it twice, and shipping a tour that is wrong on
the day it lands is worse than shipping no tour — it is the first thing a new
rider sees, and everything it teaches wrongly they have to unlearn. So the UI
stops moving, then the tour describes it. Phase 6 is also the only phase with
no API lane at all: it closes seams inside the frontend and adds no endpoint,
no migration and no stored field.

---

## 5. Phase 1 — The ideal scooter

### 5.1 The object

```jsonc
{
  "models":       ["cosmo", "rover"],   // null = any model
  "features":     ["basket"],           // consensus must be TRUE (null/unknown does not match)
  "min_battery":  40,                   // percent
  "min_quality":  "no-risk",            // "any" | "no-risk" | "ok-only"
  "must_reach":   true,                 // disqualify anything that cannot reach ITS OWN leg's end (§5.2)
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
   `must_reach` when set. A vehicle that cannot reach the end of **the leg it
   is assigned** is not a worse candidate, it is not a candidate.

   **"Its own leg", not "the destination" — this is a revision 3 correction
   and it is load-bearing.** Under the old single-vehicle reading the two were
   the same thing. They are not any more: a scruffy Astro with 1.5 km of range
   is a perfectly good *starter* when its hand-off is 1.2 km away, and
   disqualifying it for not reaching `D` removes exactly the vehicles the
   hand-off plan is built to use. It would also make §7.2's battery trigger
   unreachable, since a plan whose legs are all individually reach-feasible is
   the only kind that could ever be built.

   So `canReach` is evaluated per ride leg, against that leg's endpoint — the
   next pickup, or the drop-off node `X` for the final leg (§6.2). The
   "unknown never disqualifies" rule is unchanged: a vehicle whose range the
   feed never reported still passes, because not knowing is not knowing it
   cannot.
2. `min_battery`, down to the reach-feasible floor and no further.
3. Preferred `features`, dropped one at a time, cheapest-signal first.
4. `models`, widened to the same form factor (standing → standing).
5. `min_quality`, but **never below `no-risk` automatically**. Handing a rider
   a vehicle our own signals call high-risk, without asking, is the one
   relaxation that can end a trip worse than not finding anything.

Every response says what it relaxed. Every swap card shows it.

### 5.3 API — `sql/080_ride_specs.sql`

Next free migration number is **085** at the time of writing, and **this line
goes stale fast — check `sql/` rather than trusting it.** It has drifted three
times during this program's planning alone, so nowhere else in this document
names a migration number for unbuilt work: phases refer to "the next free
migration" and the implementer looks. `080` is this phase's `ride_specs`, `081` is
Phase 4's `favorite_devices`, `082` retires `find_ride_pref` (§2), and `083`
(`device_history_departure_reason`) and `084` (the equity calendar's fifth
status) arrived from outside this program while it was being planned. Note
`069` is used twice already; do not add a third.

`user_preferences.kind` carries a named CHECK constraint listing the allowed
kinds. Extend it with the **exact guarded shape `sql/050` established** — read
`pg_get_constraintdef`, test for the new value's presence, drop and re-add
only if absent. Do not use `ADD COLUMN IF NOT EXISTS` with an inline CHECK
anywhere (house rule; silently skipped when the column exists).

```
kind IN ('saved_map_settings', 'find_ride_pref', 'ride_mode_usual', 'ride_spec')
-- …and then sql/082 dropped 'find_ride_pref' again, see below
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

## 6. Phase 2 — The hand-off plan

### 6.0 What this phase is, and the misreading it corrects

**Revision 3 rewrites this phase and §7 completely.** Revisions 1–2 had the
rider *walking* to the vehicle that matched their spec, and ranked candidates
by `walk(P→v) + ride(v→D)`. That was a misreading of the original ask —
"plan to **ride** to a device that meets their specifications" — and it
produced a feature whose answer to "the good scooter is 14 minutes away" was
"then walk 14 minutes".

The real shape:

```
        90 s walk        6 min ride          9 min ride       1 min walk
 you ──────────────▶ ASTRO ──────────▶ COSMO (your spec) ──────────▶ door
                   (whatever's near)   (picked up EN ROUTE)
```

You walk a few seconds to whatever is closest and acceptable, ride it toward
where you are going, and **hand off** to the scooter you actually wanted at a
point that is already on your way. The spec-matching vehicle is a **waypoint
on the route**, not a walk target. Walking appears only twice and is short
both times: onto the first vehicle, and off the last one to the door.

That is what "along the way" was always supposed to mean. The word
**corridor** is retired with the misreading — it described a walking catchment,
and this is a route with pickups on it.

### 6.1 The five rules this phase obeys

These come from the product owner and override anything inherited from
revisions 1–2.

1. **Likely-rideable, always.** A `risk`-tier vehicle is **not offered**, at
   any leg of any plan. This is not a ranking penalty — revision 2 had it as
   "+6 minutes" and that was wrong. Rideability is the reason this platform
   exists, and a planner that routes somebody onto a scooter we have flagged
   as risky to save them four minutes has sold the whole proposition for four
   minutes.

   **The single exception is a FIRST HOP, and the asymmetry is deliberate.**
   When there is genuinely no non-risky vehicle within a **5-minute walk**,
   the app may offer a risky one as the vehicle you start on, and says plainly
   that it is doing so because there is nothing else nearby.

   It does **not** extend to pickups. The escape exists so a rider with only
   a risky scooter near them is not simply told "no plans"; a *pickup* has no
   such argument, because by then the rider is already moving and refusing a
   risky hand-off costs them a shorter chain, not the trip. So: risky first
   hops under the stated condition, never a risky pickup, and §6.2's
   candidate selection has to implement exactly that rather than excluding
   `risk` everywhere (which is what an earlier draft of it did, making this
   exception unreachable).
2. **Always show estimated cost AND time, including startup costs.** Every
   plan, every leg, every time. A hand-off's entire case is that an extra
   unlock is worth it, and that case cannot be made without the number.
3. **Chaining is unbounded, limited by cost and time** — never by a hop
   counter. §6.4 shows why the money term does the limiting by itself.
4. **Be sensitive to the free-unlock tiers**, and to the Access Program's
   free-minute budget in particular. §6.3.
5. **On a disruption, resolve it automatically, say so, and let the rider
   overrule you** from the backup plans already computed. §7.

### 6.2 The search: a graph, and it is small

Nodes are the origin `P`, the destination `D`, **a drop-off node `X` near
`D`**, and the candidate vehicles. Edges are legs:

| Edge | Mode | How it is measured |
|---|---|---|
| `P → Sᵢ` | walk | one pedestrian matrix, 1 source × N targets |
| `Sᵢ → Sⱼ` | ride | the bicycle matrix below |
| `Sᵢ → X` | ride | folded into the same bicycle matrix, as one extra target |
| `X → D` | walk | the final few metres; straight-line is fine |
| `P → D` | walk | the degenerate plan: no vehicle at all, when that is genuinely best |

**`X` exists because without it the graph cannot express the trip this phase
illustrates.** An earlier draft had both terminal edges starting at a pickup
node and ending at `D` — a ride *or* a walk from the last scooter, never a
ride *then* a walk. So the picture (`walk → ride → ride → walk`) and the graph
described two different trips. A drop-off node near `D`, with the final
walking edge starting there and bounded by its own short cap, fixes that: a
rider parks where parking is legal and walks the last block, which is what
actually happens.

In practice `X` is chosen from the legal-parking geometry the app already
knows, or falls back to `D` itself when nothing better is available — in which
case the final walk leg is zero-length and the plan reads exactly as it used
to.

So it is still **two Valhalla calls**, as revision 2 promised — but the second
one is now `N sources × (N+1) targets` rather than `N × 1`. That is the real
cost of this design and the plan should not pretend otherwise.

**What keeps N small** — and all three of these are rules we wanted anyway:

- **Rule 1 prunes hardest.** Non-`risk` vehicles only, except for the
  first-hop escape in §6.1 — which fires when nothing else is within a
  5-minute walk, and therefore only when the alternative set was empty anyway.
- **The first hop must be a short walk.** Only vehicles inside the walk cap
  can be `S₁`, which is a handful, not the fleet. Note what the spec's
  `max_walk_minutes` now MEANS: it bounds the walk onto the **first** vehicle,
  not a walk to the vehicle the rider wanted. Its default of 12 minutes was
  chosen under the old reading and is now generous for what it governs —
  worth revisiting once there is real usage, not before.
- **The bbox** is the envelope of `P` and `D`, expanded by the walk cap. A
  vehicle behind the rider and off the line is not a node.

**Those three rules do not, by themselves, bound N**, and an earlier draft
claimed they pruned it to ~30. They do not: the walk cap restricts only which
vehicles can be `S₁`, so every non-`risk` vehicle in the bbox remains a
*downstream* node. A long or dense trip can therefore produce a large N and
quadratic matrix work.

So the candidate set needs an **explicit bounded selection step**, before any
matrix call:

1. take every vehicle in the bbox, keeping its reliability tier;
2. **first hops** — the best `W` non-`risk` vehicles by walk seconds inside
   the walk cap. **If there is no non-`risk` vehicle within a FIXED five-minute
   walk**, and only then, refill from the `risk`-tier vehicles inside the walk
   cap and set `risk_tier_offered` on the response. This is rule 1's exception,
   and this step is the only place in the search that admits a `risk` vehicle
   at all.

   **Five minutes, not `max_walk_minutes`** — which riders set from 1 to 15.
   Testing the rider's own cap would admit a risky vehicle whenever the cap is
   tight: a 3-minute cap with a non-risky vehicle 4 minutes away is not "no
   non-risky vehicle nearby", and rule 1 says it must not be treated as one.
   Rule 1 is a **platform** rule; the cap is a **rider preference**; they
   cannot share a radius.

   **And when the gap opens** — non-risky vehicles beyond the cap but inside
   five minutes — **relax the cap, never rule 1.** The cap is on §5.2's
   relaxation ladder and rule 1 is not, so the answer is the 4-minute non-risky
   vehicle with its relaxation disclosed, never a risky 2-minute one and never
   an empty list;
3. **pickups** — the best `H` **non-`risk`** vehicles, ranked by how much
   ride-leg they remove: `ride(Sᵢ→D)` ascending, which is the "further along"
   test. No exception here, ever (§6.1);
4. `N = |W ∪ H|`, with `W` and `H` **constants chosen against the deployed
   matrix's own limits**, not against a guess.

   **And they have to reach the client tier, which cannot import them.** The
   two repos share no runtime module, and these values are not fixed until the
   matrix has been measured — so §6.5's tier gets them **on the candidates
   response** as a typed field, caches them for the session, and falls back to
   a documented default when it has never had one or the call failed:

   ```jsonc
   "bounds": { "first_hops": 8, "pickups": 12 }   // echoed on every response
   ```

   **The cold-start default is the same pair, and it is provisional.** `N ≤ 20`
   makes the second matrix call `20 × 21 = 420` pairs, which is the figure to
   measure the deployed matrix against; when the measured values land they
   replace the default **in both plans at once**. A default written in only one
   of them is two tiers disagreeing about what was *considered*, which rule 2
   of §6.5 forbids.

**Step 2 and step 3 differ on purpose**, and a reader tempted to unify them
should read §6.1's second paragraph first: a risky vehicle you walk to is
sometimes the only trip available, and a risky vehicle you hand off to never
is.

Only then is the matrix one call and the search a **shortest path** over a
graph whose size is known in advance. Dijkstra over a few dozen nodes is
microseconds; the bound is what makes that sentence true rather than hopeful.

**Verify the deployed matrix's maximum source and target counts before
choosing `W` and `H`.** Valhalla's `/sources_to_targets` has configurable
limits and the deployed image's are unknown to this document.

**Prerequisite, unchanged and now more load-bearing:** verify the deployed
Valhalla serves `/sources_to_targets` and honours the same costing options as
`/route`, **before** building this. `src/valhalla.py` has no matrix helper
today; adding `valhalla.matrix(sources, targets, costing_options)` is the
single largest efficiency decision in the program and should land as its own
small, tested PR. If the matrix is not there, the fallback is the
`ThreadPoolExecutor` fan-out `_score_alternates` already uses — but note that
a fan-out over N² pairs is not viable, so without a matrix this phase must
drop to **one hand-off maximum** and a bipartite search rather than a graph.

### 6.3 The money term, and why Access is the tier this feature is for

Edge cost is not seconds. It is **generalised cost**: seconds, plus money
converted to seconds, plus preference penalties. One scale, as before.

The per-hop money cost, across the tiers `config.ts` already models:

**One exception, and it is the Equity Area rate.** An equity-priced leg's
unlock comes from `EQUITY_AREA_RATE`, **not from the rider's tier**: `$1 +
13¢/min` is a rate, and whether a Pass waives *that* dollar is exactly what
Exhibit C does not say. §9.1 already requires the **worse (charged)** VeoPlus
reading for the second unlock, so pricing it per tier in the search would have
the planner and the disclosure disagree about one leg, with the planner on the
optimistic side.

| Tier | Unlock | Cost of one extra hop |
|---|---|---|
| `equity` (Access) | $0 | **nothing** |
| `resident_plus`, `visitor_plus` (Pass) | $0 | **nothing** |
| `resident` | $1 | $1 + tax, per hop |
| `visitor` | $1 | $1 + tax, per hop |

**Three of the five tiers pay nothing to hand off.** That is why rule 3 needs
no hop counter: the money term limits chaining by itself, and for the tiers
where hopping is free it correctly declines to limit it at all. A `resident`
rider will rarely see a plan with two hops in it, and will not need to be told
why.

**The Access Program's free hour is a CLIFF, not a slope.** 60 free minutes a
day, then 15¢/min with no unlock. So today's 58th minute is free and the 62nd
costs money. For a rider near the end of that hour a faster route is worth
disproportionately more than it is to anybody else, and a planner that prices
minutes linearly misses this completely.

Handling it correctly: **the free-minute balance goes into the search state.**
A node is `(location, free minutes consumed so far)`, and an edge's money term
is computed from the balance on arrival.

**This replaces an earlier shortcut that was simply wrong.** Revision 3 said
to solve twice — once pricing every minute free, once pricing every minute
paid — and take the cheaper. Neither search need find the optimum, and the
counterexample is not exotic. With 10 free minutes left and a paid minute
scored at 2 minutes of time, three plans given as (total minutes, billable
ride minutes):

| Plan | Total | Billable | Paid | **True score** | All-free search | All-paid search |
|---|---|---|---|---|---|---|
| A | 12 | 12 | 2 | **16** | 12 ← picks this | 36 |
| B | 14 | 8 | 0 | **14** | 14 | 30 ← picks this |
| C | 13 | 10 | 0 | **13** ✅ | 13 | 33 |

The all-free search picks A, the all-paid search picks B, and the optimum is
C — which neither returns, so repricing their winners cannot recover it. The
error is structural: uniform pricing destroys the very trade-off (spend more
total minutes to stay inside the free balance) that the cliff creates.

**The state space is small enough that there is no excuse.** The free budget
is 60 whole minutes, so the augmentation is at most 61 copies of a graph with
~N nodes — still trivially Dijkstra-able, and exact. Equivalently, keep
nondominated `(time, free-minutes-consumed)` labels per node. Either way,
**rank crossing trips by their actual charges**, never by a regime assumed in
advance.

For a rider with no free balance (every tier but Access, or an Access rider
who has spent the hour) the money term is linear and the augmentation
collapses to one layer — the ordinary search, at no extra cost.

#### 6.3.0 The exchange rate, which this document had been assuming without stating

"Money converted to seconds" is not implementable until the conversion is a
number. Without one, two correct implementations of this plan rank the same
fleet differently, and nothing in either is wrong.

**`SECONDS_PER_CENT = 8`.** One constant, shared by the server tier and the
client tier, because a client that degrades to a different exchange rate
degrades to a different *answer*, not a rougher one.

**It is already in this document.** The counterexample above scores "a paid
minute at 2 minutes of time" — a paid Access minute is 15¢, and 15 × 8 = 120
seconds. That table is arithmetic at this rate, not an illustration, and it
only works because the rate is roughly this high. The constant was implicit;
stating it changes nothing except that it can now be implemented and argued
with.

Both ends of it, in rider terms:

| | Worth |
|---|---|
| A $1 unlock | **13 min 20 s** — so a `resident`'s extra hand-off has to save more than thirteen minutes |
| One free Access minute preserved | **2 minutes** of extra travel — so walking a minute to save a billable minute is worth doing |

**Below about 4 seconds per cent the Access cliff stops changing routes at
all**, and §6.3's state augmentation becomes dead weight. The reason is one
line of arithmetic: staying inside the free hour means walking instead of
riding, which trades one minute of time for one billable minute (15¢). That
trade pays only while 15¢ outranks 60 seconds — i.e. above 4 s/¢. So the rate
is not a free parameter to tune for taste: set it near a
value-of-time-at-minimum-wage figure (about 2 s/¢) and this feature's hardest
piece of machinery would be solving a trade-off that no longer exists.

**It is a policy choice and should be read as one.** 8 s/¢ values an hour of
a rider's time at about $4.50 — low against any wage, and deliberately so: the
riders this is built for are the ones for whom $1 is a real number. The figure
belongs beside `bonus_favorite`'s 90 seconds as a starting value with its
consequence written down, and it is the single number that most changes the
plan list, so it should be reviewed on real trips rather than defended from
this paragraph.

Tests, in both lanes: a plan one unlock cheaper and **13 minutes slower**
wins; the same plan **14 minutes** slower loses. Asserted on the crossover,
because a scalar with no stated rate passes any test you write for it.

#### 6.3.1 "How many free minutes have I got left?"

`config.ts` carries the honest admission that makes this necessary:

> *"the ticker can't know how much of today's free hour is left, so it prices
> minutes beyond 60 and labels the estimate accordingly"*

Assuming the free hour is **gone** is the right pessimism for a live cost
ticker. It is the **worst possible assumption for planning**, because it
prices a free trip as a paid one and talks the rider out of the hand-off they
should have taken. So, three parts:

1. **Estimate** today's used minutes from the rider's own tracked rides.
   `billableMinutes(elapsedMs)` already exists; sum today's.
2. **Label it an estimate, and be specific about the direction of the
   error.** Rides taken outside this app are invisible to it, so our figure is
   a **floor** on minutes used and a **ceiling** on minutes remaining. Never
   present it as authoritative — a rider who trusts "you have 30 minutes left"
   and gets billed has been lied to by a number we invented.
3. **Let the rider correct it while planning.** One control — *"I've got about
   N free minutes left"* — overriding the estimate for this trip. This is also
   the honest resolution of (2): the rider is the only party who actually
   knows, and asking is cheaper and truer than inferring harder.

**Untouched by any of this:** the standing decision never to offer an Equity
**Area** stopover to an Access rider (§3). That is about the *geographic*
discount, whose interaction with the Access tier is genuinely unstated in the
contract. The tier's own pricing, which is what this section models, is stated
plainly in Exhibit C.

### 6.4 `POST /api/v1/trip/candidates` → plans, not vehicles

The response is a ranked list of **plans**. A plan is a sequence of legs; the
single-vehicle trip is simply a plan with one ride leg, and it competes in the
same list rather than being a separate concept.

```jsonc
// request
{
  "from": { "lat": 39.7392, "lon": -104.9903 },
  "to":   { "lat": 39.7508, "lon": -104.9966 },
  "spec": { /* §5.1 */ },
  "exclude": ["<vehicle_identifier>", "..."],
  "rate_plan": "equity",
  "free_minutes_remaining": 30,   // the rider's own answer; null = estimate it
  "limit": 4,                     // plans returned, hard-capped
  "geometry": true,               // routed geometry for the top plan only

  // IN-RIDE STATE — null on an initial search, set on every re-solve (§7).
  // Without this a re-solve cannot compare "keep riding what you are on"
  // against "hand off", and would re-charge an unlock the rider has paid.
  "in_ride": {
    "vehicle_identifier": "…",
    "range_meters": 2400,          // what the CURRENT vehicle can still do
    "unlock_paid": true,           // so continuing costs no unlock
    "started_at": "2026-10-02T01:31:00Z",   // for the free-minute balance
    // Minutes of today's free hour spent BEFORE this rental began. A
    // baseline, not a running total: it cannot age, so it is safe to send.
    "free_minutes_used_before_ride": 6
  }
}
```

**`in_ride` changes the graph, not just the pricing.** On an initial search the
only edges leaving the origin are walks (§6.2). On a re-solve the rider is
*on* a vehicle, so the origin also has a **continuation edge** — keep riding
the current scooter to any reachable node — priced with **no unlock**, because
it is already paid, and bounded by that vehicle's remaining range rather than
a fresh one's.

Omitting this would make every re-solve quietly prefer handing off, since the
option of simply carrying on would not be in the graph to lose.

**`started_at` needs a "now" to be worth anything, and the evaluation instant
is therefore part of the contract.** Today's usage is

```
free_minutes_used_before_ride + billable_minutes(now − started_at)
```

**Through the billable-minute rounding, never a raw subtraction.** Veo bills
the **started** minute — `ride-cost.ts` is `max(1, ceil(ms / 60_000))` and
`billable_minutes` here must agree with it — so a rider 61 seconds in has spent
**2** of the free hour, not 1. Subtracting raw also mixes units, since the
baseline is minutes and the instants are not. Rounding this down would rank the
rider with free minutes they do not have and price a paid minute as free, which
is the single direction §6.3.1 forbids: its whole promise is a **floor** on
minutes used and a **ceiling** on minutes remaining.

**The field is a baseline for a reason.** Revision 3 sent
`free_minutes_used_this_ride` — minutes spent *during* the current rental — and
then aged it by `now − started_at`, which **double-counts** every minute
already in the snapshot: a 6-minute figure measured six minutes in becomes 12.
A baseline measured *before the rental began* is constant for the whole ride,
so ageing it is exactly right and there is no second timestamp to keep
consistent. A re-solve after a **nonzero** baseline is the regression that
catches the double-count; the zero case passes either way, which is why the
bug survived.

The balance still has to be advanced by elapsed time before anything is
priced.
The server does that **once per request, from the instant it receives it**, and
prices the whole search from that single value — never re-reading the clock
mid-search, which would make a plan's rank depend on where in the search it was
reached. The same figure is echoed on the response so a replayed request is
reproducible. The client tier takes the instant as an **injected** input for the
same reason: a search that reads the clock internally ranks identical inputs
differently from run to run, and cannot be tested against the cliff at all.

```jsonc
// response
{
  "plans": [{
    "plan_id": "…",
    "legs": [
      { "mode": "walk", "seconds": 92,  "meters": 118 },
      { "mode": "ride", "seconds": 361, "meters": 1804, "vehicle": { /* … */ },
        "unlock_cents": 0, "minute_cents": 0, "tax_cents": 0,
        "free_minutes_used": 7 },
      { "mode": "ride", "seconds": 540, "meters": 2700, "vehicle": { /* … */ },
        "unlock_cents": 0, "minute_cents": 0, "tax_cents": 0,
        "free_minutes_used": 9 },
      { "mode": "walk", "seconds": 60,  "meters": 78 }
    ],
    "total_seconds": 1053,
    "estimated_cents": 0,
    "estimated_cents_is_estimate": true,
    "free_minutes_after": 14,
    "hand_offs": 1,
    "relaxed": [],
    "why": "Picks up the Cosmo you wanted at 16th & Blake, already on your way."
  }],
  "backups": [ /* the runners-up, kept alive for §7 */ ],
  "considered": 37,
  "risk_tier_offered": false,     // true only under rule 1's 5-minute escape
  "beta_warning": "…"
}
```

**POST, not GET** — unchanged, and now overdetermined: the spec is a
structured object and the request carries a rate plan and a free-minute
figure too.

Rate-limited on the same IP bucket as `/route` (`_limit_route_ip`, 30/min).

**`backups` is not padding.** §7 needs the runners-up to already exist at the
moment something goes wrong, because the rider is *riding* and a search that
starts when the problem is noticed is a search that finishes too late to be
useful.

### 6.5 The client's cheap tier

`along-the-way.ts` runs the same generalised-cost search with straight lines
and no network, over the unfiltered fleet the map already holds
(`devices.allFeatures()`, never `visibleFeatures()` — a rider's leftover map
filters are a view, not a statement of what they will ride). It renders the
list instantly; the server tier corrects it with routed legs at the moment a
decision is made, never on a refresh tick.

**Three things it must be GIVEN rather than find out**, because it is pure and
this document kept implying otherwise:

| Input | Why it cannot be read inside |
|---|---|
| the **evaluation instant** | see §6.4: a search that reads the clock ranks identical inputs differently and cannot be tested against the free-minute cliff |
| the **tax rate** | `ride-cost.ts` holds it as mutable module state. A pure search that reads it ranks against whatever the module currently says, and a client pricing pre-tax while the server prices with tax breaks reconciliation rule 3 below without either being wrong |
| the **free-minute baseline** | §6.4's `free_minutes_used_before_ride`, which does not age |

**And `X` collapses to `D` in this tier, stated rather than left to be
discovered.** §6.2's drop-off node needs legal-parking geometry, which the
client does not have — so the cheap tier ends its last ride leg **at `D`**, and
evaluates `must_reach` for that leg against `D` too. The client's figures therefore **omit the final walk**.

**An earlier draft called that a lower bound. It is not**, and the correction
matters because a false directional guarantee invites a test that enforces it:
the omitted walk pushes the estimate down, while `DETOUR_FACTOR = 1.35` —
**rounded up** rather than averaged, against 1.33 and 1.18 observed
(`reach.ts`) — and a fixed 18 km/h riding pace push it up. Nothing makes them
cancel in a known direction, so a routed leg can come back **shorter** than the
client's. The honest statement is the one `reach.ts` already makes about
itself: it is an estimate and must be labelled one. Reconciliation rule 3 is
what resolves it — the routed answer, which has the geometry, replaces the
estimate at the moment a decision is made.

The reconciliation rules survive revision 2 intact, because they were never
about walking:

1. They may disagree on **order**. That is what the correction is for.
2. They may not disagree on **disqualification**. A vehicle the client struck
   out never reappears from the server.
3. Where they disagree on a **duration or a price**, the routed figure is
   shown. Never an average, and never the cheap one beside the expensive one.
4. A failed or rate-limited call degrades to the client tier with a visible
   "estimated" label, and never blocks the list.

---

## 7. Phase 3 — The living plan

### 7.1 One rule, replacing the auto-accept envelope

Revision 2 had an "auto-accept envelope": claim automatically inside defined
bounds, ask the rider outside them. **That is withdrawn.** The rider is on a
moving scooter. A question they cannot safely read is not a safer default than
an action they can undo at the next light.

So, whenever the plan is disrupted:

1. **Resolve it automatically.** Re-solve from where the rider is now, claim
   what the new plan needs, release what it does not.
2. **Tell them, once.** What changed, and what the plan is now.
3. **Let them overrule it**, with the alternatives already on hand — the
   `backups` §6.4 returned. One tap to see them, one to take one.

No envelope, no branch, no "was this change big enough to ask about". Always
act, always say, always reversible.

### 7.2 What counts as a disruption

The plan is **live**, and "somebody took your scooter" is no longer a special
case — it is one entry in a list:

- the vehicle you are heading for is taken, disabled, or vanishes from the feed;
- a materially better plan appears (this is revision 2's "upgrade", and it is
  no longer a separate optional Phase 3b — it is the same code path).
  **"Materially" is 120 seconds of generalised cost, and a re-solve for this
  trigger fires at most once every 3 minutes.** Without a number this trigger
  re-solves on estimate noise: the search reruns on every refresh, and a
  one-second improvement is a disruption by this list's own wording. 120 s is
  chosen **above `bonus_favorite`'s 90 s on purpose** — a favourite coming into
  range must not by itself rearrange a trip in progress — and it is 15¢ at
  §6.3.0's exchange rate, so it clears rounding without needing a real saving
  to be large. Ties and sub-threshold gains change **nothing**: no re-solve, no
  claim movement, no notification. **The other four triggers have no threshold
  and no interval** — a vehicle that is gone is gone;
- your battery is draining faster than the estimate and the current leg will
  not reach its hand-off;
- you fall far enough behind the plan's timings that its dibs will expire;
- you take a different turn and the remaining legs no longer fit.

Each re-solves the **remaining** route, not just the next vehicle. Re-solving
only the next vehicle is what leaves a rider on a route that no longer makes
sense — the failure mode that made "a plan plus a rescue" the wrong shape.

### 7.3 Dibs, and why riding changes it

Dibs goes on the **next** vehicle in the plan, claimed while the rider is
riding toward it.

**It does not hold the vehicle, and this document must never imply it does.**
`src/api_dibs.py` is explicit: *"It does not reserve anything: Veo has no
reservation system, this app cannot stop a vehicle unlocking, and no claim
here changes what the operator will rent to whom."* An earlier draft of this
section said the pickup "is still there when you arrive because you claimed
it" — which is precisely the reservation promise §7.4 forbids, and it was
wrong on the facts as well as the vocabulary.

What a claim actually buys a hand-off is two things, both real and neither a
guarantee:

1. **Recorded intent**, so the app greys out "I'll ride this one" for anybody
   else looking at the same vehicle in this app (`GET /api/v1/dibs/vehicle/{id}`).
2. **A watched vehicle**, so the moment it goes the plan re-solves (§7.1–7.2)
   rather than the rider discovering it on arrival.

The honest sentence is therefore: *the pickup is monitored, and if somebody
takes it you will be moved before you get there* — not *it will be waiting*.

Two consequences the existing dibs rules do not cover:

- **`DIBS_MAX_WALK_MINUTES = 15` is the wrong constraint for a ridden
  approach.** It exists to stop somebody claiming a vehicle they cannot reach
  in time. Riding reaches perhaps four times as far inside the same 25-minute
  window (`sql/076`), so the constraint must become a **time-to-arrival** one
  computed from the actual leg, not a walk-minutes constant.
- **A plan holds at most one claim at a time**, exactly as revision 2's swap
  rule required — but **the server keeps that invariant, not the client.** A
  chained plan does not get to hold three scooters hostage because it intends
  to visit them.

  **Not "release, then claim".** That was revision 2's wording and it is the
  *non-atomic* path: two calls, a window holding no claim at all, and a failed
  second call that permanently loses the first — for nothing, since the rider
  is still riding toward a pickup they no longer hold. A re-solve therefore
  moves the claim in **one** `POST /api/v1/dibs` carrying `replaces`, with the
  old row expired and the new one written in a single transaction. That is
  what makes "at most one" an invariant rather than a convention the client is
  trusted to honour, and it is the reason `replaces` exists (§7.4's migration).

  **And the fallback this section used to permit could not have worked.** It
  said two calls were acceptable until `replaces` ships, provided a failed
  claim was treated as "a re-solve that did not happen, keeping the old claim"
  — but the first call has already released that claim, so there is nothing to
  keep; and claim-before-release is refused by the server invariant three
  paragraphs up. The licence was self-contradictory, so it is withdrawn: a
  **claim-moving re-solve is a hard dependency on `replaces`** (§7.4's
  migration), and the sequencing table says so.

  **What ships before it**, since Phase 3 is not blocked wholesale: the
  re-solve happens — route, plan, notification — and the claim does not follow
  it. The old claim is released, the new pickup is **unclaimed**, and the
  surface says so. The cost is the recorded intent alone; the **watcher reads
  the stored plan, not the claim** (§13.4.1), so monitoring is unaffected. A
  surface implying the claim moved when it did not is the one unacceptable
  outcome.

### 7.4 What this must never claim

Unchanged and still binding: a claim is **dibs**, not a reservation and not a
hold. A hand-off plan makes that vocabulary more tempting to break, not less,
because the plan *sounds* like a booking. It is not one.

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

### 8.3 `sql/081_favorite_devices.sql`

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

- **Plan ranking (§6.3).** `bonus_favorite` — a modest one. Starting
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

**REVISION 3 MERGED THE STOPOVER INTO THE HAND-OFF.** Revisions 1–2 designed
5b as a second mechanism — its own search, its own card, its own "restart
faff" cost. Once a trip can hand off (§6), **an Equity Area stopover simply IS
a hand-off whose pickup was chosen for the discount**: same legs, same second
unlock, same re-rent exposure. So there is no separate stopover search. The
arithmetic below becomes a **term in the generalised cost** — an Equity Area
changes the per-minute rate of any leg starting or ending inside one — and the
plan search finds equity hand-offs for free, ranked against every other plan.

One caveat in §9.2 stops being a caveat as a result. "Show whether another
vehicle meeting the spec is standing in that Equity Area before advising the
split" was listed as the honest mitigation for the two-rental risk; under the
merged model it is automatic, because a plan is built out of vehicles that
actually exist.

Break-even, with `t` the riding minutes and `d` the minutes added by the
detour and the restart faff:

```
saving = (base_per_min − 13¢) × t  −  13¢ × d  −  $1.00 (second unlock)
```

- Resident (25¢): worth it past **~8.3 riding minutes**, at zero detour.
- Visitor (39¢): worth it past **~3.9 minutes**.
- **Access tier: never offered.** See §3.

And the cheapest case is free: **if the direct route already crosses an Equity
Area, `d = 0`** and the only cost is the second unlock.

**Two different questions, and revision 3b's deletion of the stopover search
makes it worth separating them** — the paragraph that used to stand here
answered both as one, which is no longer implementable now that the rate is a
term in the Phase 2 planner rather than its own search:

| Question | Answered by | Whose job |
|---|---|---|
| *Does this leg bill at the Equity Area rate?* | the **leg's own endpoints** against the polygons — `isInEquityArea(from) ‖ isInEquityArea(to)` | the money term, §6.3 |
| *Where might a worthwhile equity pickup be?* | the direct route's geometry against the polygons, which is nearly free (`equity-areas.ts`'s `isInEquityArea`, already how the on-screen indicator works) | candidate selection, §6.2 |

The first is the rule Exhibit A §5.2 actually states — a trip that **starts or
ends** inside a polygon — so a leg merely *passing through* one earns no
discount, and pricing it as though it did advertises money the rider will not
get. §12.5 draws exactly this line for receipts, and the planner has no licence
to be looser than the receipt checker. The second is a hint for choosing
candidates, and it is all that survives of "two tiers of search": there is no
second routing call to test a detour, because there is no separate stopover
search left to make one.

### 9.2 Four things this must be honest about

1. **We cannot promise the discount.** The app's own
   `EQUITY_DISCOUNT_NOTICE` already tells riders to screenshot the receipt if
   they do not see it. A feature that advises a *behaviour change* on the
   strength of that discount inherits the caveat and must state it at the
   point of advice, not in a drawer: **"this should cost $X. If Veo bills you
   the base rate, screenshot it."**
2. **Two rentals is a real risk, not just a fee.** Between ending leg one and
   starting leg two, somebody can take the scooter. Dibs does not prevent that
   — nothing does (§7.3). **The plan's own details must say so**, not a
   stopover card: there is no such card any more (§9's opening), and the
   disclosure travels with every equity plan instead.

   The mitigation is now structural rather than advisory: a hand-off plan is
   built out of a pickup that **exists**, so "is there another vehicle meeting
   the spec standing in that Equity Area" is answered by the plan existing at
   all. Revision 2 had to ask the question separately because its stopover was
   a location rather than a vehicle.
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

`src/equity_savings.py` + a `savings` block **on each plan** (§6.4), rather
than a new endpoint: the question "what will this cost" is asked about a plan,
and answering it anywhere else means the answer can disagree with the plan it
is about.

**There is no `stopover` field, because there is no stopover.** Revision 3
deleted the separate split (§9's opening): an equity hand-off is an ordinary
plan whose pickup happens to sit inside a polygon, so the saving is a property
of *that plan* and the hand-off is already in its `legs`. A dedicated field
would be a second representation of something the plan already describes.

```jsonc
"savings": {
  "plan": "resident",
  "baseline_cents": 475,        // the cheapest plan with no equity-priced leg
  "cents": 295,
  "saves_cents": 180,
  "adds_seconds": 130,          // versus that baseline
  "from": "start_in_equity_area",   // or "hand_off_in_equity_area" | "none"
  "equity_legs": [1],           // indices into this plan's own `legs`
  "caveats": ["discount_not_guaranteed"]
}
```

`equity_legs` is how a card says *which* leg is discounted without inventing a
parallel geometry: it points into the legs the plan already carries.

The polygons are already server-side (`data/equity.geojson`, boundary layer
`equity`, `src/equity_groups.py`'s `OFFICIAL_GROUP`) and client-side (bundled
`public/equity-areas.geojson`, geometry-identical by test). Neither side needs
new geometry — which is the whole reason this phase is small.

---

## 10. Phase 6 — One app, one mode

Frontend-only. No endpoint, no migration, no stored field. This phase is
about **friction that has no feature behind it**: the places where the app
still asks the rider to be in a mode.

### 10.1 What is already done, so this phase does not redo it

Most of the mode teardown has happened, and `wireModes()` in `main.ts` is
unusually honest about it. **ONE MAP:** finding a ride no longer wipes the
rider's filters, forces `hideUnavailable`, clears the choropleth, hides
drawer tabs or fetches a lean payload — which also killed the "merely
visiting Find wheels destroyed my analysis setup" bug and the entire
snapshot/restore dance it needed. **NO ANALYSIS MODE:** the third mode is
gone, because there were never three things to be in; Equity Compliance is
now a named button in the Tools drawer rather than a side effect. The bottom
of the screen asks "where are you going?" (`home-bar.ts`) instead of asking
the rider to classify themselves, and the destination they type is handed
to the wizard through `pending-trip.ts` so Screen 3 opens pre-filled rather
than asking twice.

**So the remaining friction is not "the app has modes". It is that the
scaffolding of the old modes is still standing, and it still costs.** Four
seams, in the order they bite.

### 10.2 Seam 1 — the hidden mode bar

`index.html` still carries `#mode-switch` with two `hidden` buttons, and the
home bar enters a ride by **synthetically clicking one of them**. The comment
in the markup is candid about why — every mode preset was wired to those
buttons by `wireModes()`, and clicking them was how the entry point moved
without re-deriving the behaviour.

That was the right call for the move. It is the wrong thing to leave:

- Entering the app's main flow runs through an element the rider cannot see,
  cannot reach, and which exists only to be clicked by code.
- It keeps "mode" alive as a concept in the source long after it died in the
  UI, so every new contributor learns it before learning it is gone.
- It is already load-bearing in places that have nothing to do with modes —
  `wireFreshnessCollapse()` had to be told to lift `#home-bar` rather than
  `#mode-switch`, and `install-prompt.ts` carries the same note. Two files
  already know about the seam. A third will get it wrong.

**The work:** move what `wireModes()` does for `data-mode="ride"` and
`data-mode="riding"` into two named functions the home bar calls directly,
delete the `#mode-switch` element, and delete the `setActive`/`aria-pressed`
bookkeeping that has had nothing to display since the bar went `hidden`.

Two known knots, neither of which this phase may paper over:

- `resetIconography` and `setSelect` are kept alive by bare `void` statements
  because deleting them makes whole drawer branches unreachable to the
  compiler. `wireModes()` says so in a comment. That is a real pre-existing
  knot; untangling it is **its own change**, and this phase must either do it
  properly or leave the `void`s exactly where they are with the comment
  intact. Quietly deleting them to make a diff look tidier is how drawer
  state loses its only writers.
- The HUD's exit path hands the bar back to whichever mode was active before.
  With no bar, "before" has to become an explicit piece of state rather than
  an implicit one, or closing the HUD lands the rider nowhere.

### 10.3 Seam 2 — two model filters that disagree about the empty set

`devices.ts` carries `rideModelFilter` (the HUD's "Show" pills) **alongside**
the Filters drawer's own `models`. They are different fields with different
semantics, and the difference is the dangerous kind:

| | `null` means | empty set means |
|---|---|---|
| Filters drawer `models` | show every model | show every model |
| `rideModelFilter` | no ride filter at all | **show none** |

Both are documented, both are correct in isolation, and one map applies both.
A rider who deselects every pill while riding sees an empty map; a rider who
deselects every model in the drawer sees the whole fleet. Same gesture,
opposite outcome, and nothing in the UI distinguishes them.

**The work:** one model-filter concept over the map, with one meaning for the
empty set, and the HUD pills expressed in terms of it. If the ride surface
genuinely needs "show none" — and it may, since the pills are a live HUD
control rather than a search — then it needs to be a *named* state ("hide all
scooters") rather than an empty selection that means the opposite of what the
same empty selection means one drawer away.

### 10.4 Seam 3 — the spec does not follow the rider into the ride

Phase 1 lets a rider say, once and on their account, what they are willing to
ride. The ride HUD does not know it. Its "Show" pills are set from
`rideModelFilterFor()` and the ride's own options, with no reference to the
attached spec (§5.5). So a rider who has said "only Cosmos, must have a
basket" gets a HUD showing everything, and has to say it again in pills.

This is the same mistake `ride-preflight.ts` was built to fix one screen
earlier — its header names re-asking a question the rider already answered as
"the single loudest piece of friction left in the flow" — and the same fix
applies: read the answer that already exists.

**The work:** when a spec is attached, the ride surface opens honouring it,
and says which spec it is honouring. The rider can still change the pills —
that detaches, exactly as §5.5's attach/detach rule already specifies for the
map — but they start from what they already told us.

### 10.5 Seam 4 — one vocabulary, two flows

There are two ways into a ride, and they ask different questions:

- **The wizard** (`ride-modal.ts`, Screens 1–6): who you are, where you are,
  which scooter, then a linear flow. Right when the rider opens 🧭 with
  nothing in mind.
- **The pre-flight** (`ride-preflight.ts`): two toggles and sometimes one
  either/or, then straight in. Right when the rider is already standing at a
  scooter with its popup open.

Both are correct, and this phase does **not** merge them — the two situations
really are different, and collapsing them would recreate the friction the
pre-flight exists to remove. What it fixes is that they must not drift into
two vocabularies for the same settings. `ride-preflight.ts` already holds the
line ("this module does not invent a parallel settings vocabulary"), and
`track-preference.ts` is the precedent for the other direction: a question
that was asked every ride turned out to have the same answer every time, so
it left the survey and became one standing setting in Settings → Local Data.

**The work:** audit every question either flow asks against that test — *is
the answer per-ride, or is it standing?* — and move the standing ones out.
Two are already named as suspects by the code that owns them. The naming
collision `ride-settings.ts` documents is in scope too: `RideOptions.theme`
is the **route-preview basemap flavour**, not the app's theme, which is why
that panel deliberately has no Theme row. Rename the field rather than keep
explaining it.

### 10.6 What this phase must not do

- **No new stored field.** Nothing here is a retention question, and it must
  not become one.
- **No re-litigating ONE MAP.** Entering a ride flow still changes nothing
  about the map the rider set up. Every seam above is closed by *deleting*
  mode machinery, never by adding a preset back.
- **No default on the wheels toggle.** `home-bar.ts` states why neither
  option is preselected, and "reducing friction" is exactly the argument that
  would undo it. An unanswered question is honest; a wrong default is not.

---

## 11. Phase 7 — The walkthrough, restored

The seven-screen intro tour (`onboarding.ts`) still exists, is still
replayable from the About drawer, and **does not auto-show**:
`ONBOARDING_AUTOSHOW = false` in `main.ts`, with a comment saying it is off
"while the tour is rewritten". This phase is that rewrite.

### 11.1 Why it is last

The tour's job is to describe the app. An app that is still moving cannot be
described — and a tour that confidently describes the wrong app is worse than
no tour, because it is the first thing a new rider sees and it teaches them
things they then have to unlearn. That is the reasoning already written at
the call site, and it is why this phase sits behind Phase 6 rather than in
front of it.

### 11.2 What is actually broken

Two different kinds of wrong, and they need different fixes:

**The CTA is broken, mechanically.** `onStartExploring` finishes the tour by
clicking `#mode-switch .mode-btn[data-mode="ride"]` — an element that is
`hidden` today and, after Phase 6, will not exist. The tour's final promise,
"start exploring", is a click into the seam Phase 6 deletes. It also switches
the legend on and fires the one-time "tap any scooter" nudge, both of which
are still fine.

**Two of the seven screens describe a UI that moved.** Screen `ride-mode`
sells "Ride Mode" as a place you go, which is the mode vocabulary the app has
spent several PRs removing; screen `models` promises "save your favorite
combos and reuse them in one tap", which is presets — true, but now sitting
next to specs, and the tour is where a rider would form their idea of the
difference. The other five (`welcome`, `features`, `rideability`, `routing`,
`contribute`, `territory`) describe things that still exist and still work.

### 11.3 The rule

**The tour describes the app; the app does not chase the tour.** If a screen
is wrong, the screen changes. The one thing this phase may never do is add or
keep a surface in the app because the walkthrough mentions it. `ONBOARDING_SCREENS`
is exported precisely so a "what does the tour promise" audit can read the
copy without opening the overlay — this phase is the first such audit, and it
should leave a test behind that fails when a screen names a control that no
longer exists.

### 11.4 What ships

- The CTA lands on the home bar's "where are you going?" question rather than
  clicking a deleted element.
- The `ride-mode` screen is rewritten around what the rider actually gets —
  a landscape dashboard while riding — with no claim that it is a mode they
  switch into.
- The `models` screen distinguishes a saved *view* from a saved *spec* in one
  sentence, using §2's vocabulary, and points at the one-tap bridge.
- Whatever Phase 1 and Phase 4 put in front of riders earns a screen or a
  sentence: a rider who never learns that "my ideal scooter" or "My Scooters"
  exist has them only by accident.
- `ONBOARDING_AUTOSHOW` goes back to `true`. That is the deliverable; the
  rewrite is what earns it. Turning it on was always one line — the reason it
  is one line is so this phase is a decision, not a revert.
- Still replayable from About, still once per browser, still skippable on
  every screen.

---

## 12. Phase 8 — The receipt

### 12.1 Why this is the most "this app" feature in the program

Exhibit A §5.2 obliges Veo to discount **any trip that starts or ends within a
designated Equity Area**. Not on request, not on enrolment — "shall". Exhibit
C prices it at $1 + 13¢/min. `config.ts` already says what the gap is:

> *"the rider does not opt in, does not enroll, and does not have to know it
> exists… Putting it in the picker would frame an automatic entitlement as an
> option you have to know to select — which is the failure mode this whole app
> exists to correct."*

And the app already tells riders to screenshot the receipt if they do not see
the discount (`EQUITY_DISCOUNT_NOTICE`) — then does nothing with the
screenshot. This phase closes that loop, and it does two jobs at once:

1. **For one rider:** was this trip charged correctly, and if not, here is the
   complaint, ready to send.
2. **For everybody:** *is Veo actually applying the discount?* Thousands of
   individual "was I charged right" answers aggregate into the one question
   nobody can currently answer, and the answer is a far stronger lever with
   DOTI than any single complaint.

### 12.2 The architecture decision that makes this safe

**The image never leaves the device.** OCR runs on-device, the rider confirms
what was read, and **only the confirmed fields upload**. The evidence pile
needs numbers, not photographs.

That is the whole privacy argument, and it should be stated to riders in those
words. "We store photos of your account and your receipts" and "we store
figures you checked yourself" are different products, and only the second one
is worth building.

If on-device OCR cannot be made accurate enough to be worth shipping, the
fallback is **not** "upload the image to the API" — it is **manual entry**,
which §12.3 requires anyway.

### 12.3 Account confirmation, and the least data that does the job

A complaint needs to credibly assert *"I am this account"*, and the evidence
pile needs a receipt to be attributable enough to be worth counting.

Two routes, equally supported — **manual entry is never the degraded path**:

- a screenshot of the Veo app's profile section, read on-device;
- typing the account identifier in.

**We keep the account identifier and nothing else.** A profile screen carries
a name, an email, a phone number and possibly payment details; none of those
are needed to ask whether a trip was priced per Exhibit C. The extractor
takes the identifier, the UI shows the rider exactly what was taken, and the
rest is discarded with the image. This is the `favorite_devices` precedent
from §8 — *"the cheapest privacy decision available is not to have the data"* —
applied to a much more sensitive screen.

**Never store payment details, ever, even when the rider hands them to us.** A
screenshot containing a card fragment is discarded like the rest of the image;
the extractor must not have a field for it to land in.

### 12.4 The pipeline, and the limit nobody should discover late

```
screenshot (or typing) → on-device extract → RIDER CONFIRMS → verdict → [complaint] [pile]
```

Fields: trip start, end, duration, amount charged, the rate lines (unlock and
per-minute), and start/end location **when the receipt shows them**.

**The limit:** many receipts show time and money but no geography. The Equity
Area question is geographic, so without locations we cannot answer it from the
receipt alone. Two honest resolutions, in order:

1. **Match the receipt to the rider's own tracked ride by time.** If they
   tracked it in the app, we have the geometry already and the check is exact.
2. **If there is no tracked ride, say so.** We can still check the arithmetic
   (does unlock + per-min × minutes equal the total?) but not the entitlement.
   "We cannot tell from this receipt" is a frequent and correct answer, and a
   feature that guesses instead is one that sends riders to lose arguments.

### 12.5 The bar for saying somebody was overcharged

All three, or we do not make the claim:

- the trip **demonstrably** starts or ends inside an Equity Area polygon
  (`equity-areas.ts`'s bundled geometry, already how the on-screen indicator
  works);
- the confirmed charge **demonstrably EXCEEDS** the applicable expected
  charge — not merely differs from it;
- the rider has **confirmed** the extracted figures.

**"Differs" was the wrong test and it would have produced complaints about
trips charged too little.** A lower promotional rate, a credit, or a free
Access trip all differ from `$1 + 13¢/min` while leaving the rider better
off; a tool that writes to support about those is worse than useless to the
people it is for.

So the comparison is one-sided, and it has to absorb the ways a correct charge
legitimately fails to equal the arithmetic:

- **billable-minute rounding** — `billableMinutes` is `ceil`, with a floor of
  1 (`ride-cost.ts`), so compare against the rounded figure, never raw minutes;
- **tax**, which `estimateWithTax` applies on top of `unlock + perMin`;
- **credits, promotions and discounts**, which can only ever reduce the charge
  and therefore never evidence an overcharge;
- **the unresolved Access and Pass interactions**, which stay *"we cannot
  tell"* per §9.2.3 rather than becoming a claim either way.

A margin below which we say nothing is part of this: a few cents of rounding
disagreement is not a finding, and treating it as one spends the credibility
the evidence pile depends on.

**The margin is 10¢, and it has a real ceiling rather than being a matter of
taste.** One minute of the Equity Area discount is **12¢** (25¢ base against
13¢), so a margin at or above that makes the **shortest trips unprovable** — a
one-minute ride billed at the base rate would fall inside the margin and be
reported as "we cannot tell", which is the opposite of what this phase is for.
Below, the only thing to clear is tax rounding, which is a cent or two. So 10¢
sits between the two bounds, and the upper bound is the part to preserve if the
figure is ever revisited. Test at 9¢, 10¢ and 11¢ **with their verdicts
stated** — `cannot_tell`, `cannot_tell`, `overcharged` — since naming the three
figures without their expected answers still lets a test choose `>` or `≥` and
pass either way.

Below that bar the verdict is *"we cannot tell"*, and the UI says why.

**Two cases below the bar read alike and must not be collapsed**, or `correct`
becomes unreachable and the evidence pile can never record the discount *being
applied* — which is the only question it exists to answer:

| The charge | Verdict |
|---|---|
| explained **entirely** by billable-minute rounding and tax — i.e. it **equals** the expected figure, properly computed | **`correct`** |
| **exceeds** the expected figure by **10¢ or less** — the margin itself included | **`cannot_tell`** |
| **exceeds** it by **more than 10¢** | **`overcharged`**, the bar above being met |

One is arithmetic we can account for; the other is a gap we cannot explain and
will not accuse anybody over. A false
accusation is worse than silence here: it costs a rider their time and their
credibility, and it costs this project the only thing that makes the evidence
pile worth anything.

**VeoPlus stays unmodelled**, per §9.2.3: whether the Pass waives the Equity
Area's $1 unlock is not stated in Exhibit C. A receipt that differs only by
that unlock is *"we cannot tell"*, not an overcharge.

### 12.6 The complaint

**The rider sends it, from their own address.** The app never sends it.

That is not a limitation to route around, it is the design: sending it would
mean this project making a contractual assertion on somebody's behalf, about a
contract it is not party to, from an address they do not control. The rider
pressing send is the correct ceiling.

**The mechanism is a `mailto:`, not a clipboard copy, and §14.1's CC is why:
text in a body cannot set a recipient.** Copying a body leaves the rider to
type the addresses, which is precisely where an opted-in CC silently fails to
happen. So the action opens their own mail client with `to`, `cc`, `subject`
and `body` populated — the rider reviews and sends, and the CC is a real
header rather than a line of prose.

`mailto:` has a length limit that varies by client — and **"when the body
would overflow" is not a condition anything can evaluate.** No client exposes
such a signal, a `mailto:` that is too long **opens a silently truncated
draft** instead of failing, and the limit applies to the **fully
percent-encoded URI** rather than to the body, which encoding can more than
double. Measuring the body measures the wrong string, and the failure it misses
is a complaint that looks sent with its figures gone.

So: build the whole URI, **measure its encoded length**, and take the clipboard
route above **1,800 characters** — conservative, below the smallest limit in
common circulation rather than tuned to one client. **The clipboard route is
also always available as its own control**, so no rider depends on our estimate
of a limit we cannot query. Either way it shows `To:` and any `Cc:` **as their
own copyable fields**: a fallback that folds the CC into body text is the bug
this paragraph exists to prevent.

**Neither path is reachable until the rider has confirmed the figures**
(§12.3). Gating one and not the other would leave an unconfirmed complaint
sendable by the route that happens to be primary, which is the whole failure
the confirm step exists to stop.

The body states the **account identifier** (§12.3's only purpose — without it
the complaint cannot credibly say whose trip this was), the trip, the charge,
the expected charge under Exhibit C, and cites Exhibit A §5.2 — facts and a
contract reference, no adjectives. It is a billing query, not an accusation,
because at the single-receipt level an overcharge is indistinguishable from a
bug.

**Recipient:** `support@veoride.zendesk.com`. It is a Zendesk queue, which is
worth knowing for two reasons: a ticket gets a reference number the rider can
quote later, and a queue tends to answer templates better than prose — so the
body leads with the trip, the charge and the expected charge, and puts the
contract citation under them rather than opening with it.

Store the address in **one place** (`config.ts`'s contact block, beside the
rate plans it will be quoted next to), never inlined in the template string.
Support addresses move, and a copy button that silently yields a dead
recipient is worse than one that is missing.

### 12.7 The evidence pile

New stored data, so it comes with the full house treatment (§15).

- **Consent is explicit, per submission, and revocable.** A rider checking
  their own receipt contributes nothing by default. Contributing is a separate
  deliberate act, and withdrawing must actually remove the row and recompute
  the aggregate — a consent you cannot withdraw is not one.
- **The pile stores what the question needs and no more.** "How often is the
  discount applied?" needs the date, the area, the charged rate and the
  expected rate. It does not need the account identifier, the exact
  coordinates, or the time of day to the minute.
- **The account link is kept only for as long as the rider needs it** to see
  and withdraw their own submissions; the aggregate reads de-identified rows.
- **New migration**, and therefore the three-address rule in full: `src/cli.py`
  (retention sweep and de-identification), `src/api_meta.py:_PRIVACY`, and
  `privacy_policy.html`, in the same PR.

**What the aggregate may claim.** "The Equity Area rate was not applied on N of
M qualifying trips riders submitted" — a factual statement with its own
denominator, and a self-selected sample that must be described as one. Never
"Veo is stealing from poor people", however tempting the number. The strength
of this evidence is entirely that it is boring and checkable.

### 12.8 What this must never do

- Never send the email.
- Never upload the image.
- Never store payment details, even when they are handed to us.
- Never accuse on an unconfirmed read.
- Never publish a rider-identifiable receipt.
- Never present a self-selected sample as a census.

---

## 13. Phase 9 — Reaching the rider

**Formerly "Pocket-proof", and now committed.** The name was right about the
problem and coy about the mechanism: the mechanism is SMS.

### 13.1 Most of this already exists, which is why it is now affordable

| Piece | Status |
|---|---|
| Outbound SMS with **consent (STOP), brand prefix, quota, fallback** | `src/comms.py`, shipped |
| Inbound replies, claim-on-poll + ack | `comms.py:poll_replies` / `ack_reply`, shipped |
| **Verified** phone numbers on the profile | `POST /api/v1/profile/phone/{code,verify}` → `phone_verified`, shipped |
| "A profile carries an email, a phone, or both" | `accounts_email_or_phone_required`, `sql/025` |
| Deep-link entry with a reauth path | the `?ml=` magic-link flow, `main.ts:633` |
| Watching a claimed vehicle, with reasons | `device-watch.ts`, shipped |

So "encourage adding a phone number" is **a prompt, not a build**. What is
genuinely new is consent, the rapid check, and the resume link.

### 13.2 A number given for sign-in is not consent to be texted about scooters

`comms.py` honours STOP across every application on the shared number, which
is the legal floor and not the whole duty. A rider who typed their number to
receive a **sign-in code** has not agreed to receive **trip alerts**, and
treating those as one consent is how an app becomes something people block.

So: a separate, explicit, revocable opt-in for trip alerts, stored
per-account, asked **at the moment it is useful** — when a rider starts a
hand-off plan — and never as a wall in front of the feature. Declining is a
first-class answer that costs the rider nothing but the texts.

The opt-in copy states what will be sent, roughly how often, that message and
data rates apply, and how to stop. Standard practice, and cheap.

**A plan never requires SMS.** The whole feature works with it off; SMS is the
channel that survives the screen going dark, not a dependency.

### 13.3 What earns a text, and what does not

Risk 12 is the one to respect here: `dibs-notify.ts` caps itself at four
alerts per claim *on purpose*, and a re-solve that buzzes for every trigger in
§7.2 spends the budget that protects the message that matters.

**SMS is strictly narrower than the in-app notice.** It is for what you would
want to be interrupted for:

| Event | In-app | SMS |
|---|---|---|
| Your pickup is gone; you have been moved | yes | **yes** |
| The plan changed where you are going | yes | **yes** |
| A better option appeared and we took it | yes | no |
| Re-solved, nothing you would act on changed | no | no |
| Plan complete | yes | no |

**Hard ceiling of three texts per trip**, on top of `dibs-notify.ts`'s four
alerts per claim. Three because the two SMS-worthy events above are both
"where you are going has changed", and a trip that produces a fourth is a trip
going wrong in a way a fourth text does not fix — at that point the app is the
place to look, and a phone that has buzzed four times is a phone whose next
alert gets ignored. Without a stated number the "ceiling holds" test can pick
its own and pass, which is the safeguard defeating itself. Test the boundary:
the third sends, the fourth does not, and the suppression is visible in-app.

A text that says "we checked and it is fine" is not reassurance, it is
attrition.

### 13.4 The rapid check — narrow, not fast

The ask was "fire calls up to every 20s instead of 2m". The measurement says
do something better and cheaper, because **the bottleneck is not the polling
rate**:

- **Upstream is always current.** `crontab` records it: *"the upstream feed is
  generated per-request (`last_updated` stamps at fetch time, `ttl=0`), so any
  cadence returns current fleet state."*
- **Our ingest is every 2 minutes**, and that is where the staleness lives.
- **The client already polls every 90 seconds** (`REFRESH_MS = 90_000`) —
  i.e. *already faster than the data behind it moves.*

So a 20-second global poll would re-read the same two-minute-old cycle six
times: **six times the load, zero extra freshness.** It is not a small
inefficiency, it is the entire cost with none of the benefit.

**What actually delivers 20-second news:** an active plan depends on a handful
of vehicles — the pickup and its backups, one to five of them — so what we
*read, store and compare* is those. Upstream being per-request means a fetch
returns genuinely current state.

**A retraction, because an earlier draft claimed a saving it does not have.**
It said this was "three orders of magnitude cheaper" than a global poll. That
is wrong, and `src/ingest.py`'s `fetch_gbfs()` is why: GBFS `free_bike_status`
is a **whole-feed** endpoint. There is no per-vehicle query, so checking five
vehicles still downloads the entire fleet payload and filters it afterwards.
Narrowing saves our own storage and comparison work — not the upstream fetch.

**The saving that is real is coalescing, and it is about scaling, not size:**

| | Upstream fetches |
|---|---|
| Ingest cycle, today | 1 per 2 min |
| **One shared 20-second check**, serving every active plan | 1 per 20 s — **6× the ingest, and O(1) in riders** |
| Per-rider independent checks (the naive reading) | 1 per 20 s **per rider** — O(riders), and unacceptable |

So the design is **one coalesced fetch on a 20-second tick, shared across all
live plans**, with each plan reading its own 1–5 vehicles out of the result.
Six times the upstream requests is a real and statable cost; multiplying full
fleet fetches by the number of riding riders is not.

Alternatively, **if a vehicle-scoped upstream API can be verified to exist**,
use it and the per-vehicle saving becomes real. Treat that as a prerequisite
to check, not an assumption — exactly as with the Valhalla matrix.

**Do not change the global ingest cadence to achieve this.** The crontab is
explicit that the live schedule is an admin-editable copy
(`/admin/scheduler/edit`), and that cadence is shared with the compliance
audit's trip-duration resolution. This feature does not get to retune the
audit.

#### 13.4.1 Who watches while the tab is closed

**Foreground-only checking and closed-tab SMS cannot both be the whole
story**, and an earlier draft asserted both: the client check stopped on
backgrounding, while §13.6 promised alerts with the app closed. Nothing would
have been detecting the loss to text about.

Two distinct watchers, and the plan should name both:

| | Runs where | While |
|---|---|---|
| **Client check** | the browser | a plan is live **and the document is foregrounded**; stops on `visibilitychange`, completion, abandonment |
| **Server watcher** | the API, over the stored plan (§13.6) | the plan is live, **regardless of the tab** — bounded by the plan's own lifetime and a hard ceiling |

The client one keeps an open app instantly fresh. The **server** one is what
makes an SMS possible at all, and it is the one Phase 9's acceptance criterion
("phone in a pocket") actually rests on. The `ride_watch` job is the existing
pattern.

Rate-limit per **account**, not just per IP — an account is the thing that has
one trip — and the test that matters is **pickup loss detected while the app is
backgrounded or closed**, which is the case the client check cannot cover.

### 13.5 The resume link: a reference, never a credential

Every alert carries a link that puts the rider back where they were —
including, as asked, after re-authenticating.

**The link carries a plan reference. It does not carry a session.** An SMS
renders on a lock screen, survives in carrier logs, gets screenshotted and
lands on shared handsets; a URL that *is* a credential in that channel is a
credential you have published. So:

- session alive → the plan resumes;
- session gone → normal sign-in, **then** the plan resumes.

That is exactly the behaviour asked for, and it is also the safe one — a
pleasant coincidence worth writing down so nobody later "simplifies" it into a
token.

The `?ml=` flow is the precedent, including the question `main.ts:636` already
asks about whether a deep link belongs to the person holding it.

### 13.6 The server-side plan, and the retention conversation it opens

For an alert to fire with the tab closed, the plan must live somewhere that is
not the tab. That means **storing a live destination and a rider's progress
toward it** — the thing §15 warns is *"a much bigger version of this
conversation"*.

It is now committed, so the conversation happens rather than being deferred:

- Store the **plan**, not a track: the legs, the current claim, the backups.
  Not a breadcrumb trail of where the rider has been.
- **Three operations, because storage with no end state is a leak.** Create on
  the plan the rider chose, update on each re-solve, finish on arrival or
  abandonment: `POST /api/v1/trip/plans`, `PATCH …/plans/{id}`, `DELETE
  …/plans/{id}`. The server cannot infer which of the offered backups was
  taken, and `fetchPlanCriticalState` reads vehicles rather than plans, so
  without `create` the watcher has nothing to watch and the resume link has
  nothing to resume. Without `finish` the watcher runs until the ceiling
  expires it, texting about a trip that ended.
- **Delete on completion or abandonment**, with a short hard ceiling
  regardless — a plan is worth minutes, not days, and `pending-trip.ts`
  already argues exactly this for the client-side intent.
- Three-address rule in full: `src/cli.py`, `api_meta.py:_PRIVACY`,
  `privacy_policy.html`.

### 13.7 Tests

- A rider with `phone_verified` but no trip-alert opt-in gets **no** SMS.
- STOP, then a re-solve: no send is attempted, and the 409 is not an error
  path the rider ever sees.
- The per-trip SMS ceiling holds across a plan that re-solves many times.
- A re-solve that changes nothing actionable sends nothing on either channel.
- The resume link with a dead session lands on sign-in and **then** the plan.
- The resume link carries no token that grants access on its own — asserted,
  not assumed.
- The targeted check covers only plan-critical vehicles, and stops on
  completion, abandonment and backgrounding.

---

## 14. Phase 10 — Advocacy

Phase 8 hands a rider a complaint. This is what happens when they want
somebody in their corner while they send it.

### 14.1 The CC, and what consenting to it actually discloses

The complaint email (§12.6) offers an opt-in CC to
**`advocacy@weseeyouveo.com`**.

**This is a disclosure to a third party, and a different one from the evidence
pile.** The pile takes de-identified figures; a CC'd email carries the rider's
own words, their account identifier, their trip times and their email address,
into a mailbox a person reads. Consenting to one is not consenting to the
other, and the UI must not imply it is.

So it is its own tick, defaulted **off**, with a plain statement of what the
CC sees. And it is per-complaint: a rider who wanted backup last week has not
volunteered for it forever.

**It must set a real `cc` recipient** (§12.6's `mailto:`). An address written
into body text is not a CC and would never reach the mailbox — a feature that
*appears* to loop in an advocate and does not is worse than no feature, since
the rider believes they have backup they do not have.

### 14.2 The portal

A review surface for the advocacy mailbox: complaints in, their verdicts,
their outcomes. Its value is the pattern — the same area, the same rate, the
same month — which is the evidence pile's question asked with names attached
for the cases where a rider explicitly asked for help.

Access is operator-only, the same posture as `/admin/*`.

### 14.3 The reply rule: invited, never uninvited

**No reply is sent into a case unless the case mentions `@WSYV` or
`@advocacy`.**

**This is now a guard to ADD, not a rule to design.** `POST
/api/admin/inbound-messages/:id/reply` already exists and already sends,
through the site-derived Postmark token, marking the thread replied. Today
nothing stops it answering an uninvited case. That single check is the most
important line of code in this phase, and it belongs **in the server**, on
that endpoint — not in the admin UI, where a determined click routes around
it.

This is the right rule and worth stating the reason, because it will be
tempting to relax it: an advocacy organisation that inserts itself into every
case becomes a nuisance and spends the standing it needs for the cases that
matter. One that appears only when invited is a resource. It also keeps the
rider's complaint **the rider's** — they asked Veo a question, and a third
party answering over their shoulder takes the case away from them.

The invitation can come from either side: a rider who asks for help, or a Veo
agent who brings them in. Both are invitations; neither is assumed.

**Most of this already exists, in `zNeill/keepdenverfair`.** Phase 10 was
scoped assuming an inbound-email pipeline had to be built. It does not:

| Piece | Where |
|---|---|
| Inbound webhook, secret-verified | `POST /api/webhooks/postmark-inbound`, `apps/server/src/server.ts` |
| Site routing from the recipient **domain** | `siteKeyForDomain(domainFromEmailAddress(recipient))` |
| Persisted inbox, deduped on `Message-ID` | `store.recordInboundMessage`, unique partial index — Postmark replays inbound under some failure modes |
| `In-Reply-To` captured | the same call — which is what threads a Zendesk case together |
| Forward to a human | `inbound-forwarder.ts` → `POSTMARK_INBOUND_FORWARD_TO` |
| Inbox API | `GET /api/admin/inbound-messages`, `GET /:id`, `POST /:id/reply` |
| Admin UI | `apps/admin/src/pages/communications.astro` |
| `weseeyouveo.com` credentials | `POSTMARK_WSYV_SERVER_TOKEN`, `POSTMARK_WSYV_INBOUND_FROM` |

**Routing keys off the domain, not the local part**, so
`advocacy@weseeyouveo.com` already arrives in that inbox with no
configuration change at all.

So the portal (§14.2) is `communications.astro` plus an invited/uninvited
filter, not a new surface. What genuinely remains is three things:

1. **Mention detection** — scan `textBody` / `strippedTextReply` for `@WSYV`
   or `@advocacy` and flag the message as invited. The flag belongs on the
   inbound row, beside `in_reply_to`.
2. **The operator alert** — that repo has no SMS at all. `comms.py` lives in
   `scooter-fyi-api` and is reached over the tailnet, so either server can
   call it; **decide which, rather than letting both grow a client.**
3. **The reply guard** — see §14.3, and read it before touching the code.

### 14.4 The alert

When a case mentions `@WSYV` or `@advocacy`, the operator gets a text, through
`comms.py` like everything else. Somebody asked for help; that is worth an
interruption in a way almost nothing else in this program is.

### 14.5 What this must never do

- Never CC without a per-complaint opt-in.
- Never reply into a case uninvited, whatever the pattern in the portal says.
- Never quote one rider's complaint to another, or in public, without
  separate and specific permission.
- Never present the portal's cases as a sample of anything — they are the
  complaints of people who asked for help, which is the most self-selected
  set in the whole program.
- Never let the advocacy relationship become a condition of the tool. The
  receipt checker works identically with the CC off.

---

## 15. House duties this program owes

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
  retention rule. Both Phase 3's migration (`release_reason`, `replaces_dibs_id`) and
  **`sql/081` in full** need `src/cli.py` (cleanup/de-id), `src/api_meta.py:
  _PRIVACY`, and `src/templates/legal/privacy_policy.html` updated
  **together**. `favorite_devices` is the more consequential of the two: it is
  a durable, account-linked record of *which specific vehicles a named person
  has physically stood at*, which is a stronger statement than anything else
  in the database. Deciding not to store the scan position (§3) is what keeps
  it from being stronger still. Phase 9, if it ever stores a live rider
  position, is a much bigger version of this conversation and should not be
  started casually.
- **Telemetry allowlist is mirrored by hand** in two repos —
  `denver-scooter-fyi/src/telemetry.ts`'s `TELEMETRY_EVENTS` and
  `src/api_telemetry.py`'s `ALLOWED_EVENTS`. **The authoritative list is the
  frontend lane's §Telemetry table**, which carries each event's props; this
  is the same names, and the two must not drift:

  `trip_plan_start`, `trip_candidates`, `trip_plan_chosen`, `trip_resolve`,
  `trip_exhausted`, `free_minutes_corrected`, `spec_applied_to_map`,
  `spec_saved_from_map`, `favorite_added`, `favorite_removed`,
  `favorite_available_alert`, `equity_savings_shown`, `equity_savings_taken`,
  `receipt_checked`, `receipt_verdict` (whose `reason` is an **enum** —
  `exceeds_bar` | `matches_expected` | `no_geography` | `inside_margin` |
  `veoplus_unmodelled` | `tier_unresolved` — since an unconstrained reason
  becomes free text from the verdict UI, and that is how an amount or an
  address reaches an allowlisted event), `receipt_complaint_prepared`,
  `receipt_contributed`, `trip_alert_opt_in`, `trip_alert_sent`,
  `resume_link_used`, `advocacy_cc_added`.

  **Three renames and one deletion came out of revision 3, and this list had
  drifted on all four** — which is precisely the failure the "mirrored by
  hand" rule exists to prevent, so it is worth naming them:

  | Was | Now | Why |
  |---|---|---|
  | `trip_swap` | `trip_resolve` | a swap became one trigger of a re-solve (§7.2) |
  | `trip_swap_offer` | **deleted** | there is no offer: every re-solve is applied and reversible (§7.1) |
  | `receipt_complaint_copied` | `receipt_complaint_prepared` | the primary path opens a draft and copies nothing (§12.6) |
  | — | `trip_plan_chosen`, `free_minutes_corrected` | new in revision 3, and never mirrored here until now |

  All of them must land in both repos in the same PR, and carry no free text — the existing contract is a fixed name plus enumerated props. **No
  `vehicle_identifier` in any of them**: that would attach a device to a
  session in the one system deliberately built to hold no persistent
  identifier.
- **Even-points invariant:** Phase 4 awards points only through the existing
  `credit_qr_scan_points` (100, even). Any new award must be even —
  `CHECK (points % 2 = 0)` on `user_points`, the assertion in
  `credit_points()`, and the sweeping unit test.
- **Tests:** fake-cursor unit tests by default; `*_pg.py` are integration tests
  gated on `VEO_TEST_PG_DSN`; one test file per module.
- **Phases 8, 9 and 10 each add a stored data category**, which is three more
  than the rest of the program combined. Phase 9's is the live trip plan (a
  destination and progress toward it — delete on completion, short hard
  ceiling regardless), Phase 10's is an advocacy mailbox holding riders' own
  words. Each needs `src/cli.py`, `api_meta.py:_PRIVACY` and
  `privacy_policy.html` in the same PR, and Phase 9 additionally needs a
  **consent record** for trip alerts that is separate from the phone number
  itself and independently revocable.
- **Phase 8 owes the most of any phase here.** It is the only one adding a new
  stored data category, and the most sensitive one in the program: the
  three-address rule in full (`src/cli.py` for the retention sweep and
  de-identification, `src/api_meta.py:_PRIVACY`, `privacy_policy.html`),
  plus a consent record that withdrawal actually honours. Its telemetry must
  carry **no receipt contents and no amounts** — a verdict enum and a boolean
  are the whole budget.
- **Frontend-only phases still owe the docs.** Phases 6 and 7 add no endpoint
  and no field, so most of the list above does not apply — but a deleted
  surface is a documentation change too. Anything §10 removes (`#mode-switch`,
  a filter semantic, a per-ride question that becomes a standing setting) must
  leave the module header that explained it updated rather than orphaned, and
  §11 owes the tour audit a test rather than a promise.

---

## 16. Risks, in the order they are likely to bite

| # | Risk | Mitigation |
|---|---|---|
| 1 | **A favourite becomes a way to follow a person.** In-use vehicles broadcast a live moving position on a public endpoint; a targeted subscription to one is a different thing from a public map. | §8.4: position withheld server-side whenever `is_reserved`, an explicit `position_withheld` flag so nobody "fixes" it later, no location in the availability alert, a 10-favourite cap, and the QR gate on top. Write the rule into the endpoint's docstring the way `sql/076` writes down what dibs is not. |
| 2 | **The QR gate proves less than it looks like it proves.** `validate_scan` is a plate-knowledge check; nothing today compares position. | §8.2: require the 75 m proximity check as well, and say in the code comment why the scan alone is not enough — otherwise the next feature to reuse the gate inherits the wrong assumption. |
| 3 | **The phone is in a pocket and the tab is throttled.** The whole re-solve runs client-side in Phase 3. | Rev 3 makes this *less* pressing than rev 2 assumed: a rider mid-hand-off is riding, and a phone mounted for navigation has the tab in front. It still bites for the pocket case — say so in the UI. Phase 9 (§13) is the committed fix: opted-in SMS through `comms.py`, which already carries consent, quota and reply routing. |
| 4 | **Auto-dibs makes dibs worse for everyone.** Dibs' own rules exist to stop hoarding; a feature that claims automatically is exactly the pressure they were written against, and rev 3's unbounded chaining makes a plan want to claim *more* vehicles. | A plan holds **at most one claim at a time**, however many hops it intends — and the **server** keeps that invariant via `replaces`, in one transaction, rather than the client releasing first (§7.3). Watch the ratio of claims to rides in telemetry, and be willing to turn auto-claim off. |
| 5 | **Valhalla has no matrix, or its matrix disagrees with its routes.** Now MORE load-bearing than in rev 2: the scooter-to-scooter relation is N×N, and a fan-out over N² pairs is not viable. | Verify against the deployed image **before** building the endpoint. Without a matrix, this phase drops to **one hand-off maximum** and a bipartite search — still useful, but say so rather than discovering it late. §6.2. |
| 6 | **The plan search is expensive and rate-limited**, and rev 3's second matrix call is N×(N+1) rather than N×1. | Still two calls per search, and N is now explicitly bounded by §6.2's selection step (`W` first hops, `H` pickups) rather than by three rules that did not in fact bound it. Non-`risk` throughout, bar rule 1's first-hop escape; first hop inside the walk cap; `P`/`D` bbox. The client's straight-line tier carries the interactive list; the server call is reserved for the moment a decision is made. |
| 7 | **A re-solve chain sends somebody in a circle**, and rev 3 removed the hop counter that used to bound it. | Re-solve from current position, permanent `exclude`, and generalised cost that must strictly improve to be adopted. Telemetry on total legs and total minutes per trip is the check — a plan that keeps re-solving is a bug, not a feature. |
| 8 | **Spec too tight = nothing found**, and "no scooters match" reads as "no scooters". | The published relaxation ladder, `relaxed` on every response, and an EXHAUSTED state that says what was tried and offers the one-tap loosening. |
| 9 | **The map bridge desynchronizes.** A filter set that still claims to be "my ideal scooter" after the rider changed it is a lie the UI is telling. | §5.5's attach/detach rule, and the lossy direction stated on the toggle rather than discovered. |
| 10 | **Availability alerts become a firehose.** A popular scooter turns over several times a day. | One alert per favourite per 6 hours, none 22:00–07:00 Denver, opt-in per favourite and off by default. |
| 11 | **Equity advice that costs money.** Wrong tier, unmodelled Pass, a discount Veo does not apply. | Never for Access; price the worse VeoPlus reading; carry the screenshot caveat at the point of advice; never offer an equity hand-off whose only advantage is a saving under $0.50 (§9 — there is no separate "split" any more; it is a plan in the ordinary list). |
| 12 | **SMS becomes the thing people block.** A living plan re-solves on five triggers; texting each one spends the budget that protects the message that matters, and `dibs-notify.ts` caps at four alerts per claim *on purpose*. | SMS is strictly narrower than the in-app notice (§13.3): two events earn a text, a hard per-trip ceiling sits on top of the per-claim one, and "we checked and it is fine" is never sent. Consent is separate from the sign-in number and revocable. |
| 13 | **The resume link becomes a credential in a channel we do not control.** An SMS renders on a lock screen, persists in carrier logs, gets screenshotted, lands on shared handsets. | The link carries a plan reference and never a session (§13.5). A dead session means signing in and *then* resuming — asserted in tests, not assumed, because this is precisely the thing a later refactor "simplifies". |
| 14 | **Rapid checking buys load instead of freshness.** The client already polls every 90s against an ingest that runs every 2 min, so a faster global poll re-reads the same cycle. | Check *narrowly*, not *fast* — but note that narrowing does **not** shrink the fetch: GBFS is a whole-feed endpoint, so the saving is **coalescing**. One shared 20-second full-feed fetch serves every live plan (6× the ingest's requests, O(1) in riders); each plan reads only its 1–5 vehicles out of that snapshot. The global cadence — shared with the compliance audit — stays alone. §13.4. |
| 15 | **Advocacy inserts itself and loses its standing.** An organisation that answers every case is a nuisance; the cases where it matters are the ones where it is invited. | No reply into a case without `@WSYV` or `@advocacy` in it (§14.3), whatever the portal's pattern suggests. The rider's complaint stays the rider's. |
| 16 | **OCR misreads a receipt and we accuse somebody wrongly.** Receipt formats change without notice, and a misread total is a rider sent to lose an argument. | The rider confirms every extracted figure over their own screenshot before **either** complaint path is reachable — the `mailto:` as well as the clipboard fallback, since gating only one of them still lets an unconfirmed complaint be sent — and §12.5's three-part bar means "we cannot tell" is a frequent, designed answer rather than a failure. |
| 17 | **The evidence pile becomes a movement record.** Receipts are time, place and money tied to an account — stronger than anything else this program stores. | The image never leaves the device (§12.2); only confirmed fields upload. The pile stores the date, area and rates, not coordinates or the account identifier; the account link lives only as long as the rider needs it to withdraw. Consent is per-submission and withdrawal actually deletes. |
| 18 | **The aggregate gets overstated.** A self-selected sample of receipts from an app whose users already suspect they were overcharged is not a census of Denver. | The claim is always "N of M trips riders submitted", with its denominator attached and its self-selection named. Never a fraud accusation, whatever the number says. The evidence is worth something precisely because it is boring and checkable. |
| 19 | **The free-minutes estimate is wrong and the rider is billed.** Rides taken outside this app are invisible to it (§6.3.1). | The figure is a *ceiling* on what is left and is labelled as one, the rider can correct it before planning, and no plan is ever described as "free" on the strength of our estimate alone. |
| 20 | **Notification fatigue kills the alert that matters**, and rev 3 announces EVERY re-solve rather than only the ones outside an envelope. | Re-solve messages *replace* `taken`, never stack. One-tick hold, same four-per-claim ceiling. A re-solve that changes nothing the rider would act on is not announced at all — "we checked and the plan stands" is not news. |
| 21 | **`recommend.ts` and the new scorer disagree in front of the rider.** | They answer different questions and may differ in order. They share disqualification predicates and must never differ on what is rideable. Consider folding the drawer onto the plan search once Phase 2 is proven. |
| 22 | **The exchange rate is one number that silently re-ranks everything.** §6.3.0's `SECONDS_PER_CENT = 8` decides whether a $1 unlock beats thirteen minutes, and below ~4 s/¢ the Access cliff stops changing routes at all — so a well-meant "let's value time realistically" edit would quietly retire §6.3's state augmentation while every test still passed. | The constant lives in one place, shared by both tiers, with its two rider-facing consequences and the 4 s/¢ floor stated beside it. Crossover tests pin the 13-vs-14-minute boundary, so a change to the rate fails a test instead of changing the plan list in silence. Telemetry on hand-offs per plan per tier is the field check. |

---

## 17. What "done" looks like per phase

- **1.** A rider can write down what they like to ride, name it, have it on
  their other phone — and see only those on the map with one tap, with the
  drawer honest about the fact that it is showing them as requirements.
- **2.** Planning a trip returns *plans*, not vehicles: a rider whose ideal
  scooter is 14 minutes' walk away is offered a 90-second walk, a short ride
  and a hand-off to it, with the time and the cost of both unlocks on the
  card. No plan contains a `risk`-tier vehicle unless there was nothing
  non-risky within a 5-minute walk, and it says so when there wasn't.
- **3.** A rider riding toward a pickup loses it to somebody else, and before
  they have to think about it the remaining route is re-solved, the new claim
  is placed, and one message says what changed — with the runners-up one tap
  away if they disagree. The dibs chain in the database can say how often that
  happened and whether the rider accepted it.
- **4.** A rider standing at a scooter can keep it in two taps, find it again
  a week later, and be told when it comes free — and cannot, by any request
  the API will answer, see where it is while somebody is riding it.
- **5a.** A rider who would save real money by starting inside an Equity Area
  is told, in dollars, next to the extra walking minutes it costs.
- **5b.** A long trip that already crosses an Equity Area surfaces a hand-off
  inside it **in the ordinary plan list** — not on a special card — with the
  second unlock priced, the re-rent risk stated, and the screenshot caveat
  attached to the saving.
- **6.** `#mode-switch` is gone from `index.html` and nothing clicks a hidden
  element to start a ride; one model filter governs the map, with one meaning
  for an empty selection; a rider with an attached spec opens the ride
  surface already honouring it; and no question is asked per ride whose
  answer never changes.
- **7.** A first-time visitor sees a tour that describes the app in front of
  them, its last screen hands them to "where are you going?", and
  `ONBOARDING_AUTOSHOW` is `true` — with a test that fails if a screen starts
  describing a control that no longer exists.
- **9.** A rider whose pickup is taken while their phone is in their pocket
  gets one text, taps it, and is back in the plan — signing in again first if
  their session died. A rider who never opted in gets the same re-solve and no
  text. The plan-critical vehicles are checked every 20 seconds and the fleet
  cadence is untouched.
- **10.** A rider can send their complaint with `advocacy@weseeyouveo.com` on
  the CC because they ticked a box for that complaint, and the advocacy side
  stays silent in the case until somebody writes `@WSYV` — at which point the
  operator gets a text.
- **8.** A rider drops in a receipt for a trip that started in an Equity Area,
  is told in plain terms what they were charged and what Exhibit C says they
  should have been, and opens a ready-to-send complaint in one tap — with the image never
  having left their phone. A rider whose receipt cannot answer the question is
  told that, rather than guessed at. And the consented submissions can say, with
  a denominator attached, how often the discount was applied at all.
