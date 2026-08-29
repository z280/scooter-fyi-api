# API Requirements — data.scooter.fyi backend

Requirements for the API repo (`scooter-fyi-api`, renamed from `veo-audit`;
old URLs auto-redirect) to unblock the frontend phases in
UX_PLAN.md (lives in the frontend repo).
Grouped by the frontend phase each item unblocks; items within a group are
ordered by dependency.

Conventions used below: all endpoints are JSON over HTTPS under `/api/v1/`;
authenticated endpoints take `Authorization: Bearer <token>`; errors follow
the existing `{detail}` shape; CORS allowlist stays production-origins-only
(`denver.scooter.fyi`, plus the Vite dev proxy path as today).

---

## 1. Field promotions (unblocks frontend Phase 2 — read-only)

### 1.1 Promote `vehicle_plate` to the public devices endpoint

- Add `vehicle_plate` to `/api/v1/devices/current` feature properties
  (today it's private-only). Rationale: the number is painted on every
  scooter on the street and printed in its QR code — it is not sensitive —
  and the frontend needs it to build "Unlock in Veo" deep links
  (`https://gmjc.adj.st/?adj_t=622qh4&number=<plate>`).
- **Verification task before shipping:** scan one scooter's QR on-device
  and confirm its `number` query param equals our stored `vehicle_plate`
  for that vehicle. If they differ, expose whichever field matches the QR.

### 1.2 Public reliability signal

Preferred: compute server-side and expose a single field on the public
devices endpoint:

- `reliability_tier: "ok" | "unknown" | "high_risk"` (or 0/1/2), derived
  from: `number_failed_starts` (recent window), dwell time from
  `first_observed_at_location`, `quality_designation`,
  `has_negative_report`, and (once §4 ships) crowdsourced reports.
- Also expose the raw inputs publicly if there's no objection:
  `number_failed_starts`, `first_observed_at_location`. The frontend can
  then explain the tier ("idle 4 days · 2 failed starts") instead of
  showing an opaque grade.
- Document the tier formula in the repo so the audit stays reproducible.

---

## 2. Accounts & sessions (unblocks frontend Phase 3)

Two sign-in doors, one session model. The map stays fully usable
anonymously; accounts exist for the cost ticker's rate choice, report
attribution, ride history and badges. (This originally read "and supporter
features" — see §4.1, withdrawn.)

### 2.1 Session model

- Opaque bearer tokens (random ≥128-bit), stored **hashed** at rest, with
  scopes: `rider` (default) and `admin`. (A third `supporter` scope was
  specified here and never survives to today — see §4.1, withdrawn.)
- Rider sessions: long-lived — 30-day sliding expiry via
  `POST /api/v1/auth/refresh` (returns a rotated token + new expiry;
  invalidate the old token). Nobody re-logs-in on a street corner.
- Admin sessions: same mechanics, shorter fixed expiry (24 h, no sliding).
- Response shape stays compatible with what the frontend's `map-auth`
  plumbing already stores: `{ token, expires }` (ISO timestamp).
- `GET /api/v1/auth/session` → `{ email, scopes, expires }` for UI state;
  401 when invalid/expired. **The `supporter` field specified here was
  removed** with the tier (§4.1) — a frontend reading it now gets
  `undefined`, and should treat every signed-in session as fully
  entitled.
- `POST /api/v1/auth/signout` → revoke the presented token.

### 2.2 Google sign-in

- **Master switch:** env `GOOGLE_AUTH_ENABLED` (default on). Set it to a
  falsy value (`false`/`0`/`no`/`off`) to force Google off regardless of
  `GOOGLE_OAUTH_CLIENT_ID` — `/api/v1/auth/google` then returns 503 and
  `/api/v1/auth/config` reports `google_enabled: false`. Currently off while
  email sign-in (magic link + code) is the only offered door.
- `POST /api/v1/auth/google` with `{ credential }` (a Google ID token from
  Google Identity Services / One Tap).
- Verify locally against Google's JWKS (cache keys; no per-request Google
  API call): signature, `aud` = our OAuth client id, `iss`, `exp`, and
  **require `email_verified: true`**.
- Upsert the account by email; mint a session.
- **Admin allowlist:** env `ADMIN_EMAILS` (comma-separated; initial value
  `zneill@gmail.com`). If the verified email is on the list, the session
  gets the `admin` scope. Admin gates everything the private GitHub gate
  gates today (plates history, failed-start details, future admin
  endpoints).

### 2.3 Magic-link sign-in (Postmark)

- `POST /api/v1/auth/magic-link` with `{ email }` → always returns 202
  (no account-existence oracle). Issues a single-use token, 15-minute TTL,
  stored hashed; sends via the existing Postmark transactional account
  with a link like `https://denver.scooter.fyi/auth?ml=<token>`.
- `POST /api/v1/auth/redeem` with `{ token }` → verifies single-use +
  TTL, upserts account by email, mints a session, burns the token.
- **Magic-link sessions never carry the `admin` scope**, even for
  allowlisted emails — admin requires the Google door. One trust decision,
  enforced server-side.
- Rate limits: 3 links/hour per email, 10/hour per IP. Postmark send
  failures surface as 502 with a friendly detail.

### 2.4 Profile

- `GET /api/v1/profile` / `PUT /api/v1/profile` (scope: `rider`).
- Fields (client-writable): `rate_plan: "resident" | "visitor" | "equity"`,
  `theme: string | null`, `favorites: []` (opaque JSON array for now —
  shape lands with the favorite-device-types spec).
- Fields (server-computed, read-only): `badges: [{ id, label, earned_at }]`
  (see §4.3). The `supporter: boolean` specified here is gone with §4.1.

### 2.5 Retirements — WITHDRAWN (2026-07-28)

This asked to retire the GitHub OAuth app and its callback route once the
admin allowlist worked. **The requirement is dropped by operator decision:
the GitHub gate on `/admin/*` stays.**

Rider auth (bearer sessions + `admin_allowlist`) and the admin panel's
GitHub OAuth are two separate mechanisms on purpose, and keeping the
operator portal behind a second, independent door is a feature rather than
debt. Do not "finish" this retirement.

Note the *map-auth bearer flow* referenced elsewhere in the codebase WAS
retired — that is a different thing from the `/admin/*` GitHub gate, and
those comments are accurate.

---

## 3. Report ingestion (unblocks frontend Phase 4)

### 3.1 Device failure reports

- `POST /api/v1/reports/device` with
  `{ vehicle_identifier, report_type: "not_rideable" | "dead_battery" | "damaged", observed_at?, lat?, lng? }`.
  Two values have been added since (`improperly_parked`, `sql/023`;
  `not_found`, `sql/029`), and `failed_unlock` was renamed
  **`not_rideable`** (`sql/037`) because the rider-facing question is
  broader than whether the unlock worked. The old spelling is still
  accepted as a deprecated alias and normalised at the model boundary, so
  a frontend and backend deploying at different times can't 422 each
  other's riders; nothing reads it back out.
- Anonymous allowed (tight limits: 3/hour per IP); authenticated reports
  are linked to the account (10/hour) and weighted higher in aggregates.
- Idempotency: dedupe identical (vehicle, type, reporter) within 30 min.
- **Feedback loop:** reports feed the §1.2 `reliability_tier` inputs and
  `has_negative_report`.

### 3.2 Missed-discount reports

- `POST /api/v1/reports/discount` with
  `{ ride_ended_at, zone_version: "v1" | "v2", end_lat?, end_lng?, amount_charged_cents? }`
  plus optional multipart `receipt` image.
- Receipt images → R2, private bucket; **strip EXIF** on ingest; retention
  policy documented (suggest 18 months); requires a signed-in session
  (evidence needs provenance).

### 3.3 Aggregates & export

- `GET /api/v1/reports/summary?layer=<boundary>` →
  `{ regions: { [region_name]: { device_reports, discount_reports, est_overcharge_cents } } }`
  — powers the "Contract violations" choropleth and the ticker. Public,
  CDN-cacheable (~10 min).
- `GET /api/v1/reports/export/monthly.csv?month=YYYY-MM` — public CSV for
  DOTI/journalists. No auth, rate-limited.

---

## 4. Rides & badges

### 4.1 Supporter tier / Stripe — WITHDRAWN (2026-07-28)

This section asked for a paid supporter tier: a `POST /webhooks/stripe`
endpoint verifying Stripe signatures, a fixed-price monthly subscription
with a 30-day trial plus the legacy one-time Payment Link, and a derived
`supporter: true` flag gating parts of the rider surface.

**Withdrawn by operator decision. There is no paid tier, no supporter
status, and no Stripe integration.** `sql/036_decommercialize.sql` dropped
`supporter_payments`, `supporter_subscriptions`, and the five `supporter*`
/ `stripe_customer_id` columns on `accounts` (verified empty first:
0 payment rows, 0 supporter accounts — nothing of value was discarded).
`src/stripe_webhook.py` and its route are gone, `STRIPE_WEBHOOK_SECRET` is
no longer read, rendered, or deployed, and the `supporter` scope no longer
exists.

Support for the project, when it exists, comes from merchandise or a
direct donation with **no in-app incentive attached** — which needs no
backend at all. Do not re-derive a supporter flag from a donation.

The only gates in this system are "signed in" and "on the admin
allowlist".

### 4.2 Ride history

- `POST /api/v1/rides` (scope `rider`) with
  `{ started_at, ended_at, duration_s, distance_m, est_cost_cents, rate_plan, started_in_zone: bool, ended_in_zone: bool, polyline }`
  (polyline = encoded lat/lng string). **No supporter gate** — nothing on
  the rider surface is gated, and paywalling data collection just means
  less data.
- `GET /api/v1/rides` — owner-only list, paginated.
- `GET /api/v1/rides/export?format=geojson|csv` — owner-only.
- `DELETE /api/v1/rides/:id` and `DELETE /api/v1/rides` — **hard delete**,
  immediate. Route polylines are the most sensitive data this system will
  hold; no soft-delete, no analytics reuse, and say both in the privacy
  note.

**Delivered wider than specified (2026-07-27, `sql/035_off_feed_rides.sql`):**
`rides` is now the OFF-FEED ride tracker — rides on vehicles with no
`vehicle_identifier`, which the GBFS-detected mechanism (§1.1b /
`sql/027_tracked_rides.sql`) cannot cover. It carries a full lifecycle
(`start` → `waypoints` → `end`) alongside the one-shot POST above, and an
active ride expires 24 h after creation (`sql/040`) so an abandoned ride
can't permanently occupy the one-active-ride slot. A client-asserted
`distance_m` is plausibility-checked before storage, because §4.3's
mileage badges count it.

### 4.3 Badges

- Server-computed on profile read (no separate endpoint): reports filed,
  ghost scooters confirmed (report later corroborated by another user or
  API inference), discount reports, miles logged, ride streaks. **Every
  badge is available to every account** — the `supporter` badge this
  originally carved out is withdrawn with §4.1, and nothing here is tied
  to payment.
- Mileage/streak badges count both ride mechanisms (`rides` and
  `tracked_rides`), ended rides only. See `src/badges.py`.

---

## 5. Cross-cutting

- **Rate limiting:** per-IP and per-account buckets on all POST endpoints,
  plus per-IP buckets on the two routing GETs and the geocoding GET
  (`route_ip` 30/min, `route_profiles_ip` 60/min, `geocode_ip` 20/min — a
  sidecar round trip is expensive enough to be worth capping); 429 with
  `Retry-After`.
- **Secrets/env:** `ADMIN_EMAILS`, `GOOGLE_OAUTH_CLIENT_ID`,
  `POSTMARK_TOKEN`, R2 credentials. (`STRIPE_WEBHOOK_SECRET` was listed
  here; withdrawn with §4.1 and removed from `.env.example`,
  `docker-compose.yml` and the deploy workflow.)
- **Privacy page data:** the API should serve
  `GET /api/v1/meta/privacy` (or a static doc) enumerating retention:
  sessions (30 d idle), magic-link tokens (15 min), receipts (18 mo),
  rides (until user deletes), reports (indefinite, aggregated). Grown
  since to cover tracked rides, device photos, ride transaction
  screenshots and model reports — anything the system stores belongs in
  that payload AND in `src/templates/legal/privacy_policy.html`.
- **Repo rename:** `veo-audit` → `scooter-fyi-api` (GitHub auto-redirects
  old URLs). Keep "Veo Audit" as the public dataset/report brand.

## 6. Sequencing

| Order | Item | Unblocks frontend |
|---|---|---|
| 1 | §1.1 plate promotion (+ QR verification) | Phase 2 deep links |
| 2 | §1.2 reliability tier / raw fields | Phase 2 reliability UI |
| 3 | §2 accounts (Google → sessions → magic link → profile) | Phase 3 cost ticker |
| 4 | §2.5 GitHub retirement | Phase 3 admin migration |
| 5 | §3 reports + aggregates | Phase 4 |
| 6 | §4 rides + badges (§4.1 Stripe withdrawn) | Phase 5 |

Items 1–2 are read-only and deployable independently of everything else;
start there.

---

## 7. Real range & calculated battery percent (design note, 2026-07-07)

Grounded in the 37-day archive analysis (`scripts/analyze_range_signal.py`,
44.2M points, 2026-05-31 → 2026-07-06). Full findings in that script's
docstring; the three that drive this design:

- `current_range_meters` is an **integer SoC percent behind a fleet-wide
  100-value lookup table** (stable all 37 days, cap 45,293 m for every
  vehicle type regardless of rated max). It carries zero per-model range
  information.
- Straight-line distance explains ≤3% of per-pair SoC burn (r² ≤ 0.03) —
  round trips, van rebalancing, 1%-step quantization, and post-ride
  rebound (+0.63% mean) swamp it. **Per-ride burn prediction is not
  viable from GBFS pairs**; per-model aggregate burn rates are
  (≈2.3–2.9 %SoC/km ⇒ real full-charge range ≈35–43 km, not the rated
  45–67 km).
- Idle readings are trustworthy: 98.5% of 42M stationary pairs show
  exactly zero change.

### 7.1 Fix `compute_battery_percent` (bug, ship first)

`src/quality.py` currently divides by `max_range_meters_for_type` (Veo's
rated max), which the data disproves: the true full-charge value is
45,293 m for every type, so a full scooter reads 86% and a full bicycle
reads **68%** — no bike can ever show 100. Two changes:

- **Battery percent = rank of `current_range_meters` in the 100-value
  lookup table** (exact integer SoC). Persist the table (data/ or a small
  migration) with a fallback of `round(100 * r / 45_293)` for values not
  in it; log/Sentry when the fallback fires so table drift is noticed.
- **Quality tiers:** `_GREAT_FRAC_OF_MAX` is applied against rated max, so
  "great" requires 50,250 m for bicycles — above the 45,293 m cap.
  **No bicycle can currently earn "great."** Re-express tier thresholds
  in recovered SoC percent.

### 7.2 Real-range feature

- **Model:** per-model burn-rate table `{model: {p25, p50, p75 %SoC/km}}`
  computed offline from the archive (evolves from
  `scripts/analyze_range_signal.py` §4 means; conservative = p75).
  Stored in Postgres (small table, one row per model + computed_at);
  refreshed by a monthly cli job, not per-request.
- **API:** on `/api/v1/devices/current` features, alongside
  `battery_percent`: `est_range_low_m` / `est_range_high_m` =
  battery_percent ÷ p75/p25 burn — **an honest interval, not a false-
  precision point estimate**. Optional `battery_settling: true` when the
  device MOVED within the last ~20 min (rebound window — reading may be
  ~1% low).
- **Freshness signal:** a swap-detection flag (`recently_swapped`, SoC
  jump ≥ +20% while stationary) is cheap from `device_state` deltas and
  marks "guaranteed full battery" devices on the map.
- **Frontend framing:** display as "~4–6 mi real range"; per-model, so
  Apollo ≠ Astro at the same percent. This is the substance of the
  "Range Maximizer" premium tier: honest range budget + elevation-aware
  routing — NOT per-ride battery forecasting (see revisit below).

### 7.3 Revisit after the data cooks (target: ≥ 2026-08-05)

`vehicle_model_name` only exists in the archive from 2026-07-05
(migration 016), so per-model regressions currently rest on ~2 days.
By early August there will be a full month. Then:

1. Re-run `scripts/analyze_range_signal.py`; per-model rows get
   month-scale samples.
2. Add a per-day slope breakdown — explain why the pre-016 period shows
   slope ≈ 0 while the named-model days show 1.3–1.9 %SoC/km.
3. Add a clean-trip filter (single-gap pairs, displacement > 1 km,
   excluding zero-burn long moves = van transport) and re-check r².
4. Decision gate: if clean-trip r² stays < ~0.3, close per-ride
   prediction permanently and finalize the aggregate design above; if it
   climbs, revisit with Valhalla routed distance as the regressor
   (Section 2 routing plan).

---

## Status

| Item | Status |
|---|---|
| §1.1 plate promotion | **Reverted.** Shipped in PR #8, then rolled back — `vehicle_plate` is no longer exposed on the public `/api/v1/devices/current`; it stays private-only (`/api/v1/private/*`). Any frontend "Unlock in Veo" deep link must source the plate from an authenticated endpoint or Veo's own GBFS `rental_uris`. |
| §1.2 reliability tier + raw fields | Implemented (PR #8). Formula documented in `src/quality.py` and API.md. |
| §2.1–§2.4 accounts, sessions, profile | Implemented (PR #9): `src/accounts.py`, `src/api_auth.py`, `src/api_profile.py`, `sql/012`. |
| §2.5 GitHub OAuth retirement | **Done** — the GitHub "elevated map" OAuth flow (`map_auth.py`, `map_auth_dep.py`, the `scripts/client/` drop-ins, the `/admin` Map-tokens view, and the `api_tokens` table) is removed. The `/api/v1/private/*` endpoints it gated now require the Google `admin` session scope (`require_admin`). NOTE: the *operator* `/admin` panel keeps its own separate GitHub OAuth (`auth.py`) — that was never part of §2. Deploy prereq: `ADMIN_EMAILS` must be set so an admin session can actually be minted, else the private endpoints are unreachable. |
| §3 reports + aggregates | Implemented (PR #9): `src/api_frontend_reports.py`, `src/receipts.py`, `src/geo.py`, `sql/013`. Device reports feed `has_negative_report`/`reliability_tier`. |
| §4.1 Stripe / supporter tier | **Withdrawn (2026-07-28).** Removed by `sql/036_decommercialize.sql`; `src/stripe_webhook.py` and `STRIPE_WEBHOOK_SECRET` are gone everywhere including the deploy workflow. See §4.1. |
| §4.2–§4.3 rides + badges | Implemented (PR #9), un-gated and widened into off-feed rides (`src/api_rides.py`, `src/badges.py`, `sql/014` → `sql/035`, `sql/040`). |
| §5 rate limits, env, privacy endpoint | Implemented (PR #9): `src/ratelimit.py`, `src/api_meta.py`, `.env.example`. |
| Repo rename | **Done** — repo renamed `veo-audit` → `scooter-fyi-api`; GitHub redirects old URLs, and in-repo references (compose image fallbacks, doc links, image names) are updated. The Compose project name `veo-audit` and the `/opt/veo-audit` deploy dir deliberately keep the old name — see the post-rename checklist in MIGRATION.md. |
| Equity boundary migration (new §1.1a) | In progress — see note below. `er1`–`er6` per-rank layers now tracked with full metric parity to v1/v2 (snapshot + daily SLA). `v1` retirement and the compliance-metric cutoff are still pending a DOTI decision. |
| Vehicle classification + trip tracking (new §1.1b) | Implemented — see note below. `vehicle_use_type`/`vehicle_model_name` on devices/current + device_state/history; `sitting`/`standing` compliance parity with `bicycle`/`scooter`; `trip_events` + daily popularity rollup at 9am. |
| §7 real range + battery percent | §7.1 implemented (2026-07-07): rank-based `battery_percent` via `data/range_soc_lut.json`, quality tiers re-expressed in SoC percent ('great' now reachable for bicycles). §7.2 buildable now; §7.3 revisit gated on ≥30 days of post-016 archive (≥ 2026-08-05). |
| Ride Mode A1 — ride session foundation (§10 reported fields) | Implemented. `sql/047_tracked_rides_reported_fields.sql` adds §10's `reported_minutes` (0–1440) / `reported_plan` (`resident\|visitor\|equity`) as separately guarded named constraints, **not** §10's published inline-CHECK DDL. `sql/049_ride_sessions.sql` adds the per-ride signing material (`track_nonce`, `track_key`, `track_key_issued_at`), the rider- and feed-derived start battery/position, the client-owned `ride_options` blob (4 KB cap, `413`/`422` in the handler) and the `validation_status`/`validation_reasons`/`validated_at` triple. `POST /api/v1/tracked-rides` issues `track_signing`; it is owner-only and appears in exactly three responses (start, `GET .../active`, `GET .../{id}`) and **never** in the list — enforced structurally by a separate `_RIDE_COLS_OWNER` column list. `PATCH .../end` sets a provisional status only; A2 owns finalisation. |
| Ride Mode A1 — routing maneuvers + routing rate limits | Implemented. `GET /api/v1/route?maneuvers=true` returns turn-by-turn cues via new `valhalla.trip_maneuvers()`, with per-leg shape indices re-offset onto the concatenated response LineString in the same pass (and with the same conditional duplicate-vertex drop) as `trip_shape()` — cues from a leg that contributed no geometry are dropped rather than mis-pointed. Closes the standing gap that both routing GETs were unlimited: `route_ip` 30/min and `route_profiles_ip` 60/min per IP, enforced from a route dependency so it cannot be forgotten. Both endpoints now touch Postgres and fail **closed** on a DB outage, matching every other `enforce()` call site. |
| Ride Mode geocoding (`GET /api/v1/geocode/search`) | Implemented (A1). Self-hosted **Photon** sidecar — `docker/photon/Dockerfile` (pinned + sha256-verified official jar), index seeded from `r2://$R2_MAP_BUCKET/photon/photon-index-<YYYYMMDD>.tar.zst` by `src/r2_map.py:sync_photon_index` (ETag-gated; `fetch_photon_index` one-shot + `refresh_photon_index` at 05:00), fronted by `src/api_geocode.py`: public, bucket `geocode_ip` 20/min/IP, `envelope.denver_core` bbox filter (wider than the routing graph on purpose), `in_coverage` computed against `valhalla.graph_bbox`, 3 s timeout → 503 `geocoder_unavailable`, 512-entry/24 h in-process cache. Index rebuild is manual and quarterly: `scripts/build_photon_index.md`. |
| Ride Mode Usuals (A1) | Implemented — `sql/050_ride_mode_usuals.sql` adds `user_preferences` kind `ride_mode_usual` (name required, one per `(account, name)`); `src/api_preferences.py` serves `GET/PUT/DELETE /api/v1/profile/ride-usuals[/{name}]`, 10 per account (`MAX_RIDE_USUALS`), 16 KB per blob. Blob is opaque (`ride_options` + `label`), validated only when used to start a ride. |
| Along the Way — My Scooters (Phase 4) | Implemented — `sql/081_favorite_devices.sql` adds `favorite_devices` (one row per `(account, vehicle)`, cascading with the account, no FK to `device_state` so a favourite outlives its vehicle); `src/api_favorites.py` serves `GET/POST/PATCH/DELETE /api/v1/profile/favorite-devices[/{vehicle_identifier}]`, 10 per account (`MAX_FAVORITE_DEVICES`). **Two distinct rules.** The GATE: keeping a vehicle needs a payload that passes `validate_scan` **and** a fix within `FAVORITE_PROXIMITY_METERS` (75 m, the Unlock-in-Veo radius) of its last known position — the scan alone only proves plate knowledge, since nothing in `src/api_qr.py` or `credit_qr_scan_points` has ever compared the submitted `lat`/`lng` to anything. The WITHHOLDING: a favourite's position, battery and range are **not returned while the vehicle is in a rental** (`is_reserved` or `device_state.rental_started_at`), reported as an explicit `position_withheld` flag rather than a silent omission. Rented Veo vehicles broadcast a live moving position in the public feed (`src/ride_watch.py`), so a one-tap persistent subscription to one vehicle would be a tool for following a person; parked position stays public, moving position does not. The rider's own fix is **checked and discarded**, never stored. Keeping runs the existing once-per-`(account, vehicle)` `credit_qr_scan_points` path, so there is no way to double-pay. |
| Ride Mode A1 — pricing + points schedule | Implemented. `GET /api/v1/meta/pricing` (public; fractional Denver combined tax rate, `config.json` `"pricing"`, out-of-range values refused) and `GET /api/v1/points/schedule` (public; the **complete** action → award map, generated from `src/points.py` with no literals in the handler, including all five ride-mode constants and their formula shapes). The ride-mode values are published ahead of the award machinery on purpose — frontend F2 interpolates them into Screen 2/9 copy on day one. Awards land in A2/A3 and need no further edits to the schedule. |
| Ride Mode A2 — track donation + verification | Implemented. `sql/051_track_donations.sql` adds `track_donations`/`donated_track_points` (raw JWS discarded after verification; only `chain_root_hash` + the per-check `verification` summary persist), a guarded `battery_trip_observations.source` (`feed_mined`\|`donated_ride`), and `tracked_rides.track_donated_at` (the durable already-donated marker that survives de-id). `src/track_verify.py:verify_track_chain` is a pure, six-check verifier (signature+chain integrity share one `chain` key; monotonic/bounds; accuracy-clamped speed plausibility with a >10% sustained-fast `points_status="pending_review"` flag; GBFS start/end correlation, feed-anchored with a client-supplied fallback; volume minimums) — never raises, not even on malformed input (`verdict="error"`). `POST /api/v1/tracked-rides/{id}/track` (owner-only, `track_donation_account` 6/h, 2 MB / 600-batch caps) composes it with persistence, points, and battery ingestion in one transaction opening on the same `ride_validation:<ride_id>` advisory lock the start handler and `finalize_validation` use, so a donation and a GBFS resolve landing on the same ride serialize instead of racing. |
| Ride Mode A2 — points reshape + supersession | Implemented. `sql/053_ride_mode_points.sql` widens `user_points_action_allowed` for the five new ride-mode actions (guarded on `'battery_contribution'` alone, so it no-ops correctly whichever of A2/A3 lands first) and adds `CHECK (points % 2 = 0)`, VALIDATED. `src/points.py:credit_battery_contribution`/`credit_nav_distance_bonus` implement the `8 + 2⌈km/2⌉` / `2⌈km/3⌉` formulas over the *verified* donation distance, filed at the ride's **start** point (deliberately unlike the superseded end-filed awards), gated by the caller on `ride_options`/own-device/both-batteries-known/a stored route. `credit_points` gained `assert points % 2 == 0`. `PATCH .../end` no longer credits `waypoint`/`gbfs_trip_validated` — GBFS reappearance is now an eligibility *gate* (feeding `validation.status`), not an award; both actions remain on historical ledger rows. |
| Ride Mode A2 — validation finisher + battery ingestion | Implemented. `src/ride_watch.py:finalize_validation(cur, ride_id)` settles a `pending_feed` ride once GBFS resolves (its own resolve path) or the watch window elapses (`src/cli.py:expire_stale_watches`, extended with the same finalizer loop) — locking `ride_validation:<ride_id>` **before** touching the ride row in every participant, the exact ordering that avoids a deadlock against a mid-flight donation. On a late `pending_feed → eligible` transition it credits the held `battery_contribution`/`nav_distance_bonus` (reading the `points_status` flag back off `track_donations.verification`, since the raw batches it was computed from are long gone) and runs `battery_model.ingest_donated_observation` — the sole ingestion path for a donation that arrived before GBFS resolved. `ingest_donated_observation` double-count-guards against the nightly feed-mined miner (deletes any overlapping non-`donated_ride` observation first) and re-derives elevation by routing a downsampled subset of the verified track through Valhalla (falls back to `NULL` on a failed map-match, e.g. no sidecar). |
| Ride Mode A2 — de-identification sweep | Implemented. `python -m src.cli deidentify_donations` (crontab `15 * * * *`) nulls `account_id`/`tracked_ride_id` and coarsens `donated_track_points.recorded_ms` to the minute, 4 h after `points_settled_at` **or** 28 h after `donated_at` regardless of settlement (the force floor). Also carries a `to_regclass('ride_routes')`-guarded 28 h sweep for phase A3's route geometry — now active: `sql/052` (below) has landed, so `to_regclass('ride_routes')` resolves and this arm sweeps `ride_routes` on its own 28 h `created_at` clock every run. Three-address rule closed: `src/api_meta.py:_PRIVACY` gained `donated_tracks` and the pre-existing `user_points` gap, and amended `tracked_rides` to describe the post-de-id delete-cascade boundary; `src/templates/legal/privacy_policy.html`'s retention table carries the matching rows. |
| Ride Mode A3 — ride routes persistence | Implemented. `sql/052_ride_surveys_routes.sql` adds `ride_routes` (geometry: profile, origin/destination, `route_polyline`, distance/duration/battery estimate, nullable-on-de-id `tracked_ride_id`/`account_id`) and `ride_surveys`, plus a guarded widening of `user_points_action_allowed` for `ride_survey`/`nav_route_feedback`/`nav_qualitative_feedback` keyed on `'ride_survey'` — deliberately a *different* sentinel from sql/053's `'battery_contribution'` key, so the two migrations converge on the identical five-action list regardless of landing order (verified: replaying either migration after the other is a byte-identical no-op). `POST /api/v1/ride-routes` (owner-scoped, `ride_route_account` 30/h): validates `profile` against `load().valhalla.profile()`, the polyline against `src/polyline.py:decode()` (≥2 points), both endpoints against `load().valhalla.contains()` (mirroring `GET /api/v1/route`'s `out_of_coverage` rejection), and the three client-claimed metrics by bound (`distance_meters` 0–80 000, `duration_seconds` 0–10 800, `battery_percent_estimate` 0–100); a non-null `tracked_ride_id` must resolve to a caller-owned ride, else 404. No uniqueness on `tracked_ride_id` — multiple stored routes per ride is intended (`tests/test_ride_routes.py`). `src/cli.py:deidentify_donations`'s pre-existing `to_regclass('ride_routes')`-guarded 28 h sweep now activates against this table with no further code change. |
| Ride Mode A3 — end-of-ride survey + award wiring | Implemented. `src/api_ride_surveys.py`: `POST /api/v1/tracked-rides/{id}/survey` (owner-only, ride must be ended, single-shot — `ride_surveys.tracked_ride_id UNIQUE` backstopping a `SELECT ... FOR UPDATE` race guard) validates `issues` against the 16-item vocabulary and `model_bonus` keys against the ride's server-stamped `device_state.current_vehicle_model_name` (never the client's claim), and links a caller-owned `ride_route_id` to this ride on submission (422 if it's already linked elsewhere or unowned — a stale id and a guessed one fail identically, by design). Wires all three of A1's remaining ride-mode point actions via `src/points.py:credit_ride_survey`/`credit_nav_route_feedback`/`credit_nav_qualitative_feedback` (flat 4/4/6, every gate read by the caller off `ride_options` and the payload, filed `source_table='tracked_rides'` so `MAX_POINTS_PER_RIDE` binds). Ride payloads (`start`/`GET .../active`/`GET .../{id}`/list) gain a `survey_submitted` flag via a new batched `_survey_submitted_ids` helper in `src/api_tracked_rides.py`, itself guarded on `to_regclass('ride_surveys')` for A2/A3 landing-order safety. |
| Ride Mode A4 — leaderboard endpoint + privacy semantics | Implemented, and since sql/061 computed at READ time rather than nightly. `src/area_leaders.py:refresh_universe()` (cron `15 9 * * 1 python -m src.cli refresh_area_universe`) full-replaces only the CELL UNIVERSE (`h3_r8_area_report`: every r8 cell that has ever had an observed device or a point, all-time, unioned from `device_history`/`device_state`/`user_points`) — weekly, because that answer is all-time and its `device_history` DISTINCT scan is the one expensive step. `sql/061` dropped the stored `h3_r8_area_leaders`/`regional_leaders` tables and `h3_r8_area_report`'s derived counters: the standings are now a trailing-28-day, `status='confirmed'` aggregate of `user_points` run per request, tie-break `points DESC, first_point_at ASC, account_id ASC` (in SQL; `area_leaders._rank_cell` remains the reference implementation and a test holds the two together), top 3 per cell. `src/api_leaderboard.py`: `GET /api/v1/leaderboard/map` and `GET /api/v1/leaderboard/regional` (both public) live-join `accounts`, applying `show_in_leaderboards`/`show_public_username`/NULL-`display_name` filtering at READ time, so an opt-out takes effect on the very next request; a skipped earner falls through (leader = highest surviving earner, runners_up = the rest, <=3 total; total_points/distinct_earners stay unfiltered, since they carry no identity). Colors are live-joined too; an unclaimed `ruling_color`/`ruling_border_color` pair also nulls `ruling_alpha` rather than leaking its `NOT NULL DEFAULT 0.60` schema value. Neither endpoint 503s any more — there is no run to be missing — and the map unions the stored universe with cells that have points in the window, so a cell claimed since the last weekly refresh renders immediately. ETag is content-only (`W/"arealb:<sha256(canonical payload)[:16]>"`): there is no run id to key on, and `computed_at` is now "when you asked", so keying on it would make every tag unique. `Cache-Control: public, max-age=30`. `GET /api/v1/private/{area,regional}-leaders` (`src/api_private.py`) are the unfiltered admin siblings, likewise live. Three-address rule: `src/api_meta.py:_PRIVACY` and `src/templates/legal/privacy_policy.html` both now describe `h3_r8_area_report` as the no-personal-data cell list it is, and state plainly that standings are not stored at all. |

**§1.1a Equity boundary migration note (2026-07-04, updated):** Denver
DOTI delivered an authoritative, census-block-group-based Equity Index
(`data/DOTI_Equity_Index_Final.geojson`, 572 block groups, continuous
`EquityScore` + 6-tier `EquityGroupRank` where 1 = highest need). Analysis
of the two legacy boundaries against it:

- **`v2`** is built on the *same* census block groups (identical
  `GEOID20` keys) as the new index — its 65-block-group footprint is a
  strict superset of the new index's `EquityGroupRank ≤ 1` area (100%
  overlap) and 70.8% of `EquityGroupRank ≤ 2`. Same lineage, refined
  scoring.
- **`v1`** is a hand-drawn, non-census polygon set with no linking
  identifier at all. Best-case IoU against any rank cutoff is 0.27 — a
  materially worse and structurally different match.

**Decision: `v1` is being retired; `v2`'s historical series is the one
being carried forward.**

Superseding the earlier composite `v3`/`v4` prototype layers, the system
now tracks **each of the six `EquityGroupRank` tiers individually** as
`er1` (highest need) through `er6` (lowest) — see `src/equity_groups.py`
for the registry and `sql/015_equity_rank_groups.sql` for the schema.
Each group has full metric parity with `v1`/`v2`:

- **`snapshot_metadata_core`** gets the same 8 fields
  (`total_devices_<g>`, `total_bike_<g>`, `total_scooter_<g>`,
  `percent_all_devices_<g>`, `percent_all_bikes_<g>`,
  `percent_all_scooters_<g>`, `percent_bikes_<g>`, `percent_scooters_<g>`)
  for every `<g>` in `{v1, v2, er1..er6}`, computed every 10-minute cycle.
- **`daily_sla_compliance`** gets the matching `avg_*` fields for every
  group in the 6am–9am Denver window. `compliance_<g>_pass` booleans are
  **only** stored for `v1`/`v2` (`COMPLIANCE_GROUPS` in
  `src/equity_groups.py`) — no individual `erN` tier is itself a
  compliance boundary, so there's nothing to pass/fail on its own. The
  frontend combines whichever `erN` groups make up a candidate cutoff
  and computes pass/fail itself from the `avg_percent_all_devices_erN`
  values.

Tracking every rank **individually and atomically** — rather than
pre-combining into a guessed cutoff like the old `v3`/`v4` did — means
whatever cutoff DOTI eventually confirms as contractually authoritative
(e.g. "rank ≤ 2") can be reconstructed retroactively from already-collected
history (`er1 + er2`) instead of needing the right combination decided in
advance. **No individual `erN` tier is itself a confirmed compliance
requirement** — `percent_all_devices_v1` / `compliance_v1_pass` remain
the primary RFP §3.0 metric until DOTI confirms otherwise. Once that
happens, this note gets replaced with the actual migration (retiring
`v1`, promoting the confirmed cutoff to "the" compliance metric).

**§1.1b Vehicle classification + trip tracking note (2026-07-05):**
Field investigation while chasing the equity-boundary question above
surfaced that Veo's own `vehicle_types.json` mislabels its pedal-equipped
two-person e-bike (`vehicle_type_id: 4`, in-app name "Apollo") as
`form_factor: "scooter"` — confirmed by direct visual inspection of four
physical units (seat, pedals, no way that's a scooter). Two changes:

1. **Ground-truth vehicle registry** (`src/ingest.py`
   `_KNOWN_VEHICLE_TYPES`): `vehicle_type_id → {app_name, use_type,
   form_factor override}`, currently covering `id=1` (Astro, standing
   scooter), `id=3` (Cosmo, sitting e-bike, no pedals), `id=4`
   (Apollo, sitting e-bike, pedals, ~18mph — `form_factor` corrected to
   `bicycle`), and `id=5` (Cosmo-class, sitting e-bike, no pedals —
   field-confirmed 2026-07-16; Veo's registry wrongly says `scooter`, so
   `form_factor` corrected to `bicycle`). `vehicle_model_name` and `vehicle_use_type` are new fields
   on `/api/v1/devices/current`, `device_state`, and `device_history`.
2. **`vehicle_use_type` (sitting/standing) gets full compliance-stat
   parity with `form_factor` (bicycle/scooter)** — same 8-field family,
   same tracked groups (v1/v2/er1-6 + citywide), in both
   `snapshot_metadata_core` and `daily_sla_compliance`. Generalized via
   `SPLIT_DIMENSIONS` in `src/equity_groups.py` rather than duplicated
   by hand — a third dimension is a registry entry + migration, not a
   rewrite. Rationale: sitting vs standing is the accessibility-relevant
   operative distinction for compliance, independent of Veo's own GBFS
   form-factor vocabulary (which this incident shows can be wrong).

Also landed in the same pass: **trip/popularity tracking**. Every MOVED
transition `src/device_state.py` detects (a vehicle relocated between
cycles — i.e. someone rode it) is logged to `trip_events`. A new 9am
Denver cron job (`python -m src.cli daily_trips`, `src/daily_trips.py`)
rolls the prior full calendar day up into `daily_trip_summary` (total
trips, distinct vehicles tripped) and `daily_vehicle_trip_counts`
(per-vehicle trip count + popularity rank, ties sharing a rank). Read
back via `GET /api/v1/private/trips/daily?date=YYYY-MM-DD`. A new batch
lookup endpoint, `GET /api/v1/private/devices/lookup-batch?plates=...`,
also shipped alongside — built for checking hand-spotted plates (like
the Apollo/Cosmo ground-truth set that started this) against stored
signals in one call instead of one request per plate.

**Cadence cutover note (2026-07-07):** ingest moved from every 10
minutes to every 2 minutes. The upstream feed is generated per-request
(`last_updated` stamps at fetch time, `ttl: 0` — measured), so there was
never an upstream cadence to match; 2 min tightens trip-duration
resolution to ±2 min for the §7 burn-rate work. Metric-continuity
impacts: (a) `daily_trip_summary` counts step up slightly from the
cutover date — back-to-back rides with a short intermediate stop that a
10-min gap merged into one MOVED event now resolve as separate trips
(an accuracy correction, not inflation; single rides are unaffected
because in-rental vehicles are absent from the feed and produce exactly
one MOVED on reappearance at any cadence — **WRONG, see the correction
below**); (b) per-cycle tables grow 5×
faster (~6.3M raw rows/day) — `archive_hours` dropped 48→24 to keep the
archive window inside the scheduler's memory ceiling, and the archive
DuckDB session is now explicitly memory-capped; (c) the 6–9 AM SLA
averages are cadence-insensitive (more samples, same estimator). The
live schedule is the admin-edited crontab on the `scheduler_state`
volume — the repo `crontab` only seeds fresh environments.

**Correction (2026-08-10) — in-rental vehicles are NOT absent from the
feed.** Measured against the telemetry archive: Veo keeps a rented vehicle
in `free_bike_status` for the whole rental, sampled every 2 minutes,
broadcasting its live moving position, with `is_reserved` true. Verified on
a specific ride (2026-08-08 19:23–19:44): available at 19:22, `is_reserved`
true at 19:24, 14 moving samples, `is_reserved` false again at 19:46 at the
drop point. Fleet-wide over one window, consecutive samples of reserved
vehicles moved 320 m on average (68% of steps > 50 m) against 1.2 m for
everything else (0.2%).

Two consequences, one fixed and one still open:

* **Fixed** (this change): `src/ride_watch.py` read "checked out" as
  "absent", so the watch never fired — 19 of 19 tracked rides had
  `gbfs_left_feed_at` NULL and all 17 donations aged out at
  `gbfs_end: pending_feed` for 0 points. It now accepts absence *or*
  `is_reserved`.
* **OPEN — `trip_events` over-counts trips by roughly 6×.** Every 2-minute
  sample of a moving rental clears `src/device_state.py`'s MOVED threshold
  and appends its own `trip_events` row, so one rental becomes ~10 "trips"
  rather than the one this note assumed. On 2026-08-09: 187,820 MOVED steps
  over 16 m, of which 161,160 (86%) fall inside a reservation episode,
  against **30,566 reservation episodes** — median 6 min, mean 10 min, 70%
  between 4 and 40 min, i.e. a credible rental-duration profile. The same
  fragmentation splits one rental into ~10 two-minute rows in
  `device_history`, which is what `dwell_stats` reads. Anything downstream
  of trip counts (`daily_trip_summary`, `daily_vehicle_trip_counts`, H3
  popularity, area leaders, and any §-level compliance figure derived from
  them) is affected. **Not corrected here** — collapsing a rental into one
  trip restates published historical metrics and is a deliberate call, not
  a drive-by fix.

**§1.1 QR verification note:** the stored `vehicle_plate` is parsed from
the `&number=` query param of Veo's own `rental_uris.android/.ios` deep
links in the GBFS feed (see `src/ingest.py`). The frontend deep link
(`https://gmjc.adj.st/?adj_t=622qh4&number=<plate>`) uses the same
adjust.com host and `number` param, so equality with the QR code's
`number` is expected by construction — but the on-device scan of one
physical scooter remains a required human check before the frontend
ships Phase 2.
