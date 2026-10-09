# Fleet Reports — reports that stick, and the stewardship that requires

**Status:** Phase 1 implemented in #144, with the owner's 2026-10-09 overrides in #145. **Phase 1b
(condition checks, §4.4) and Phase 2 (the admin centre, §4.1(5)) implemented together in one PR
(owner's request, 2026-10-09) — decisions in §4.5.** Phase 3 frontend pending; its contract is
`docs/reference/API.md` "Condition checks" and `needs_condition_check`.
Revised 2026-10-07 against `main` (`e442e2c` api / `5aac9ba` frontend);
Phase 1 re-verified every API citation against `main` at `044a424` on
2026-10-09 — see §4.0 for what had moved and what was wrong.

**Phases.** Phase 1 = §4.1 items 1-4, 6 and 7 (the API: data model,
suppression, identify, census and export endpoints). Phase 1b = §4.4
(condition checks). Phase 2 = §4.1(5), the admin pages, built on Phase 1's
`/api/v1/private/*` JSON. Phase 3 = §4.2, the frontend.

**Read §2.2 first if you are here to implement.** The first revision of this
plan got the current behaviour wrong: it described reports-until-movement as new
work when the accountable half already ships, and its §4.1(3) would have
unclocked the *anonymous* branches — making unaccountable reports permanent,
which is the plan's own risk 2. §2.2, §2.3 and §4.1(3) are rewritten, and the
retractions are left in place rather than quietly corrected, because the
original claims are the ones another agent would otherwise re-derive.

**Every `file:line` in §3 is machine-checked against the commits named above —
and that claim is narrower than it first read.** The checker matches citations
carrying a LINE NUMBER. Bare filename references were never checked, which is
how `src/along-the-way.ts` sat in §3 and §4.2 as though it were on the frontend
mainline when it exists only on PR #94's branch. Both rows now say so.

The first revision's line numbers were not checked at all — written against a
working copy, off by 19 lines in `api_frontend_reports.py`, which is how the
superseded predicate got quoted as current. A citation in this document is a
claim about a specific commit; re-check them if `main` has moved, and do not
read a bare filename as having been verified.

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
migration and no module with `docs/ALONG_THE_WAY_PLAN.md`. It is here because it
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

`not_rideable` was the wrong claim here. It is in the set that flips
`has_negative_report` server-side, so a perfectly good Apollo is marked
unreliable on the strength of where it is parked.

**An earlier revision of this paragraph overstated it**, and the overstatement
is worth keeping visible because it was the one claim in §1 with no `file:line`
behind it. It said the mark "follows it after Veo retrieves it and redeploys it
three blocks away". It does not. `compute_reliability_tier` takes
`has_negative_report` and `number_failed_starts`, and **both reset on
movement** — every `has_negative_report` branch clears on a move, and
`sql/004` marks `number_failed_starts` "reset on movement" in as many words. A
retrieval and redeployment clears it.

What is true is narrower and still enough: for exactly as long as the scooter
sits behind that fence, the fleet's most-wanted model carries a false claim
about its *hardware* — and the rider who filed it had no way to say the true
thing instead. The argument for this plan does not need the mark to outlive the
situation.

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

### 2.2 Reports stand until the device MOVES — MOSTLY ALREADY TRUE

**An earlier revision of this section was wrong, and wrong in the most
expensive way a plan can be: it proposed as new work something the codebase
already does, and quoted a predicate that is not the one that matters.** It
showed the `negative_reports` branch — 24 hours plus an h3_10 cell test — and
read it as "the" rule. There are **three** branches, and the third already
implements this decision.

`api_frontend_reports.py:83` states the shipped rule in prose, and
`api_public.py:490-523` implements it:

| Source | How long it counts |
|---|---|
| `negative_reports` (map-pin rows, no account column at all) | 24 hours, in the vehicle's h3_10 cell |
| `device_reports` with no `account_id` | 24 hours, in the cell |
| **`device_reports` with an `account_id`** | **until the vehicle MOVES or comes back at a FULL CHARGE** — not time-boxed, not cell-scoped |

And the reasoning is already written down, in words this plan was about to
reinvent: *"the useful question about it is not 'how long ago?' but 'has
anything happened since?'"*, and *"a signed-in report on a scooter nobody
touches holds indefinitely, which is the point."*

**So the split is deliberate: accountable reports persist, anonymous ones age
out because nobody's name is on them.** That is a griefing control, and it is
this plan's own risk 2 solved in advance.

**WHAT THIS MEANS FOR THE WORK.** Do **not** remove the 24-hour interval. The
two branches that carry it are the anonymous ones, and unclocking them makes
unaccountable reports permanent — exactly the abuse §2.6(2) warns about. What
actually remains on this axis is narrower than the original section claimed:

1. **The new `inaccessible` type must reach the accountable branch**, which it
   does for free: that branch filters on `reliability_report_type_sql`, so
   adding the type to `NON_RELIABILITY_REPORT_TYPES` **excludes** it from
   `has_negative_report` (correct — it is not a rideability claim) and §2.5's
   separate suppression flag is what must pick it up.
2. **`improperly_parked` persistence is NOT free.** The original section said
   the movement rule "applies to every report type, `improperly_parked`
   included". It cannot, as written: that type is excluded from every branch by
   `reliability_report_type_sql`. Parking persistence has to ride §2.5's
   suppression flag, not `has_negative_report`.

   **Overridden by the owner, 2026-10-09: `improperly_parked` does NOT
   suppress.** *"Improperly Parked is not the same as Inaccessible/Can't Find
   it. The latter should avoid especially if on private property, the
   improperly parked is a report to veo."* A badly parked scooter is reachable
   and rideable, so it stays on the map. It needs no persistence on any rider
   axis: it is stored, counted in the admin export, the dossier and the
   reports queue, and is Veo's to act on. `inaccessible` and `not_found` DO
   suppress — riders are steered away — and, because either can point at
   private property, neither's location is published (public CSV blank,
   identify's `last_seen` rounded to ~100 m).
3. **The full-charge clause is wrong** — §2.4, which is now this plan's one
   real finding about expiry.

### 2.3 "Moved" means moved — and the shipped mechanism is better than the one this plan proposed

**Also retracted.** The original rule was *"the h3_10 index differs AND the
device is ≥50 m from the reported point"*, justified by jitter flipping a cell
index near a boundary. Two things are wrong with it.

**The accountable branch does not use cells at all.** It uses
`ds.first_observed_at_location <= dr.reported_at` — `device_state`'s
"reset on movement" column, which the ingest advances when a vehicle moves past
its stationary threshold. `api_public.py:505` says why, and it is the better
argument: *"`first_observed_at_location` already answers 'has it moved?' more
precisely than a cell comparison can"*. A cell test cannot see a 60 m move
within one cell; this does. **Adopting the proposed rule would have been a
regression dressed as a fix.**

**And the coordinates it leans on are nullable.** `sql/013` declares `lat`,
`lng` and `h3_10_index` all nullable, with a comment that the cell is
"anchored to the SCOOTER's current cell when coords are absent". An
`AND`-joined distance test is therefore NULL for a coordinate-less report, and
the report would never clear at all.

**So: no change here.** The movement signal is `first_observed_at_location`,
already shipped, and the NULL handling is already right — `IS NULL` holds the
flag, "which is the safe direction for a claim that the scooter does not work".
A later refinement should start from that column, not from cells.

### 2.4 Battery is an EVENT, never a level — and this one IS a live bug

This is the decision that survives review, and it is now precisely located
rather than hypothetical. The accountable branch ends with
(`api_public.py:521-523`):

```sql
AND (r.current_range_meters IS NULL
     OR r.current_range_meters < %s)   -- "...and not charged back to full"
```

That is a **level** test. **The Apollo that prompted this plan is the
counterexample, and it defeats the shipped rule, not a hypothetical one:** it
sits at 100% behind the fence, so `current_range_meters < threshold` is false
the moment the report is filed, and the report clears instantly. A fully
charged scooter is effectively unreportable today.

Only a **rise** is evidence a human touched the vehicle. The fix is to compare
against the charge **at report time** — which means `device_reports` has to
record it, so this needs a column, not just a predicate change. For the case
that prompted the plan no rise is possible and movement is the only honest
signal, which is why §2.3's mechanism carries the weight.

**§5's "a device at 100% battery can be reported and the report stands" test
fails today.** That is the regression test for this section, and it is the one
test in this plan that already has a bug to catch.

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

### 2.9 Broken parts come from feature confirmation (`poor_condition`)

*Owner, 2026-10-09.* The advocacy export must count broken **bells, cup
holders and baskets** (and phone holders, which the same data carries). That
data **already exists**: when a rider confirms a scooter's features
(`POST /api/v1/reports/device-features`, the frontend's
`device-features.ts`), they are asked which of the features present are not
in good condition, stored as `device_feature_reports.poor_condition` (sql/055;
basket since sql/058) and folded by `src/device_features.py` into
`device_state.features_poor_condition` under the consensus rules — first
valid report authoritative, a disagreeing report opens `needs_review`, a
2-of-3 vote resolves it. **There is no second capture path, and none should
be built** (an earlier draft of this decision proposed a `damaged_parts`
field on device reports; it was withdrawn before it shipped).

- **Definition.** A vehicle has a broken *part* when its consensus says the
  part is present **and** lists it in `poor_condition`, and the vehicle is
  not in `needs_review`. A later report saying the part is fine disagrees
  with the consensus and moves the vehicle to `needs_review`, so disputed
  ones are counted separately (`under_review`), never as broken. The sample
  is vehicles the feed carried in the export window; the denominator is
  vehicles whose consensus has the part.
- **Broken parts never suppress.** A scooter with a broken bell still rides
  and is still reachable; hiding it would be §2.5's mistake in reverse. It
  counts in the export and the per-device dossier, and nowhere a rider is
  routed.

### 2.10 Why not rideable: a reason, two decoys, and when it was seen

*Owner, 2026-10-09.* A `not_rideable` report may say why: `acceleration`,
`flat_tire`, `wheel`, `lighting`, `seat`, `handlebar` (`device_reports.reason`,
sql/100; NULL = unspecified, so older clients keep working). The same picker
offers two **decoys** that are not reasons but different reports, and the
**server** re-files them so every client gets it right: "cannot find" →
`not_found` (owner, 2026-10-09, corrected after #144 shipped it as
`inaccessible`; sql/101 moves the constraint and re-files any such row.
`inaccessible` stays its own type, for a
scooter you can see but cannot reach — fenced in, locked inside, private
property), "dead battery" → `dead_battery`. The decoy picked is kept in
`device_reports.submitted_reason`, so a remap is visible rather than silent.
`observed_at` — when the rider saw it — is optional, defaults to the
submission time, and is refused in the future or more than 30 days back. The
reason breakdown and observed dates go to the admin export and the dossier;
the public CSV gains nothing.

---

## 3. What exists today (so nobody re-derives it)

### API — `z280/scooter-fyi-api`

| Thing | Where |
|---|---|
| The five report types | `src/api_frontend_reports.py:53` — `_REPORT_TYPES` |
| **How long a report counts — the shipped rule, in prose** | same file `:83-101`. Read this before §2.2 |
| Types excluded from reliability | same file `:106` — `NON_RELIABILITY_REPORT_TYPES` |
| The SQL predicate for that exclusion | same file `:109` — `reliability_report_type_sql()` |
| `has_negative_report` — **three** branches | `src/api_public.py:490-524` |
| The accountable branch (no clock, no cell) | `src/api_public.py:505-523` |
| The full-charge level test §2.4 must replace | `src/api_public.py:521-523` |
| Applied in the h3 aggregate, **snapshot-bounded** | `src/api_h3.py:42` (type filter), `:120`/`:126` (windows) |
| `device_reports` schema — nullable `lat`/`lng`/`h3_10_index`, **no resolution state** | `sql/013_frontend_reports.sql:15-30` |
| Deprecated-alias seam (worth copying) | `api_frontend_reports.py:56-82` — how a rename ships across two repos that cannot deploy atomically |
| Points awarded per report type | `src/points.py:195` |
| Admin pages, OAuth-gated | `src/api_admin.py` — `cycles`, `failures`, `scheduler`, `regions`, `admins`, `analytics`, `campaigns` |
| Admin page pattern | `_render("name.html", ...)` + Jinja templates in `src/templates/` |
| SMS with consent, quota, STOP | `src/comms.py` — **already built**, see `docs/ALONG_THE_WAY_PLAN.md` §13 |
| QR scan endpoint — authed, 20/hr, and **nothing calls it** | `src/api_qr.py:30` |
| Plate extraction and the match check | `src/qr.py:32` `extract_plate`, `:40` `validate_scan` |
| Why the client cannot resolve a hidden device | `src/identity.py:59-69` — salted HMAC, "anyone without it cannot" |
| QR payload registry | `sql/032_device_qr_codes.sql` |
| Fleet census columns | `sql/004_device_history.sql:27-28` — `first_ever_observed_at` (never reset), `last_observed_at`. The `first_observed_at_location` trap is `:25`, "reset on movement" — and it is also §2.3's movement signal |
| Why "absent" is not "missing" | `src/device_state.py:243-257` — `ABSENT_STOP_AFTER` and the fleet measurement behind it |

### Frontend — `z280/denver-scooter-fyi`

| Thing | Where |
|---|---|
| `DeviceReportType` + `submitDeviceReport` | `src/reports.ts:15` |
| The report chips in the device popup | `src/devices.ts:1698` |
| `improperly_parked` fire-and-forget | `src/devices.ts:2399` (contract), `:2468-2474` (the call) |
| Why a `not_rideable` report overrides the tier | `src/devices.ts:1646` (comment) |
| Reliability tiers and their reasons | `src/reliability.ts` |
| The Phase 2 planner that must exclude these | `src/along-the-way.ts` — **check whether it is on frontend `main` yet.** When this was written it existed only on PR #94's branch; that PR may since have merged. See §4.2 |
| Camera surface; hands back the raw payload and nothing else | `src/qr-scan.ts:182` — `openQrScanner` |
| The mode dial: union, spec table, wrapping rotate | `src/qr-utility.ts:30-80` |
| Client-side plate read — a lookup key, never a decision | `src/qr-utility.ts:118` — `plateFromQr` |
| **The dead end this feature removes** | `src/qr-ride-scan.ts:52-53` — `unknown_vehicle` |
| `qr_scan` points hidden because nothing calls the endpoint | `src/leaderboard-panel.ts:134` |
| Modal chrome: one-at-a-time, and its two precedents | `src/qr-utility.ts:157-158` |
| The focus trap both modals use | `src/modal-focus-trap.ts:39` — `trapFocusWithin` |

---

## 4. The work

### 4.0 Phase 1 re-verification (2026-10-09, against `044a424`)

Every `file:line` in §3 was re-checked before building. What moved, and what
was wrong:

- **§2.4 is still a live bug, confirmed.** The level test had moved to
  `api_public.py:~545` and `api_h3.py:~140`, and **a test asserted the bug**:
  `test_negative_report_hold_pg.py::test_a_full_charge_clears_it`. Replaced by
  a rise test and by §5's "100% can be reported and stands" test.
- **The next migration number was 100**, not 089: `main` had reached
  `sql/099`. Checked with `scripts/check_migration_numbers.py` and the open
  PR list.
- **`sql/037`'s device_reports block was unguarded.** §4.1(1) says `sql/029`
  documents why an unguarded drop/re-add of this constraint is a replay
  bug — but `sql/037` still had one, so the first stored `inaccessible` row
  would have broken the next whole-directory replay. Guarded in place (as
  `sql/037`'s own `user_points` block already was).
- **Resolution state (§4.1(3b)) and the `first_ever_observed_at` index were
  both still absent**, as the plan said.
- **`docs/reference/API.md` still described `has_negative_report` as the
  24-hour cell rule only**, two rules out of date. Rewritten.
- **The public monthly CSV publishes every device report's point rounded to
  ~100 m.** For `inaccessible` that is somebody's yard, and §6 refuses "a map
  of addresses where scooters disappear": its coordinates are blanked there.
- **§5's "the resolve endpoint refuses an unauthenticated caller" and risk 5's
  mitigation contradicted the owner's 2026-10-08 decision** that #143 wrote
  into §4.1(6). Both are amended below.

### 4.1 API

1. **Migration.** Add `inaccessible` to `device_reports.report_type`'s CHECK
   constraint, and the matching points action if one is wanted.

   **THE NEXT FREE NUMBER WAS 088 WHEN THIS WAS WRITTEN, AND IS NOT ANY MORE.**
   `main` now carries `sql/088_standardise_movement_radius.sql`, so the next free
   number is **089 — and verify that too**, with `git ls-tree origin/main sql/`
   rather than a local `ls`, which can be stale. This is the second time this
   plan has been wrong about it.

   Check the OPEN PULL REQUESTS as well, not just `main`: #105 adds its own
   `sql/088_discount_reports_equity_areas.sql`, which already collides with
   main's 088. Two branches picking the same number is the failure mode here,
   and `main` alone will not show it to you.

   **Read `sql/029` BEFORE `sql/037`.** An earlier revision of this item said
   to copy 037's shape "exactly"; that is wrong for this change. 037's
   drop-then-re-add exists because it *rewrites rows*, and the old constraint
   would reject the very UPDATE doing the migration. **Adding a permitted value
   rewrites nothing**, so it needs no unguarded drop — and `sql/029` is the file
   that documents why an unguarded drop/re-add on this constraint is a
   replay-safety bug. Still drop `IF EXISTS` under **both** historical names
   (`device_reports_report_type_allowed` and the older inline
   `device_reports_report_type_check`), since instances predating `sql/023`
   carry the other one.

2. **Accept the type.** Add to `_REPORT_TYPES` **and**
   `NON_RELIABILITY_REPORT_TYPES`. No alias is needed — this is a new value,
   not a rename — but read the alias comment anyway: it explains the merge
   ordering between these two repos, which this change also has (ship the API
   first; the button 422s against an old backend otherwise).

3. **Persistence — MOSTLY ALREADY SHIPPED, and the original instruction here
   was actively harmful.** It said to remove the `INTERVAL '24 hours'` from both
   `has_negative_report` subqueries and add a ≥50 m displacement test. Do
   **neither**:

   - The two clocked branches are the **anonymous** ones (`negative_reports`,
     and `device_reports` with a NULL `account_id`). Unclocking them makes
     unaccountable reports permanent — this plan's own risk 2, and the abuse
     §2.6(2) exists to contain. The accountable branch is already unclocked.
   - The ≥50 m cell rule would **regress** the shipped
     `first_observed_at_location` test (§2.3), and leans on nullable
     coordinates.
   - `api_h3.py`'s windows are bounded against the **snapshot** time
     (`%(snap)s - INTERVAL '24 hours'`), not `NOW()`. Removing them would
     retroactively reshade historical cells, changing aggregates for days that
     have already been published. Leave them alone.

   What remains is §2.4's battery fix, and §4.1(3a) below.

3a. **Record the charge at report time.** §2.4 needs a *rise*, which cannot be
   computed from a level alone — `device_reports` has to store the range (or
   battery) as it stood when the report was filed. New nullable column in the
   same migration as §4.1(1); the predicate then compares `r.current_range_meters`
   against that row's recorded value instead of a fixed threshold. A NULL
   recorded value must behave exactly as the current NULL branch does: clear
   nothing.

3b. **Add resolution state, or §4.1(4) and §2.6(1) cannot be built.** Both
   speak of "unresolved" reports, and `sql/013` has no column for it — no
   `resolved_at`, no `voided_by`, nothing. Without it there is no way to express
   an admin voiding a report, and `suppressed` has no "unresolved" to compute
   from. Add `resolved_at`, `resolved_by` and a short `resolution` note, in the
   same migration. **This was missing from every earlier revision of this plan**
   and is the one gap that blocks two other items outright.

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
     Plus vehicles with an unresolved **broken bell, cup holder or basket**
     (and phone holder) from feature confirmation (§2.9), and the
     not-rideable reason breakdown with observed dates (§2.10). The data is
     Phase 1's `GET /api/v1/private/reports/export` (JSON and CSV, with
     window, sample and definitions); the page is Phase 2.

6. **Resolve endpoint** (§2.7) — `POST /api/v1/devices/identify`, taking the
   raw QR payload exactly as `qr-scan.ts` yields it and returning the device
   card plus §2.7's reason. Reuse `qr.py`'s `extract_plate` and `identity.py`'s
   `hash_plate`; do **not** re-implement either. It must answer for a device
   that is suppressed, missing or gone — those are the cases it exists for — so
   it reads `device_state` directly rather than the `/devices/current` feed.

   **Access rule: superseded by the owner's decision of 2026-10-08.** This
   section originally required a session and an account bucket at 20/hour, on
   the argument that a public plate → `vehicle_identifier` mapping is a fleet
   enumeration oracle. The owner chose the opposite, and it is live: the
   plate lookup `GET /api/v1/vehicles/resolve?plate=` (scooter-fyi-api#134) is
   **public, rate-limited to 30/min per IP**, returns `device_id` +
   `vehicle_identifier` and never the plate, answers 404 when no vehicle or
   more than one carries the plate, and is redacted from the access log. That
   is what lets a signed-out rider scan a sticker or open a `?ride=plate:`
   link (denver-scooter-fyi#120). **Build this endpoint to the same rule** —
   public, per-IP limited, never echoing the plate — or extend
   `/vehicles/resolve` itself to read `device_state` for suppressed, missing
   and gone vehicles rather than adding a second plate oracle with a
   different policy. The forward direction (vehicle → plate) stays
   signed-in only (`GET /api/v1/vehicles/plates`).

   **Phase 1 built the extension, not a second endpoint:**
   `GET /api/v1/vehicles/resolve` takes `qr=` (the raw payload, read with
   `qr.py`'s `extract_plate`) as an alternative to `plate=`, and
   `explain=true` returns `status` — `on_map`, `suppressed`, `missing`,
   `gone` — with the standing reports, reading `device_state` (matched by
   `identity.py`'s `hash_plate` or the stored plate) for vehicles the feed no
   longer carries. Same public, 30/min-per-IP, never-echo, 404-on-none-or-
   ambiguous rule. "Filtered" is the client's to detect.

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
3. **Exclude from the planner.** **Check where `along-the-way.ts` lives before
   you start.** When this was written it existed only on PR #94's branch, not on
   the frontend commit named in this header — so following the plan literally
   gave you nothing to edit. If #94 has merged, it is on `main` and this is an
   ordinary edit; if not, make the change on that branch. Its `toCandidates()`
   already
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

### 4.3 Phase 1 decisions worth keeping visible

- **Anonymous reports never suppress.** They keep feeding
  `has_negative_report` for 24 hours in their cell, as before; hiding a
  vehicle from every rider needs an account behind it (risk 2).
- **Suppression uses the accountable hold rule** — not moved since, charge not
  risen, unresolved — for every report type **except `improperly_parked`**
  (owner's override of §2.2(2) and §5, 2026-10-09: a parking report is a
  report to Veo, not a reason to steer riders away).
- **`suppressed_reason` priority:** `inaccessible`, `not_found`,
  `not_rideable`, `damaged`, `dead_battery`.
- **Neither an `inaccessible` nor a `not_found` report's location is
  published**: the public monthly CSV blanks both.
- **The charge rise that clears a report is 5% of a full charge** (~2.3 km),
  far above the feed's parked drift and far below any swap. Reports filed
  before sql/100 have no recorded charge and so clear only on movement — the
  §2.4 fix applying to them too.
- **`inaccessible` earns no points**; paying for a suppressing report pays
  for griefing.
- **Resolving a report is final** (no un-resolve) and attributed to the
  admin's account; the `/h3` aggregate honours it only from later cycles.
- **Acknowledging gone keeps a row forever**: withdrawing sets `not_gone`
  and records who withdrew it.

### 4.4 Phase 1b: condition checks (IMPLEMENTED — see §4.5 for what was decided)

*Owner, 2026-10-09.* A sticky report needs a way to be cleared by riders, not
only by movement or an admin. Feature confirmation is where a rider is
already standing at the scooter answering questions, so condition checks
ride along with it.

**The flow (frontend in Phase 3).**

1. After a rider confirms features, ask: *"Would you like to confirm
   condition (ride-ability) as well? (requires starting scooter)"*.
2. If yes, list the scooter's **standing negative rideability reports** with
   their observed dates. "Negative rideability" = unresolved `not_rideable`
   (with its reason, if any), `dead_battery`, `damaged` and `inaccessible`
   reports that still hold under the accountable rule (§2.2/§2.4: not moved,
   charge not risen) — exactly `open_reports` on identify, minus `not_found`,
   which a rider standing at the scooter has already answered (it is here).
   `improperly_parked` never appears there: it does not suppress. For each:
   *"Still a problem? Y/N"*.
3. Then: *"Did you do a test ride? Y/N"*.
   - **No** → every condition answer is discarded. Nothing is stored against
     the reports and no points are awarded.
   - **Yes** → each "no longer a problem" **resolves** that report — the same
     resolution columns as an admin void (`resolved_at`, `resolved_by`,
     `resolution`), attributed to a *rider check* rather than an admin (a
     `resolution_source` of `rider_check` vs `admin`, or a separate
     `condition_checks` table the resolution points at; the implementer
     chooses, but the audit must say which). Each "still a problem"
     **reconfirms** it: a reconfirmation row (who, when, observed) that the
     dossier shows and the export can count, without restarting the report's
     clock — persistence is already "until it moves".

**The map flag.** `needs_condition_check` on `/devices/current`: `true` for a
vehicle with at least one standing negative rideability report (as defined
above), so the map can invite riders to check it. It is an invitation, not a
verdict, and is independent of both `reliability_tier` and `suppressed` —
though in practice most `needs_condition_check` vehicles are suppressed, which
is why the identify modal (§2.7) is where the invitation is most useful.

**Points (owner's rule, via the existing `points.py` ledger).** At most **50**
per check:

- **10** for completing the condition form with "Did you do a test ride?" =
  **Yes**, whatever the condition answers are;
- **+40** when the feed **confirms** that test ride: the vehicle shows a
  reservation episode or movement within a short window after the check
  (suggest 15 minutes, reusing the reservation-episode and movement signals
  `device_state.py` already derives);
- test ride = **No**: answers discarded, **no points**.

Anti-abuse, the implementer's choice but documented: one award per vehicle
per account per 24 hours; a daily cap per account (suggest 10 checks); the
+40 only when the confirming episode starts after the check was submitted;
and the 10 only when the account has not already resolved that vehicle's
reports in the window.

**Interactions to get right.**

- **Persistence (§2.2).** A rider resolution is a resolution: the report
  stops counting immediately, exactly like an admin void. Reports filed
  *after* the check are new evidence and stand on their own.
- **Suppression (§2.5).** Resolving the last standing report unsuppresses the
  vehicle on the next request. A false "no longer a problem" is therefore a
  way to un-hide a vehicle — the mirror image of griefing — which is why it
  requires a test ride, and why the +40 depends on the feed agreeing.
- **Griefing controls (§2.6(2)).** The Phase 2 reporter view must show rider
  resolutions alongside reports, per account: an account that resolves
  reports nobody else's rides corroborate is the same signal as an account
  that files them. An admin can reinstate a rider-resolved report (the one
  case where un-resolving is allowed, because the resolution was not an
  admin's judgement).

### 4.5 Phases 1b + 2 as built (2026-10-09) — the decisions

**Phase 1b — condition checks** (`src/condition_checks.py`,
`src/api_condition_checks.py`, `sql/102`).

- **Endpoints.** `GET /api/v1/devices/{vid}/conditions` and
  `POST /api/v1/devices/{vid}/condition-checks`, signed in, per-account
  limits 60/h and 20/h (a refused POST still spends quota). **Not** folded
  into the feature POST: that one accepts anonymous reports, dedupes on its
  own answer shape and is graded later; a check must be signed in, acts at
  once and needs the GET's list first. The two are linked instead — the
  feature POST's `id` is accepted as `feature_report_id`.
- **"Negative rideability"** = standing `inaccessible`, `not_rideable` (with
  reason), `damaged`, `dead_battery` (`fleet_reports.CONDITION_CHECK_TYPES` =
  the suppressing set minus `not_found`). A standing **`not_found`** is never
  asked; a test-ridden check resolves it automatically (outcome `found`) —
  the rider found and rode it. `improperly_parked` never appears.
- **Proof of presence is required** (added: an un-hiding resolution is the
  mirror of griefing): a plate-valid feature confirmation by the same
  account on the same vehicle under an hour old, the plate, or the QR.
- **Every listed condition must be answered** on a test-ridden check
  (`422 unanswered`); an answer for a report that stopped standing since the
  GET is recorded `stale` and changes nothing. No standing condition →
  `409 nothing_to_check` (stops "checks" on clean scooters for points).
- **Test ride = No**: all answers discarded; one audit row
  (`device_condition_checks.test_ride = false`) and nothing else.
- **Test ride = Yes**: "no longer a problem" resolves through
  `fleet_reports.resolve_report` — the one write path every resolver uses —
  with `resolution_source = 'rider_check'`, the rider's account and
  `resolved_by_check_id`; admin resolutions are `'admin'`. "Still a problem"
  stamps `last_reconfirmed_at` / `reconfirm_count` and writes an answer row.
  **A reconfirmation does not restart the hold** (§4.4 as written): a test
  ride that moves the scooter past the stationary threshold clears all its
  reports by the movement rule, reconfirmed or not. *Open question for the
  owner — see the PR.*
- **Points**: 10 (`condition_check`) on a test-ridden check; +40
  (`condition_check_confirmed`) when the feed confirms; max 50. Withheld
  (reason stored on the check) for **`own_reports_only`** (every report the
  check acted on was the rider's own — the check still applies, it just pays
  nothing, so "report it, then check it" is no loop), **`cooldown`** (one
  paid check per vehicle per account per **24 h**), **`daily_cap`** (**10**
  paid checks per account per rolling 24 h), `no_location`. The +40 only
  follows a paid 10.
- **Feed confirmation window: 20 minutes either side of the submission**,
  checked every ingest cycle after `device_state` is written
  (`condition_checks.confirm_pending_checks`). Signals: a rental episode
  (`device_state.rental_started_at`) that started in the window; a move
  (`first_observed_at_location` advanced past its value at check time) in
  the window; or, seen at submission, a rental or a move in the 20 minutes
  before. Either side, not only after, because "Did you do a test ride?" is
  past tense — the ride normally starts before the form is sent. None by
  20 minutes after → `unconfirmed`.
- **`needs_condition_check`** on `/devices/current`, from the same single
  pass as `suppressed` (`fleet_reports.suppressions_and_condition_checks`);
  `null` when that query fails.
- **Reinstatement**: an admin may reinstate a rider-resolved report from
  `/admin/fleet` (stamped `reinstated_at/_by_login/reinstate_reason`); an
  admin's resolution stays final. Points already paid are not clawed back.

**Phase 2 — the admin centre** (`src/api_admin.py` `/admin/fleet/*`,
`src/admin_watch.py`, templates `fleet_*.html`).

- **Auth** is the existing GitHub-OAuth `auth.require_admin`; every POST
  passes `_csrf_ok`. That session has no rider account, so page writes are
  attributed to the **GitHub login** (`device_reports.resolved_by_login`,
  `device_census_ack.*_by_login`, sql/102) while the `/api/v1/private`
  routes keep attributing to the account. Pages call Phase 1's functions
  (dossier, census, export, the census writes refactored into shared
  helpers), so page and JSON cannot disagree.
- **Reports queue** filters type, reason, neighbourhood and standing (and
  open/resolved), 50 a page. The region is a point-in-polygon on the
  report's own point, else its cell, else the vehicle's position, applied
  in Python over the newest 5,000 matching rows. "Dedupe status" = other
  reports of the same type on the vehicle within ±30 min, plus how many
  accounts stand behind its standing reports.
- **Dossier**: reports with provenance, condition checks with answers,
  feature consensus and broken parts, census ack + note, stops with idle
  time, hourly battery, SMS watches, and **"repeatedly hidden at the same
  spot"**: all the vehicle's stops grouped within 50 m; a spot whose
  suppressing reports fell in two or more separate stops is flagged.
  Positions shown rounded to ~100 m.
- **Reporter view**: per account (id + public username, never email) —
  volume, vehicles, cells, days and hours active, counts by type, admin
  voids, rider resolutions, and that account's condition checks (no-ride
  checks, resolutions, feed confirmed / not). Detail: hour-of-day and res-8
  cell spread.
- **SMS watch**: the texts go to the **verified phone of an
  admin-allowlisted account** named by email, with a consent tick, a
  confirmation text first, STOP honoured (comms 409 and the
  `sms_opted_out_at` mirror both end the watch), at most **20 texts** per
  watch (`cap_reached`), expiry ≤ **7 days** (default 24 h), at most 10
  live watches per account; one text per cycle summarising every change
  (feed leave/rejoin, rental start/end, disabled, a > 50 m non-rental move).
- **Census** and **export** pages render the Phase 1 data; CSV via
  `/admin/fleet/export.csv`.

---

## 5. Tests

- A **signed-in** report with no movement is still live a week later. This
  already passes; write it anyway, because §2.2's rule is now load-bearing for
  the rest of the plan and nothing currently pins it.
- An **anonymous** report still ages out at 24 hours. This is the guard against
  the change §4.1(3) was originally going to make, and the only test that fails
  if somebody unclocks those branches.
- A `improperly_parked` report does not touch `has_negative_report` at all (it
  is excluded by type) ~~, and persists only through §2.5's suppression
  flag~~ — and, per the owner's 2026-10-09 override of §2.2(2), does not
  suppress either; it still counts in the export and the dossier.
- An `inaccessible` report does **not** change `reliability_tier`, and **does**
  set `suppressed`.
- ~~An `improperly_parked` report likewise suppresses without touching the
  tier.~~ **Overridden by the owner, 2026-10-09:** an `improperly_parked`
  report neither suppresses nor touches the tier. A `not_found` report does
  suppress.
- **A device at 100% battery can be reported and the report stands.** This
  FAILS TODAY — the shipped full-charge clause clears it immediately
  (`api_public.py:521-523`). It is the one test in this plan with a live bug to
  catch, written against the scooter that prompted it.
- A report whose recorded charge is NULL clears nothing, exactly as the current
  NULL branch behaves.
- Voiding a report requires the resolution columns of §4.1(3b); a test that
  passes without them means `suppressed` is computing from something else.
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
- ~~The resolve endpoint refuses an unauthenticated caller, and rate-limits a
  scripted one at the same ceiling as `qr-scan`.~~ **Superseded** by the
  owner's 2026-10-08 decision (§4.1(6)): the test is that identify misses are
  charged to the same 30/min per-IP bucket as the plain plate lookup, and
  that the response never echoes the plate.
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
| 5 | **The resolve endpoint becomes a plate-enumeration oracle.** It hands out the plate → `vehicle_identifier` mapping the salt exists to withhold. | *Superseded by the owner, 2026-10-08 (§4.1(6)).* The mitigation was `require_session` + an account bucket; the owner chose a public lookup at 30/min per IP, never echoing the plate, 404 on none or ambiguous — and one plate oracle with one policy, which is why identify extends `/vehicles/resolve` rather than adding a second endpoint. `last_seen` on a missing vehicle is rounded to ~100 m. |
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
