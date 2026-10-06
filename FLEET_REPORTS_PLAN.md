# Fleet Reports — reports that stick, and the stewardship that requires

**Status:** specified, not started. Written 2026-10-06 against `main`
(`c421ac1` api / `22f4532` frontend).

**Scope:** two repositories. `z280/scooter-fyi-api` owns the report vocabulary,
the persistence rule, the public flag, the resolve endpoint and the admin
surface; `z280/denver-scooter-fyi` owns the button a rider presses, the scan
that explains a hidden scooter, what the map shows, and keeping suppressed
vehicles out of the Phase 2 planner. Every work item below names its repo.

**Three strands, one reason.** Reports that *stick* (§2.1-2.5) take scooters
off the map; scan-to-identify (§2.7) is how a rider standing in front of one
finds out why; the admin centre (§2.6, §2.8) is who can see the whole picture
and put a mistake right. The first strand is the request. The other two are
what make it safe to ship — a system that hides things owes an explanation to
the person it hid them from, and a lever to the people maintaining it.

**This is NOT part of the Along the Way program.** It shares no phase, no
migration and no module with `ALONG_THE_WAY_PLAN.md`. It is here because it
touches the same tables and deserves the same kind of document.

---

## 1. Why this exists

A rider found an Apollo — the most-wanted model in the fleet — parked **inside
a neighbour's yard, between the garage and the house, behind a fence**. The
scooter is fine. It is at 100% charge. It would ride perfectly. Nobody can get
it without walking onto private property, and nobody should.

They reported it to Veo. Then they went to report it to *other riders*, and the
app made them choose a label. They chose **Not rideable**, because it was the
closest of five, and it is wrong.

**The app models one axis and this is the other one.**

| Question | Who answers it today |
|---|---|
| **Will it ride?** | `not_rideable`, `dead_battery`, `damaged` → `has_negative_report` → `reliability_tier` |
| **Can I get to it?** | *nothing* |

`not_rideable` was actively harmful here. It is in the set that flips
`has_negative_report` server-side, so a perfectly good Apollo is now marked
unreliable — and because `reliability_tier` is scoped to the **vehicle**, that
mark follows it after Veo retrieves it and redeploys it three blocks away. The
scooter is punished for where somebody else parked it.

`improperly_parked` is the right *category* and still the wrong answer. It is
the lone member of `NON_RELIABILITY_REPORT_TYPES`, and its comment states the
principle exactly: *"A scooter blocking a sidewalk can still be a great ride,
so parking complaints stay out of the 'worth the walk?' signal."* True — but a
scooter blocking a sidewalk is **reachable**, and this one is not. The
compliance aggregate never reaches the rider looking at the map.

**Phase 2 of the other program makes this urgent rather than cosmetic.**
`rankPlans` will route a rider to that Apollo today, and because its reliability
tier is clean, the "likely rideable always" rule ranks it *well*. Under the
hand-off model it can be a **pickup** — so a rider rides a starter scooter to a
fence and finds nothing to hand off to.

---

## 2. The decisions

### 2.1 A sixth report type: `inaccessible`

Meaning: **the vehicle may be perfectly fine; you cannot lawfully or
reasonably reach it.** Private property, locked yard, inside a fence, behind a
construction hoarding, in a building.

- it joins `NON_RELIABILITY_REPORT_TYPES` — it says nothing about whether the
  scooter rides;
- it is nevertheless the **strongest possible "no"** to *"is this worth the
  walk?"*, which is why §2.4's suppression axis exists rather than reusing the
  tier;
- rider-facing copy must **discourage retrieval**, not merely inform: *"on
  private property — don't go in"*. A label that sends somebody over a fence to
  prove a point is worse than no label.

### 2.2 Reports stand until the device MOVES, not for 24 hours

This is already half-built, and the bug is one `AND` away from being right.
`api_public.py`'s `has_negative_report` reads:

```sql
WHERE nr.vehicle_identifier = r.vehicle_identifier
  AND nr.h3_10_index        = r.h3_10_index          -- clears when it MOVES
  AND nr.reported_at >= NOW() - INTERVAL '24 hours'  -- ...and also on a clock
```

The cell condition is exactly the rule we want, and the comment above it
already says so. **The interval is what lets a scooter sit untouched in a yard
and quietly rehabilitate itself overnight.**

**Drop the interval. Keep the cell test.** A report stands until the thing
actually moves. This applies to every report type, `improperly_parked`
included: a badly parked scooter that nobody has touched is still badly parked
tomorrow.

### 2.3 "Moved" means moved, not GPS jitter

h3 resolution 10 averages **~76 m edge, ~15,000 m²** (computed with the
`h3-js` already in the frontend). That is comfortably clear of GPS noise, so
a cell change is a good primary signal.

The gap is a vehicle parked near a **cell boundary**, where jitter alone can
flip the index and clear a live report. Reports already carry `lat`/`lng`
(`DeviceReportIn`), so:

> **Moved = the h3_10 index differs AND the device is ≥50 m from the reported
> point.** Both, not either.

### 2.4 Battery is an EVENT, never a level

The obvious second clearing rule — *"≥90% charge means somebody serviced it"* —
**is wrong, and the Apollo that prompted this plan is the counterexample.** It
is already at 100%, so a level threshold clears its own report the instant it
is filed. A fully charged scooter would be permanently unreportable.

Only a **rise** is evidence that a human touched the vehicle. If this is
implemented at all, it is "battery increased by ≥X points since the report",
never "battery is above X". For the case that prompted the plan, no rise is
possible and **movement is the only honest signal**.

### 2.5 Suppression is its own axis — do NOT overload `reliability_tier`

The goal is "keep it off the map". The tempting lever is to rate these
`high_risk`. **Don't.** §1 exists because the app conflates *will it ride* with
*can I reach it*; routing parking and access into `reliability_tier` re-merges
exactly what this plan separates, and a rider reading "high risk" learns
something false about the hardware.

Instead: a **suppression flag** on `/devices/current` — working name
`suppressed` with a machine-readable `suppressed_reason` — computed from
unresolved reports of any type, cleared by §2.2's movement rule.
`reliability_tier` stays about rideability. The device card can then say *why*
it is hidden, which "high risk" never could.

### 2.6 Removing the expiry forces two admin capabilities

> These two are forced by §2.2. The rest of the admin centre — the census
> lists, the per-device dossier, SMS watch — is in §2.8 and §4.1(5).

These are **consequences, not extras**. Shipping §2.2 without them is a
regression:

1. **A false report becomes permanent.** Today a mistake ages out in a day.
   Under the new rule it stands until the scooter moves — which, for a scooter
   nobody can reach, may be never. Admins must be able to **resolve or void a
   report**. This is the safety valve the 24 hours used to provide.
2. **Sticky reports are griefable.** One person can suppress a neighbourhood's
   fleet. The existing dedupe window (`_DEDUPE_WINDOW_MINUTES`) limits repeats
   of the *same* report, not volume across devices. Admins must be able to
   **see per-reporter activity** and discount a bad actor.

### 2.7 A hidden scooter must be able to explain itself

§2.5 takes a scooter off the map. That leaves a rider standing in front of one
with no way to find out why — and the thing they are looking at is *physically
present*, so its absence from the screen reads as a bug in the app rather than
a fact about the scooter. **Scan-to-identify therefore ships with suppression,
not after it.** Point the camera at the sticker, get the card for whatever is
in front of you, on the map or off it.

**This cannot be done on the client.** Ride mode resolves a plate against the
live feed the client already holds, and `qr-utility.ts` says so in as many
words: "the plate is used to look a vehicle up in a feed the client already
has". A suppressed device is *by construction* absent from that feed, so the
existing path returns `unknown_vehicle` — "a plate, but no vehicle in the feed
carries it". That is the exact dead end this feature exists to remove, and no
amount of frontend work reaches past it: `vehicle_identifier` is
`HMAC-SHA256(salt, plate)` and the salt is the server's — "anyone with the salt
can rederive; anyone without it cannot". **The client cannot name a device it
cannot already see.** Hence a server resolve endpoint, §4.1(6).

**The answer is a four-way distinction, and the modal must not blur it.** Once
this plan ships there are four reasons a scooter in front of you is missing
from the map, and they mean entirely different things:

| Reason | What happened | What to tell the rider |
|---|---|---|
| **Suppressed** (§2.5) | We are hiding it — an unresolved report, and it has not moved since | Which report, how old, and that it clears when the scooter moves |
| **Missing** (§2.8) | Veo's feed stopped carrying it | Where and when we last saw it — it may be in a van |
| **Permanently gone** (§2.8) | Missing, and an admin confirmed it is not coming back | Say so plainly |
| **Filtered** | It *is* on the map; the rider's own filters exclude it | Which filter, and offer to clear it — the only one of the four they can fix |

A scooter that is simply present and rideable also scans, and the modal says
so. "Why is this not on the map" is the hard case, not the only one.

**This also unblocks something already built.** `/api/v1/devices/qr-scan` has
shipped — authed, rate-limited, `sql/032` behind it — and nothing calls it.
`leaderboard-panel.ts` hides the `qr_scan` points action outright because
"advertising 100 pts for a flow that doesn't exist is a promise nobody can
collect on". A scan surface that reaches real devices is what lets that come
out of hiding, and the farm risk is already handled: the award is once per
account per device (`points.py`'s `credit_qr_scan_points` → `already_scanned_by_you`)
under a 20/hour account bucket. Worth doing in the same change; not required by
anything above it.

### 2.8 The census: devices arriving, and devices vanishing

Two admin lists — **newest devices added**, and **devices missing** spanning
back indefinitely, with an entry acknowledgeable into a separate **permanently
gone** list. The data already exists in `device_state`: `first_ever_observed_at`
("never reset") descending is the first list; `last_observed_at` ascending is
the second. Three decisions:

**The missing threshold is days, not hours.** The obvious constant is
`ABSENT_STOP_AFTER` (one hour) and it is the wrong one — it decides when to
close a *stop*, and its own header carries the measurement that rules it out
here: "under 2 h 1.1%, under 6 h 4.5%, and under 12 h 8.4%" of the fleet is
absent at any moment, because "the overnight pulls sit in the 2-12 h range and
redeploy through the morning". An hourly threshold produces a nightly list of
hundreds of scooters that are in a van and back by nine. Start at **72 hours**,
make it a query parameter, and keep it a number somebody can defend: the list
is only useful if appearing on it is unusual.

**Use `first_ever_observed_at`, never `first_observed_at_location`.** They sit
two lines apart in the same table and the second is commented "reset on
movement" — building the new-arrivals list on it would report every scooter
that moved this morning as new. This is the likeliest single bug in the
section; §5 has a test for it.

**Acknowledgement is a new table, not a column.** `device_state` is rewritten
by every ingest cycle, and an admin's judgement must not live where a cycle can
overwrite it. A small `device_census_ack` (`vehicle_identifier` PK, `status`,
`acknowledged_by`, `acknowledged_at`, `note`) holds it, and the join against it
is what makes "permanently gone" a separate list rather than a filter nobody
can audit. **It must survive a return**: a scooter marked gone that starts
reporting again is the most interesting row in the system — a van emptied, or a
vehicle recovered. Do not delete the ack on reappearance; surface the
contradiction.

---

## 3. What exists today (so nobody re-derives it)

### API — `z280/scooter-fyi-api`

| Thing | Where |
|---|---|
| The five report types | `src/api_frontend_reports.py:53` — `_REPORT_TYPES` |
| Types excluded from reliability | same file `:87` — `NON_RELIABILITY_REPORT_TYPES` |
| The SQL predicate for that exclusion | same file `:90` — `reliability_report_type_sql()` |
| Applied in the devices query | `src/api_public.py:460` |
| Applied in the h3 aggregate | `src/api_h3.py:125` |
| `has_negative_report`, 24h + h3_10 | `src/api_public.py:433-462` |
| Deprecated-alias seam (worth copying) | `api_frontend_reports.py:56-82` — how a rename ships across two repos that cannot deploy atomically |
| Points awarded per report type | `src/points.py:195` |
| Admin pages, OAuth-gated | `src/api_admin.py` — `cycles`, `failures`, `scheduler`, `regions`, `admins`, `analytics`, `campaigns` |
| Admin page pattern | `_render("name.html", ...)` + Jinja templates in `src/templates/` |
| SMS with consent, quota, STOP | `src/comms.py` — **already built**, see `ALONG_THE_WAY_PLAN.md` §13 |
| QR scan endpoint — authed, 20/hr, and **nothing calls it** | `src/api_qr.py:30` |
| Plate extraction and the match check | `src/qr.py:32` `extract_plate`, `:40` `validate_scan` |
| Why the client cannot resolve a hidden device | `src/identity.py:59-69` — salted HMAC, "anyone without it cannot" |
| QR payload registry | `sql/032_device_qr_codes.sql` |
| Fleet census columns | `sql/004_device_history.sql:26-28` — `first_ever_observed_at` (never reset), `last_observed_at`, and the `first_observed_at_location` trap two lines above |
| Why "absent" is not "missing" | `src/device_state.py:243-257` — `ABSENT_STOP_AFTER` and the fleet measurement behind it |

### Frontend — `z280/denver-scooter-fyi`

| Thing | Where |
|---|---|
| `DeviceReportType` + `submitDeviceReport` | `src/reports.ts:15` |
| "🚫 Not rideable" chip in the device popup | `src/devices.ts:1673` |
| `improperly_parked` fire-and-forget | `src/devices.ts:2443` |
| Why a `not_rideable` report overrides the tier | `src/devices.ts:1621` (comment) |
| Reliability tiers and their reasons | `src/reliability.ts` |
| The Phase 2 planner that must exclude these | `src/along-the-way.ts` — see §4.2 |
| Camera surface; hands back the raw payload and nothing else | `src/qr-scan.ts:182` — `openQrScanner` |
| The mode dial: union, spec table, wrapping rotate | `src/qr-utility.ts:30-80` |
| Client-side plate read — a lookup key, never a decision | `src/qr-utility.ts:118` — `plateFromQr` |
| **The dead end this feature removes** | `src/qr-ride-scan.ts:52-53` — `unknown_vehicle` |
| `qr_scan` points hidden because nothing calls the endpoint | `src/leaderboard-panel.ts:134` |
| Modal chrome: one-at-a-time, and its two precedents | `src/qr-utility.ts:157-158` |
| The focus trap both modals use | `src/modal-focus-trap.ts:39` — `trapFocusWithin` |

---

## 4. The work

### 4.1 API

1. **Migration.** Add `inaccessible` to `device_reports.report_type`'s CHECK
   constraint, and the matching points action if one is wanted.

   **THE NEXT FREE NUMBER IS 088 — but verify it yourself.** `ls sql/` on
   current `main`; the highest is `087_device_state_failed_start_rentals.sql`.
   A number written in a plan drifts, and this program has been bitten by that
   before.

   **Follow `sql/037`'s shape exactly**, which is the house pattern for this
   table: drop the constraint *before* rewriting rows and re-add it after,
   because the constraint would otherwise reject the very UPDATE that migrates
   the data — and drop `IF EXISTS` under **both** historical names
   (`device_reports_report_type_allowed` and the older inline
   `device_reports_report_type_check`), since instances predating `sql/023`
   carry the other one. `sql/029`'s header documents the replay-safety bug this
   shape fixes; re-read it before writing the file.

2. **Accept the type.** Add to `_REPORT_TYPES` **and**
   `NON_RELIABILITY_REPORT_TYPES`. No alias is needed — this is a new value,
   not a rename — but read the alias comment anyway: it explains the merge
   ordering between these two repos, which this change also has (ship the API
   first; the button 422s against an old backend otherwise).

3. **Persistence.** Remove the `INTERVAL '24 hours'` clause from both
   `has_negative_report` subqueries in `api_public.py`, and add the ≥50 m
   displacement test from §2.3. Check `api_h3.py:125` for the same window.

4. **Suppression flag.** Add `suppressed` + `suppressed_reason` to
   `/devices/current`, computed from unresolved reports of **any** type under
   the same movement rule. Document that it is deliberately separate from
   `reliability_tier`, citing §2.5, or somebody will "simplify" them together.

5. **Admin surface** — new pages in `src/api_admin.py`, same `_render` +
   template pattern:
   - **Reports queue**: recent reports, type, timestamp, reporter (when signed
     in), device state at report time, dedupe status. Filterable by type and
     region.
   - **Per-device dossier**: one vehicle's reports, moves, idle time, battery
     history, and whether anything changed after a report. *This is the view
     that proves "repeatedly hidden at the same spot".*
   - **Resolve / void a report** — §2.6(1). Audited: who voided it and why.
   - **Reporter view** — §2.6(2). Per-account report volume and spread.
   - **Watch a device via SMS** — subscribe an admin to movement/state changes
     on one vehicle. `comms.py` already does SMS with consent, quota and STOP,
     and the dibs watcher already watches vehicles; this is wiring, not new
     machinery.
   - **Export for advocacy**: "N vehicles reported inaccessible, M still
     unmoved after X days". That is Veo's retrieval obligation, documented.

6. **Resolve endpoint** (§2.7) — `POST /api/v1/devices/identify`, taking the
   raw QR payload exactly as `qr-scan.ts` yields it and returning the device
   card plus §2.7's reason. Reuse `qr.py`'s `extract_plate` and `identity.py`'s
   `hash_plate`; do **not** re-implement either. It must answer for a device
   that is suppressed, missing or gone — those are the cases it exists for — so
   it reads `device_state` directly rather than the `/devices/current` feed.

   **Session-required and rate-limited, and for a stronger reason than the scan
   endpoint.** This maps a plate to a `vehicle_identifier`, which is precisely
   the mapping the salt exists to withhold. Open or unmetered, it is a
   plate-enumeration oracle that inverts the privacy model for the whole fleet
   in one script. Copy `api_qr.py`'s shape: `require_session` plus `enforce` on
   an account bucket at 20/hour. Somebody will argue it should be open because
   the plate is printed on the scooter in public — the answer is that a plate
   is public *one scooter at a time, to someone standing next to it*, and this
   endpoint would be public *all at once, to someone who is not in Denver*.

7. **Census endpoints and pages** (§2.8) — newest arrivals by
   `first_ever_observed_at DESC`; missing by `last_observed_at` older than a
   `hours=` parameter defaulting to 72; permanently gone as the join against
   the new `device_census_ack` table, which needs its own migration alongside
   the §4.1(1) one. Admin actions: acknowledge, un-acknowledge, note.

   `idx_device_state_last_observed (last_observed_at DESC)` already exists and
   serves the missing list. **There is no index on `first_ever_observed_at`** —
   add one, or the arrivals page sorts the whole fleet on every load.

   A device acknowledged gone that reappears is surfaced, not silently
   relisted (§2.8). Give it somewhere to be seen: a count on the census page is
   enough, but it must not be nowhere.

### 4.2 Frontend

1. **The button.** A sixth option in the device popup's report chips
   (`devices.ts:1673` is the pattern). Copy discourages retrieval (§2.1).
2. **Show suppression honestly.** A suppressed device is kept out of the
   rider's available set, and the card says *why* — "reported inaccessible",
   not "high risk".
3. **Exclude from the planner.** `along-the-way.ts`'s `toCandidates()` already
   drops `is_disabled` / `is_reserved`; suppressed vehicles go the same way —
   **excluded, not penalised**. A ranking that can be outvoted will eventually
   send somebody over a fence.
4. **A third QR mode** (§2.7). `QrUtilityMode` is already a union with a spec
   table and a wrapping dial; add `"identify"` to `QR_UTILITY_MODES` and an
   `onIdentify` to `QrUtilityDeps`. `rotateMode` is written against
   `QR_UTILITY_MODES.length` and needs no change — **but its comment does**: it
   justifies wrapping over clamping in terms of "two positions", and that
   sentence stops being true. The file states that "dial order is deliberate",
   so: identify goes **first**, by the same reasoning that put features there —
   it is the one a rider does knowing nothing, with nothing in flight, and it
   is the only read-only mode of the three.
5. **The modal.** A modal device card — "a modal version of the scooter
   pop-up". `devices.ts:1673`'s popup is the content to mirror; the chrome is
   `trapFocusWithin` plus the one-at-a-time rule `qr-utility.ts:157-158` states
   and `qr-scan.ts` and `device-features.ts` already follow.
   **It must render for a device with no marker** — that is the entire point,
   and it is the requirement a map-coupled implementation will quietly fail.
   Dropping a temporary marker on the map is explicitly optional; if it is
   built, a suppressed device must not survive the modal closing.
6. **Say which of the four.** Each §2.7 reason gets its own sentence. The
   failure mode to avoid is a generic "we don't know about this scooter",
   which is what the rider already gets today and the reason this exists.
7. The census lists are admin-only. No rider-facing work.

---

## 5. Tests

- A report with no movement is **still live a week later** — the case the
  24-hour window silently dropped.
- Moving the device **one h3_10 cell but only 20 m** (boundary jitter) does
  **not** clear a report; moving it 200 m does.
- An `inaccessible` report does **not** change `reliability_tier`, and **does**
  set `suppressed`.
- An `improperly_parked` report likewise suppresses without touching the tier.
- A device at **100% battery** can be reported and the report stands — §2.4's
  regression, written against the scooter that prompted the plan.
- A suppressed vehicle appears in **no** `rankPlans` output: not as a first
  hop, not as a pickup, not at any rank.
- Voiding a report clears suppression immediately and is attributable.
- The migration replays cleanly against a database that already has the
  constraint under either historical name.

Scan-to-identify (§2.7):

- **A suppressed device resolves from a scan and the modal names the report.**
  Written against the Apollo behind the fence: the device is off the map, the
  rider is standing in front of it, and the app explains itself. This is the
  acceptance test for the whole section.
- Each of the four reasons renders its own sentence; none falls through to a
  generic "not found".
- A payload `plateFromQr` reads as nothing — a wifi QR, a URL — reaches
  "unreadable" without a network call.
- The resolve endpoint refuses an unauthenticated caller, and rate-limits a
  scripted one at the same ceiling as `qr-scan`.
- `rotateMode` wraps over three modes in both directions.

Census (§2.8):

- The missing list **excludes** a device absent 2 hours and **includes** one
  absent 5 days — the overnight-van regression.
- Arrivals order by `first_ever_observed_at`. Pin this with a device that moved
  recently: `first_observed_at_location` resets on movement, so an
  implementation that reaches for it reports this morning's relocations as new
  scooters, and every other test still passes.
- A device acknowledged permanently gone that reappears in the feed is
  surfaced, and its acknowledgement is **not** deleted.
- An ingest cycle rewriting `device_state` leaves acknowledgements intact.

---

## 6. Risks, and two things not to build

| | Risk | Mitigation |
|---|---|---|
| 1 | **A false report is now permanent.** | §2.6(1)'s void, shipped in the same change — not later. |
| 2 | **Griefing.** One account suppresses a neighbourhood. | §2.6(2)'s reporter view; consider a per-account rate limit beyond the dedupe window. |
| 3 | **"Inaccessible" becomes a way to point at a household.** | See below. This is the one that would do real harm. |
| 4 | Suppression hides a problem instead of surfacing it. | The vehicle stays visible to **admins** and in the export; it is removed from *rider* candidacy only — and §2.7 lets any rider standing in front of it ask why. |
| 5 | **The resolve endpoint becomes a plate-enumeration oracle.** It hands out the plate → `vehicle_identifier` mapping the salt exists to withhold. | §4.1(6): `require_session` + an account rate bucket. Never open it, however reasonable the argument sounds. |
| 6 | **The missing list is noise and nobody reads it.** An hourly threshold lists the overnight van every night. | §2.8's 72-hour floor, taken from the measurement in `device_state.py`'s own header rather than guessed. |
| 7 | A map-coupled identify modal passes review and fails the only case it was built for. | §4.2(5): the acceptance test is a device with **no marker**. |

**Do not build a map of addresses where scooters disappear.** The report is
about **a spot being unreachable**, never about who lives there. The Along the
Way risk table already refuses the adjacent feature — risk 1, "a favourite
becomes a way to follow a person" — and this would be the same thing with a
different label, aimed at a private individual who has not agreed to be
tracked. If this feeds the advocacy evidence pile, it documents **Veo's failure
to retrieve a vehicle repeatedly reported inaccessible**, which is their permit
obligation and a fair thing to publish.

**Do not route parking or access into `reliability_tier`** (§2.5). It is the
one shortcut that makes the whole plan pointless.
