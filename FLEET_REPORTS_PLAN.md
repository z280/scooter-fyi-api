# Fleet Reports — reports that stick, and the stewardship that requires

**Status:** specified, not started. Written 2026-10-06 against `main`
(`c421ac1` api / `22f4532` frontend).

**Scope:** two repositories. `z280/scooter-fyi-api` owns the report vocabulary,
the persistence rule, the public flag and the admin surface;
`z280/denver-scooter-fyi` owns the button a rider presses, what the map shows,
and keeping suppressed vehicles out of the Phase 2 planner. Every work item
below names its repo.

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

### Frontend — `z280/denver-scooter-fyi`

| Thing | Where |
|---|---|
| `DeviceReportType` + `submitDeviceReport` | `src/reports.ts:15` |
| "🚫 Not rideable" chip in the device popup | `src/devices.ts:1673` |
| `improperly_parked` fire-and-forget | `src/devices.ts:2443` |
| Why a `not_rideable` report overrides the tier | `src/devices.ts:1621` (comment) |
| Reliability tiers and their reasons | `src/reliability.ts` |
| The Phase 2 planner that must exclude these | `src/along-the-way.ts` — see §4.2 |

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

---

## 6. Risks, and two things not to build

| | Risk | Mitigation |
|---|---|---|
| 1 | **A false report is now permanent.** | §2.6(1)'s void, shipped in the same change — not later. |
| 2 | **Griefing.** One account suppresses a neighbourhood. | §2.6(2)'s reporter view; consider a per-account rate limit beyond the dedupe window. |
| 3 | **"Inaccessible" becomes a way to point at a household.** | See below. This is the one that would do real harm. |
| 4 | Suppression hides a problem instead of surfacing it. | The vehicle stays visible to **admins** and in the export; it is removed from *rider* candidacy only. |

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
