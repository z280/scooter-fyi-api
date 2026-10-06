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

## Phase 3: review + points

- **Admin review queue** (`/admin/discount-reports`): screenshot beside the submitted numbers, with approve/reject on "subtotal and total match the receipt".
- **On approve:** **+10** (`action = 'discount_receipt'`).
- **If also `match_status = corroborated`:** **+100** (`discount_receipt_corroborated`), in place of the 10.
- Points are inserted `pending` at submission and settled on review, so a rider sees them as pending at once. The existing ledger supports this.
- **Abuse limits:** one award per matched ride, 20 submissions per account per day (the existing limit), and points only after a human has looked at the screenshot.

## Phase 4 (optional): reading the screenshot automatically

OCR or vision extraction of minutes, subtotal and total, to pre-fill the form and pre-check against the image. It would turn the 10-point check from manual into assisted. Deferred: it adds a third-party processor of the receipt image, which needs its own privacy decision.

## Phase 5: reporting

- Confirmed rate errors are aggregated by Equity Area in the public summary and monthly CSV (no identity).
- The rider gets an outcome notification on their account: reviewed / corroborated / points.

## Defaults chosen (say if any should change)

1. **Gate:** minutes + a cost + an Equity Area point. Anything less is declined and not retained.
2. **Points:** 10 *or* 100 per submission, not 110.
3. **Matching tolerance:** ±10 min on times, 100 m on points (your spec).
4. **Points need a human look** at the screenshot (Phase 3) before they settle. Until then they show as pending.
