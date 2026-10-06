# Equity receipt review: plan

**Goal.** A rider who thinks a ride in an Equity Area was charged the wrong rate can submit the receipt for review. We collect enough to check the rate, try to match the claim to a ride we observed, reward useful submissions, and decline to keep anything that cannot show a rate error.

Owner's spec (2026-10-06): trip minutes (duration), cost before tax, cost with tax, start date/time, end date/time, start and end points picked on the map, and a scooter ID entered by hand or from the QR code. The backend tries to align the claim with a trip. If none of the useful data is present, thank the rider and retain nothing. Points:

- **10** for a submission whose subtotal and total match its own screenshot.
- **100** for one corroborated by a ride in our history. Owner's original wording: same scooter ID, start and end points within 100 m. Since receipts carry no location, this becomes: the same scooter observed in the feed starting and ending at the receipt's times (±4 min), with the observed points supplying the location.

## The constraint that shapes everything (owner, 2026-10-06)

**A Veo receipt has no geographic information.** It does show the **scooter code**, the start and end times, and the costs. So a claim is located by **matching the scooter code and the receipt's times against our feed history**. The ride's start and end points come from what we observed in the feed, not from the rider.

- **Scooter code → vehicle.** Veo publishes each scooter's number in its public `free_bike_status` feed (`rental_uris … &number=<plate>`), and we already derive `vehicle_identifier = HMAC(plate)` from it (`src/vehicle_identity.py`). *Assumption to confirm with a real receipt:* the receipt's scooter code is that same number.
- **History is deep enough:** `device_history` departures since 2026-05-31 and `trip_events` since 2026-07-05, with no pruning. A receipt from weeks ago is still matchable.
- **Precision:** the feed is sampled every 2 minutes, so observed rental start and end are known to about ±2 min.

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
- *No location fields.* The receipt has none, and the ride's points come from the feed match (Phase 2).
- receipt screenshot.

If the rider is signed out, the form shows "Sign in to send a receipt" (the endpoint requires a session, so the evidence has provenance).

**API:** extend `discount_reports` in a migration that supersedes #105:

- `trip_minutes`, `subtotal_cents`, `total_cents`, `ride_started_at`;
- `vehicle_ref` (the scooter code as entered) and `vehicle_identifier` (HMAC of it, computed server-side);
- `matched_start_lat/lng`, `matched_end_lat/lng`, filled from the feed match, not from the rider;
- `region_name`, `zone_version = 'equity'`;
- `review_status`, `match_status`, `matched_trip_event_id`;
- `expected_cents`, `rate_error_cents`.

**The "useful data" gate (server-side, before anything is stored).** A rate error can only be shown with:

- (a) a scooter code,
- (b) start and end times (trip minutes can be derived from them), and
- (c) a pre-tax or with-tax cost.

Where the ride happened is then established by the feed match. Without all three, the API returns `422 not_rate_checkable`, keeps no row and no image (the upload is rejected before the R2 PUT), and the form says: *"Thanks for taking part. We can't check a rate from this, so we haven't kept it."*

**Arithmetic, computed and stored at submission:**

- expected equity subtotal = $1 + $0.13 × minutes;
- the tax check total ≈ subtotal × (1 + tax rate), within rounding;
- `rate_error_cents` = charged subtotal − expected (or derived from the total).

**Privacy.**
- Start and end points are what the rider picks, not their GPS. The public CSV currently exports exact `end_lat/lng`; Phase 1 rounds exported points to 3 decimals (about 100 m).
- The privacy payload and policy list the new fields in the same change.

## Phase 2: ride matching from feed history (backend)

- **Vehicle:** `vehicle_identifier = HMAC(scooter code)`. If no vehicle with that identifier has ever been seen, the result is `match_status = unknown_vehicle`, which usually means a typo or a misread code.
- **Ride:** that vehicle's rental whose observed start (`device_history.departed_at` of the stop it left) is within ±4 min of the receipt's start, and whose release (`trip_events.detected_at`) is within ±4 min of the receipt's end. ±4 min is two feed cycles plus clock skew.
- **Where:** the matched ride's `from` and `to` points are the start and end. **Equity eligibility** is whether either point lies in an official Equity Area. A ride that started *and* ended outside every area is not owed the discount: the API records `not_equity_ride` and the rider is told why.
- **`match_status`:**
  - `corroborated`: exactly one ride, both times within tolerance;
  - `partial`: one time matches, e.g. the vehicle was absent from the feed for part of the ride;
  - `ambiguous`: more than one candidate;
  - `none`;
  - `unknown_vehicle`.
- **Where it runs:** a scheduled job (`match_discount_reports`), not the request path.
- **Known limits:**
  - **Failed starts:** an in-place rental leaves no `trip_events` row. That is now counted as a failed start (sql/087), so a receipt for one matches on the start time plus the in-place release instead.
  - **Missing scooters:** vehicles that drop out of the feed mid-ride (`departure_reason = 'absent'`) match on whichever end was observed, so `partial`.

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

1. **Gate:** scooter code + start and end times + a cost. Anything less is declined and not retained. Equity eligibility comes from the feed match.
2. **Points:** 10 *or* 100 per submission, not 110.
3. **Matching:** scooter code plus the receipt's start and end times, ±4 min against feed history. The location comes from the feed, because the receipt has none.
4. **Points settle automatically** when the API verifies a receipt (Phase 3, owner's direction). Only `uncertain` reports wait for a human, after a short shadow-mode start.
