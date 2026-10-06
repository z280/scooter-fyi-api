# Equity receipt review: plan

**Goal.** A rider who thinks a ride in an Equity Area was charged the wrong rate can submit the receipt for review. We collect enough to check the rate, try to match the claim to a ride we observed, reward useful submissions, and decline to keep anything that cannot show a rate error.

Owner's spec (2026-10-06): trip minutes (duration), cost before tax, cost with tax, start date/time, end date/time, start and end points picked on the map, and a scooter ID entered by hand or from the QR code. The backend tries to align the claim with a trip. If none of the useful data is present, thank the rider and retain nothing. Points:

- **10** for a submission whose subtotal and total match its own screenshot.
- **100** for one corroborated by a ride in our history: same scooter ID, and start and end points each within 100 m.

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
- start and end points: "Pick on map" for each, using map-pick, pre-filled from the tapped spot;
- scooter ID: typed, or "Scan QR" (reuses qr-scan);
- receipt screenshot.

If the rider is signed out, the form shows "Sign in to send a receipt" (the endpoint requires a session, so the evidence has provenance).

**API:** extend `discount_reports` in a migration that supersedes #105:

- `trip_minutes`, `subtotal_cents`, `total_cents`, `ride_started_at`;
- `start_lat`/`start_lng` (`end_*` already exists);
- `vehicle_ref` (as entered) and `vehicle_identifier` (resolved HMAC, when the ref resolves);
- `region_name`, `zone_version = 'equity'`;
- `review_status`, `match_status`, `matched_trip_event_id`;
- `expected_cents`, `rate_error_cents`.

**The "useful data" gate (server-side, before anything is stored).** A rate error can only be shown with:

- (a) trip minutes, plus
- (b) a pre-tax or with-tax cost, plus
- (c) a start or end point inside an Equity Area.

Without all three, the API returns `422 not_rate_checkable`, keeps no row and no image (the upload is rejected before the R2 PUT), and the form says: *"Thanks for taking part. We can't check a rate from this, so we haven't kept it."*

**Arithmetic, computed and stored at submission:**

- expected equity subtotal = $1 + $0.13 × minutes;
- the tax check total ≈ subtotal × (1 + tax rate), within rounding;
- `rate_error_cents` = charged subtotal − expected (or derived from the total).

**Privacy.**
- Start and end points are what the rider picks, not their GPS. The public CSV currently exports exact `end_lat/lng`; Phase 1 rounds exported points to 3 decimals (about 100 m).
- The privacy payload and policy list the new fields in the same change.

## Phase 2: ride matching (backend)

- **Candidates:** releases in `trip_events` ending within ±10 min of `ride_ended_at` whose `to` point is within 100 m of the end point. Each is paired with its rental start (`device_history.departed_at` of the origin stop) within ±10 min of `ride_started_at`, with a `from` point within 100 m of the start point. If a scooter ID was given, it must match `vehicle_identifier`.
- **`match_status`:**
  - `corroborated`: scooter ID given and matches, and both points within 100 m;
  - `plausible`: exactly one candidate, but no scooter ID or only one point;
  - `ambiguous`;
  - `none`.
- **Where it runs:** a scheduled job (`match_discount_reports`), not the request path. It's a time-boxed indexed query, but it shouldn't make the rider wait.
- **Known limit:** 2-minute snapshots put rental start and end times at ±2 min. Veo can also rotate the bike_id at release; matching uses `vehicle_identifier`, which survives rotation.

## Phase 3: automated analysis + points (the API decides)

Owner's direction (2026-10-06): the API does the analysis and settles points itself. No human is needed in the normal path.

**Extract.** Read the screenshot with two independent readers:

- **(A) a vision LLM** (Claude Haiku 4.5, structured output). It returns minutes, unlock fee, per-minute rate, subtotal, tax, total, start and end times, and the vehicle ID if shown.
- **(B) local OCR** on ovh3 (PaddleOCR or Tesseract) plus a Veo-layout parser.

A works from day one and survives layout changes. B keeps a second opinion that never leaves the server. **Needs the owner's OK:** A makes Anthropic a processor of the receipt image, so the privacy payload and policy say so in the same change.

**Cross-check, recorded per check in `analysis_checks` (JSONB):**

1. Image fields against what the rider typed (minutes, subtotal, total, times).
2. Reader A against reader B.
3. Arithmetic: unlock + rate × minutes = subtotal (±1¢), and subtotal + tax = total (±1¢, tax rate from `/meta/pricing`).
4. Times against the matched ride (Phase 2), when there is one.

**Decide (`analysis_status`):**

- `verified`: checks 1–3 pass and the readers agree → **+10**, settled immediately. If `match_status = corroborated` → **+100** instead.
- `uncertain`: a reader is missing or they disagree on a field → points stay `pending`, and the report goes to the human portal (Phase 3b).
- `rejected`: the image contradicts the typed numbers → no points, and the rider is told which field did not match.

**Shadow mode first.** For the first N reports (default 30), the API decides and records but keeps points `pending`, so the owner can compare its calls with their own before letting it settle.

**Abuse limits:**

- 20 submissions per account per day (the existing limit).
- One award per matched ride.
- 10-point awards capped at 3 per day.
- The 100-point tier needs a ride we independently observed, so a doctored image cannot earn it.

## Phase 3b: human portal (fallback + labels)

`/admin/discount-reports` shows the screenshot beside the typed and extracted fields and each check, with approve / reject / correct-a-field.

- It handles `uncertain` reports and shadow-mode spot checks.
- Every decision is stored as a label (`human_label` JSONB: the correct field values and verdict).

## Phase 4: get better over time (OCR / vision long-term)

- **Evaluation set:** human-labelled receipts become a gold set. Each reader and the overall decision get scored on it: field accuracy, false-verify rate. Shadow mode ends when the false-verify rate on the gold set is about 0.
- **Prompt and parser iteration** against the gold set comes first. This is cheap and usually enough for one app's receipt layout.
- **Training (only if volume justifies it):** with a few hundred labelled receipts, fine-tune a document model (e.g. Donut or LayoutLM) to run locally on ovh3. That removes the third-party processor and per-call cost. Not worth it before then, given that 0 receipts have ever been filed.
- **Needed now:** a few real Veo receipt screenshots, equity and standard, to build reader B's parser and the first test cases.

## Phase 5: reporting

- Confirmed rate errors are aggregated by Equity Area in the public summary and monthly CSV (no identity).
- The rider gets an outcome notification on their account: reviewed / corroborated / points.

## Defaults chosen (say if any should change)

1. **Gate:** minutes + a cost + an Equity Area point. Anything less is declined and not retained.
2. **Points:** 10 *or* 100 per submission, not 110.
3. **Matching tolerance:** ±10 min on times, 100 m on points (your spec).
4. **Points settle automatically** when the API verifies a receipt (Phase 3, owner's direction). Only `uncertain` reports wait for a human, after a short shadow-mode start.
