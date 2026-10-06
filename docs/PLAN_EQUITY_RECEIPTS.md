# Equity receipt review: plan

**Goal.** A rider who thinks a ride in an Equity Area was charged the wrong rate can submit the receipt for review. We collect enough to check the rate, try to match the claim to a ride we observed, reward useful submissions, and decline to keep anything that cannot show a rate error.

Owner's spec (2026-10-06): trip minutes (duration), cost before tax, cost with tax, start date/time, end date/time, start and end points picked on the map, and a scooter ID entered by hand or from the QR code. The backend tries to align the claim with a trip. If none of the useful data is present, thank the rider and retain nothing. Points:

Owner, 2026-10-06. One award per receipt (the highest tier it reaches), once per matched ride:

| Tier | Condition | Points |
|---|---|---|
| `valid_matched` | Valid receipt (numbers match its screenshot), correlated to a feed ride, ride **not** in an Equity Area | **10** |
| `valid_equity` | Same, ride starts or ends in an Equity Area, **no** price discrepancy (charged correctly) | **20** |
| `proven` | Feed-backed, proven failure to charge the appropriate rate, with readable plan evidence | **50** |

100 was too much next to the existing values (dibs stand-down 300/50, QR scan 100, referral 100). A receipt that is declined (not rate-checkable) or not matched earns nothing.
- Original wording of the proven tier, kept for the record: corroborated by a ride in our history. Owner's original wording: same scooter ID, start and end points within 100 m. Since receipts carry no location, this becomes: the same scooter observed in the feed starting and ending at the receipt's times (±4 min), with the observed points supplying the location.

## The constraint that shapes everything (owner, 2026-10-06)

**A Veo receipt has no geographic information.** It does show the **scooter code**, the start and end times, and the costs. So a claim is located by **matching the scooter code and the receipt's times against our feed history**. The ride's start and end points come from what we observed in the feed, not from the rider.

- **Scooter code = the plate.** The owner confirmed (2026-10-06) that the receipt shows the raw plate number, a 7-digit `101…`/`102…`/`103…` code: the same number Veo publishes in `rental_uris … &number=<plate>`. Live fleet: 102 (6,072), 103 (2,687), 101 (839). `trip_events.vehicle_plate` and `device_history.vehicle_plate` are populated on 100% of rows (7-day check: 480k and 494k), so **matching uses the plate directly, with no hashing**. The HMAC `vehicle_identifier` is still computed and stored, because the public CSV must carry that, never the raw plate. Raw plates are admin-only on our public API, even though Veo publishes them.
- **History is deep enough:** `device_history` departures since 2026-05-31 and `trip_events` since 2026-07-05, with no pruning. A receipt from weeks ago is still matchable.
- **Precision:** the feed is sampled every 2 minutes, so observed rental start and end are known to about ±2 min.

## What real receipts look like (owner's samples, 2026-10-06)

Six receipts and one Trip summary, from the Veo app:

**Receipt view:**
- `Ride #<plate> (<N> min)` with the Charge;
- a Discount line, sometimes itemised, e.g. "veoplus Premium";
- Subtotal, then "Taxes & Fees" (older receipts say "Tax");
- Total paid, Charge date (**date only**), Payment method.

There is **no time of day and no location.**

**Trip summary view:** total paid, savings, distance, minutes and **start date and time**, but **no plate.**

The prices follow one pattern:

- Charge = $1 + $0.39/min (every sample).
- After Veo's discount: $1 + $0.25/min (Sep 2026), never the equity $1 + $0.13/min.
- Taxes & Fees: every sample is exactly the legislated 9.15% **rounded up to the next cent** (see "Tax" above). It is not a different rate.

Matched against feed history by plate + date + duration (±1 min):

Charged = the subtotal after Veo's discount line. On Sep 27/29 that is the plain Resident rate, $1 + 25¢; the stacking ended after 2026-09-10. On May 26 it is the stacked $0 + 25¢.

| Receipt | Match | Equity | Charged | Contract equity price |
|---|---|---|---|---|
| #1018354, 16 min, Sep 29 | unique, 15:16→15:32 | ends EQ_003 | $5.00 | $3.08 |
| #1025640, 21 min, Sep 29 | unique, 16:00→16:20 | ends EQ_003 | $6.25 | $3.73 |
| #1021645, 4 min, Sep 29 | unique, 16:34→16:38 | starts EQ_003 | $2.00 | $1.52 |
| #1027102, 14 min, Sep 27 | **ambiguous**: two 14-min rides that day | neither | $4.50 | n/a |
| #1025894, 14 min, May 26 | before feed history (starts 2026-05-31) | — | $3.50 | — |
| Trip summary, Sep 5 01:52, 7 min, 1.52 mi | **28 candidates** fleet-wide (no plate) | — | — | — |

What follows for the design:

1. **Matching is plate + charge date + duration.** The feed resolved durations within one cycle (16→16, 21→20, 4→4).
2. **When more than one ride fits**, an optional "about what time did you start?" field resolves it, and so does a Trip summary screenshot, which carries the start time. The form offers both.
3. **The plate is mandatory.** Without it, a time and distance match 28 rides.
4. **Rides before 2026-05-31** are told plainly that they are before our history.
5. **The gate** becomes plate + minutes + subtotal/total, all of which are on every receipt. Start and end times are optional, for disambiguation only.
6. **Gold set seed.** These six receipts are the first gold-set and bake-off cases, *if the owner agrees to keep them as test fixtures*.

## Rate plans: compare the receipt against the rider's own plan (owner, 2026-10-06)

The owner's Sep 29 rides were **deliberate tests**, stopping inside the Equity Area.

Separately, Veo told him that for a period he had been **stacking VeoPlus Premium with the Denver Resident Pass**, which was never supposed to be possible. That gave free unlocks + 25¢/min. Veo removed the stacking after **2026-09-10**. So the samples hold both signatures:

- **May 26 receipt (#1025894):** $3.50 for 14 min = **$0 + 25¢**, the stacked rate.
- **Sep 27/29 receipts:** **$1 + 25¢**, a single plan (Resident), after the stacking was removed. The Sep 29 Equity Area rides were therefore charged the plain Resident rate instead of $1 + 13¢: `equity_not_applied`, with no stacking caveat.

Two consequences:

1. **The app keeps the stacked plan selectable.** `resident_plus` ("free unlocks + 25¢/min") stays in `RATE_PLANS`. Whether anyone else gets a stacked rate is itself worth learning (the owner's "social experiment").
2. **A receipt is judged against the rider's own plan, not one fixed rate.** The form asks for the rate plan, pre-filled from the profile's `rate_plan` when signed in, with "Not sure" allowed. The analysis records three prices:

| Price | How it is computed |
|---|---|
| **Implied (rate signature)** | Solve the receipt's subtotal and minutes for unlock ∈ {$0, $1} and a per-minute rate, e.g. "$1 + 25¢". Stored as `rate_signature`, whatever the plan. |
| **Plan-expected** | Unlock + per-minute from the declared plan (`RATE_PLANS`; for Access, minutes past the daily free 60 cannot be known, so the result is "≤"). |
| **Contract-expected** | If the matched ride starts or ends in an Equity Area: the **lower** of the plan price and Exhibit C's $1 + 13¢/min, which applies whatever the tier (Exhibit A §5.2). Otherwise, the plan price. |

**`rate_finding`** (only `equity_not_applied`, and a `plan_mismatch` that is an *overcharge*, count as "proven" for +50, and only with a corroborated feed match plus readable plan evidence):

- `equity_not_applied`: the ride touched an Equity Area, the equity price was lower, and it was not charged. The Sep 29 rides land here: charged Resident $1 + 25¢, owed $1 + 13¢.
- `plan_mismatch`: charged differently from the declared plan, in either direction. A rider being *undercharged* (as with the stacking) is recorded too.
- `matches_plan`.
- `plan_unknown`: the rider was not sure. The signature is still recorded.

**Social experiment.** The public aggregate counts `rate_signature` × declared plan by month (no identity), so "who else pays $1 + 25¢ while on Premium?" can be answered from data.

**Matching uses the plan too, but only as a consistency check.** A receipt's Charge line is always the base price ($1 + 39¢/min in every sample), so the minutes are double-checked from the charge: minutes = (charge − $1) / $0.39. That catches a misread duration before it feeds the ride match.

## Tax: know the legislated rate, validate strictly (owner, 2026-10-06)

The question was whether Veo charges a tax that does not exist in law, and whether a rate change explains the May → September difference.

**The legislated rate.** Denver's combined sales tax is **9.15%**: Colorado 2.90% + Denver 5.15% + RTD 1.00% + SCFD 0.10%.

- In effect since **2025-01-01**, when Denver's own rate rose from 4.81% (ballot measure 2Q, Denver Health).
- Before that it was 8.81%.
- The 2026 rate is still 9.15% (Avalara, Quaderno and others; see sources).
- `src/api_meta.py` already serves 9.15%, with this itemisation.

**What the receipts show.** Solving each receipt's "Taxes & Fees" (or "Tax") against its subtotal:

| Rounding rule | Rates fitting all 5 receipts (May–Sep) | Does 9.15% fit? |
|---|---|---|
| Round half up (nearest cent) | 9.29% only, which is not a legislated rate | no |
| Round half to even | 9.29–9.30% | no |
| **Always round up (ceiling)** | **9.15–9.20%** | **yes, all five** |

- **No rate change between May and September.** All five receipts fit one rate.
- **They fit the legislated 9.15% exactly, if Veo rounds the tax up to the next cent every time.** For example, $2.00 × 9.15% = 18.3¢ is charged as 19¢.
- **4 of the 5 are 1¢ above nearest-cent rounding.** The $5.00 receipt (45.75¢ → 46¢) is the same either way.
- **A flat fee hidden in "Taxes & Fees" does not fit.** It would add the same amount every time, but the $5.00 case has nothing extra.

**The rounding rule (to verify against primary sources).** Colorado DOR guidance (March 2026, after the penny-production halt, and long-standing in GIL-09-016) is reported as: compute to three decimals and round **up only when the third digit is greater than four**, i.e. half up. Denver is a home-rule city that collects its own 5.15%, so its municipal code's rounding rule needs checking too. If both say half up, Veo's ceiling rounding over-collects up to 1¢ on most rides.

**Validation per receipt** (strict, recorded as `tax_finding`):

1. **Rate:** the legislated rate for the ride's date and jurisdiction, from a dated table (`tax_rates`: jurisdiction, components, effective from/to, source URL). Rides are sourced to Denver; a ride ending outside the city, e.g. Glendale, is flagged rather than guessed.
2. **Compare** the receipt's tax with subtotal × rate under each rule:
   - `tax_ok`: matches half up (the legal rule).
   - `tax_rounded_up`: matches ceiling but not half up. Over-collection of 1¢; the amount is recorded.
   - `tax_unexplained`: matches no legislated rate under any rule. The implied rate range and the excess are recorded, as a possible undisclosed tax or fee.
3. **The label** is recorded ("Taxes & Fees" vs "Tax"). Older receipts say "Tax".

**Reporting** (Phase 5) adds a tax section:

- counts and totals of `tax_rounded_up` cents;
- any `tax_unexplained` cases with their implied rates;
- the legislated-rate table with sources.

This is a separate finding from the Equity rate. It is addressed to the Colorado DOR and Denver Treasury rather than DOTI, and costs riders only cents each, but adds up across the fleet.

## What exists already

| Piece | Where | State |
|---|---|---|
| Missed-discount endpoint, private EXIF-stripped receipt storage (18-month retention), rate limit, public CSV | `POST /api/v1/reports/discount`, `src/receipts.py` | Live. **0 reports ever filed.** It accepts only the retired `v1`/`v2` zones; PR #105 (draft) adds `equity` and the area. |
| Pick-a-spot-on-the-map mode | frontend `map-pick.ts` | Live (destination picker) |
| QR scanner that resolves a scooter | frontend `qr-scan.ts`, `qr-ride-scan.ts` | Live |
| Points ledger with `pending` status and `source_table`/`source_id` | `user_points`, `src/points.py` | Live |
| Observed rentals: when a scooter left its spot, release time, from/to points, vehicle | `device_history.departed_at`, `trip_events` (about 2.3M rows per 30 days), 2-minute cycles | Live |
| Equity rate | $1 unlock + $0.13/min (contract Exhibit C); tax rate from `GET /api/v1/meta/pricing` | Live |

## Phase 1: capture (form + schema)

**Frontend: the Equity Area card**, opened from the chip or a triple-tap, gets a "Didn't get the discount?" form:

- trip minutes;
- cost before tax ($), cost with tax ($);
- start and end date/time (defaulting to now);
- **scooter code** as printed on the receipt: typed, or "Scan QR" (reuses qr-scan) if the rider is still at the scooter. Required.
- **approximate start and end pins on the map** (owner, 2026-10-06), placed with map-pick and optional. The receipt has no location, so these are the rider's own independent evidence: they break ties between candidate rides, and pins far (more than 300 m) from the observed points send the case to "Needs your help" rather than letting it be proven. The feed's observed points remain what establishes Equity Area eligibility.
- receipt screenshot;
- **plan evidence screenshot: REQUIRED for an Equity Area discrepancy** (owner, 2026-10-06). This is a screenshot from the Veo app showing the rider's active plan or pass (VeoPlus Premium, Resident Pass, Access Program, or none), ideally with its dates. Without it, the form will not submit an equity discrepancy.

  **Why it is required.** Veo's likely answer to an equity claim is "riders on VeoPlus Premium or a Denver Resident plan don't get the Equity Area rate". The contract says otherwise: Exhibit A §5.2 applies the Equity Area rate to any trip starting or ending in an Equity Area, whatever the tier. So every claim must carry proof of the plan the rider was on, and the reporting can show the equity rate missing *per plan*.

  The vision reader extracts the plan name and validity dates from it, the same way as the receipt. The image is stored with the receipt (private, 18-month retention).

If the rider is signed out, the form shows "Sign in to send a receipt" (the endpoint requires a session, so the evidence has provenance).

**API:** extend `discount_reports` in a migration that supersedes #105:

- `trip_minutes`, `subtotal_cents`, `total_cents`, `ride_started_at`;
- `vehicle_plate` (the code as entered, validated `^\d{7,10}$` after stripping spaces) and `vehicle_identifier` (HMAC of it, computed server-side; the only form that leaves the admin surface);
- `pin_start_lat/lng`, `pin_end_lat/lng` (the rider's approximate pins; rounded to 3 decimals anywhere public);
- `matched_start_lat/lng`, `matched_end_lat/lng`, filled from the feed match;
- `region_name`, `zone_version = 'equity'`;
- `review_status`, `match_status`, `matched_trip_event_id`;
- `expected_cents`, `rate_error_cents`.

**The "useful data" gate (server-side, before anything is stored).** A rate error can only be shown with:

- (a) the scooter code (plate),
- (b) trip minutes, and
- (c) a pre-tax or with-tax cost.

All three are on every Veo receipt. Start and end times are optional; the receipt has none, so they are used only to break ties.

Where the ride happened is then established by the feed match. Without all three, the API returns `422 not_rate_checkable`, keeps no row and no image (the upload is rejected before the R2 PUT), and the form says: *"Thanks for taking part. We can't check a rate from this, so we haven't kept it."*

**Arithmetic, computed and stored at submission:**

- expected equity subtotal = $1 + $0.13 × minutes;
- the tax check total ≈ subtotal × (1 + tax rate), within rounding;
- `rate_error_cents` = charged subtotal − expected (or derived from the total).

**Privacy.**
- Start and end points are what the rider picks, not their GPS. The public CSV currently exports exact `end_lat/lng`; Phase 1 rounds exported points to 3 decimals (about 100 m).
- The privacy payload and policy list the new fields in the same change.

## Phase 2: ride matching from feed history (backend)

- **Vehicle:** match on `vehicle_plate` = the receipt's code. If that plate has never been seen in the feed, the result is `match_status = unknown_vehicle`, which usually means a typo or a misread code.
- **Ride:** that plate's rentals on the charge date (Denver local) whose observed duration, from `device_history.departed_at` to `trip_events.detected_at`, is within ±2 min of the receipt's minutes. If the rider gave an approximate start time or a Trip summary, keep the candidates within ±10 min of it.
- **Where:** the matched ride's `from` and `to` points are the start and end. **Equity eligibility** is whether either point lies in an official Equity Area. A ride that started *and* ended outside every area is not owed the discount: the API records `not_equity_ride` and the rider is told why.
- **`match_status`:**
  - `corroborated`: exactly one ride, both times within tolerance;
  - `partial`: one time matches, e.g. the vehicle was absent from the feed for part of the ride;
  - `ambiguous`: more than one candidate;
  - `none`;
  - `unknown_vehicle`.
- **Where it runs: in the request, in real time** (measured 2026-10-06: the plate + date match query takes 36–54 ms warm). If the ride is too recent to be in the feed, the report is stored as `match_status = waiting`, and a per-cycle job (`match_discount_reports`) retries it after every ingest until it resolves or 30 min pass.
- **Feed freshness:** cycles run every 120 s (p95 240 s), so a finished ride is matchable **2–6 min after it ends.**
- **Rider pins:**
  - **Tie-break:** among candidates, keep the ride whose observed from/to are nearest the pins.
  - **Consistency:** if pins were given, both must lie within 300 m of the observed points for the match to count as corroborated. Pins within 100 m are recorded as "strong" in the evidence packet.

### UX: a "My receipts" queue, not a live wait (owner, 2026-10-06)

With the feed 2–6 minutes behind, nobody will sit and watch a spinner. Submitting is fire-and-forget:

- The form confirms "Received. We'll check it against the feed and show the result in **My receipts**."
- If the ride is already in the feed, the match has usually finished by the time the rider opens the list.
- **My receipts** is a new tab in the account drawer (user menu), backed by `GET /api/v1/reports/discount` (the rider's own reports). Each row shows the plate, date and minutes, a status, and the points:

| Status | Meaning |
|---|---|
| Checking | Queued, or waiting for the ride to appear in the feed. It re-matches every cycle for up to 30 min, then once an hour for a day. |
| Needs your help | More than one ride fits and the pins did not settle it: "Did it start around 2:40 PM or 6:18 PM?" (start times only). |
| Proven rate error, +50 | Feed-backed, and the charged rate is not the one owed. |
| Equity Area ride, charged correctly, +20 | Matched, in an Equity Area, no discrepancy. |
| Valid, +10 | Matched to a feed ride outside any Equity Area. |
| Not matched | With the reason: plate never seen, no ride of that length that day, or before feed history. |
| Rejected | The image contradicts the typed numbers, or the plan evidence is missing or unreadable. |

- An unread badge on the user-menu entry when a status changes.
- An optional SMS/email via z280-comms when a report is proven. This is opt-in and follows the existing contact preferences.

## Phase 3: automated analysis + points (the API decides)

Owner's direction (2026-10-06): the API does the analysis and settles points itself. No human is needed in the normal path.

**Extract.** Read the screenshot with two independent readers:

- **(A) a vision LLM via OpenRouter** (owner's decision, 2026-10-06): a dedicated OpenRouter key for this project only, and the **cheapest model that passes the gold set**. It returns minutes, unlock fee, per-minute rate, subtotal, tax, total, start and end times, and the vehicle ID if shown, using structured output (JSON schema).
  - **Bake-off candidates** (OpenRouter list prices 2026-10-06, $/M tokens in/out): `qwen/qwen3.7-flash` (0.03/0.13), `google/gemma-3-12b-it` (0.05/0.15), `google/gemini-2.5-flash-lite` (0.05/0.20 batch), `openai/gpt-5-nano` (0.05/0.40). At about 1.5k tokens in and 200 out, each costs well under $0.001 per receipt.
  - **Choosing:** run every candidate on every gold receipt and pick the cheapest with perfect money-field accuracy. Re-run the bake-off when the gold set grows or a model is retired.
  - **Escalation:** when the cheap model and local OCR disagree, ask one stronger model once before falling back to `uncertain`.
  - **Privacy:** every request sets OpenRouter provider preferences `data_collection: "deny"`, and zero-data-retention routing where the chosen model supports it. The privacy payload and policy name OpenRouter and the routed provider as processors of the receipt image.
  - **The key:** `OPENROUTER_RECEIPTS_API_KEY` in the API's environment, written by a script the owner runs (never printed or pasted into a chat). With the key absent, Phase 3 runs OCR-only and anything that needs the model is `uncertain`.
- **(B) local OCR** on ovh3 (PaddleOCR or Tesseract) plus a Veo-layout parser.

A works from day one and survives layout changes. B keeps a second opinion that never leaves the server.

**Cross-check, recorded per check in `analysis_checks` (JSONB):**

1. Image fields against what the rider typed (minutes, subtotal, total, times).
2. Reader A against reader B.
3. Arithmetic: unlock + rate × minutes = subtotal (±1¢), and subtotal + tax = total (±1¢, tax rate from `/meta/pricing`).
4. Times against the matched ride (Phase 2), when there is one.

**Decide (`analysis_status`):**

- `verified`: checks 1–3 pass and the readers agree. With a corroborated feed match, the award is settled immediately at the highest tier reached:
  - **+10**: outside any Equity Area;
  - **+20**: in an Equity Area, charged correctly;
  - **+50**: **proven**, i.e. `rate_finding` is `equity_not_applied` or an overcharging `plan_mismatch`, and the plan evidence is readable.
- `uncertain`: a reader is missing or they disagree on a field → points stay `pending`, and the report goes to the human portal (Phase 3b).
- `rejected`: the image contradicts the typed numbers → no points, and the rider is told which field did not match.

**Shadow mode first.** For the first N reports (default 30), the API decides and records but keeps points `pending`, so the owner can compare its calls with their own before letting it settle.

**Abuse limits:**

- 20 submissions per account per day (the existing limit).
- One award per matched ride.
- 10-point awards capped at 3 per day.
- The 50-point tier needs a ride we independently observed, so a doctored image cannot earn it.

## Phase 3b: human portal (fallback + labels)

`/admin/discount-reports` shows the screenshot beside the typed and extracted fields and each check, with approve / reject / correct-a-field.

- It handles `uncertain` reports and shadow-mode spot checks.
- Every decision is stored as a label (`human_label` JSONB: the correct field values and verdict).

## Phase 4: get better over time (OCR / vision long-term)

- **Evaluation set:** human-labelled receipts become a gold set. Each reader and the overall decision get scored on it: field accuracy, false-verify rate. Shadow mode ends when the false-verify rate on the gold set is about 0.
- **Prompt and parser iteration** against the gold set comes first. This is cheap and usually enough for one app's receipt layout.
- **Training (only if volume justifies it):** with a few hundred labelled receipts, fine-tune a document model (e.g. Donut or LayoutLM) to run locally on ovh3. That removes the third-party processor and per-call cost. Not worth it before then, given that 0 receipts have ever been filed.
- **Needed now:** a few real Veo receipt screenshots, equity and standard, to build reader B's parser and the first test cases.

## Phase 5: reporting (owner, 2026-10-06: required)

1. **Evidence packets** (admin, for DOTI or Veo). A filterable report of proven cases. Each case carries:
   - the ride: date and time, plate, duration, and the observed start and end with the Equity Area named;
   - the charged subtotal, against the plan price and the contract price;
   - **the rider's plan, with its plan-evidence screenshot**;
   - the receipt screenshot and the match details (feed cycle ids).

   Exportable as a PDF or ZIP (images plus CSV) for one case, a date range or one area. Rider identity is never included; an opaque case id stands in for it.
2. **Rebuttal-ready summary.** Proven `equity_not_applied` broken down by declared plan (none / Resident / VeoPlus / Premium / Access), each row backed by its plan-evidence images, with the contract clause quoted (Exhibit A §5.2, Exhibit C "Equity Area Pricing"). If Veo says premium or resident riders are excluded, the answer is already a table.
3. **Public aggregates** in the existing summary and monthly CSV: counts and overcharge totals by area, plan and month, plus the rate-signature × plan table (the "does anyone else get stacked rates?" experiment). No identity, and points rounded to about 100 m.
4. **The rider's own view.** In My receipts, each proven case shows the finding in plain words: what they paid, what the contract says they owed, and why.

## Defaults chosen (say if any should change)

1. **Gate:** scooter code + start and end times + a cost. Anything less is declined and not retained. Equity eligibility comes from the feed match.
2. **Points:** 10 valid and matched; 20 matched in an Equity Area with no discrepancy; 50 proven. Highest tier only, one award per ride.
3. **Matching:** scooter code plus the receipt's start and end times, ±4 min against feed history. The location comes from the feed, because the receipt has none.
4. **Points settle automatically** when the API verifies a receipt (Phase 3, owner's direction). Only `uncertain` reports wait for a human, after a short shadow-mode start.

## Sources (tax)

- Avalara, Denver 2026 combined rate: https://www.avalara.com/us/en/taxrates/state-rates/colorado/cities/denver.html
- Quaderno, Denver sales tax 2026: https://quaderno.io/guides/denver/sales-tax/
- Colorado DOR rounding guidance, as reported: https://news.bloombergtax.com/daily-tax-report/colorado-dor-issues-guidance-on-rounding-sales-tax-post-penny-production-halt and https://www.salestaxinstitute.com/resources/colorado-announces-rounding-change-sales-tax-reporting
- Colorado DOR GIL-09-016: https://tax.colorado.gov/sites/tax/files/documents/GIL-09-016.pdf (primary; blocked automated fetch, so verify by hand)
