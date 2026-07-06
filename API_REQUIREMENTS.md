# API Requirements — data.scooter.fyi backend

Requirements for the API repo (currently `veo-audit`, proposed rename
`scooter-fyi-api`) to unblock the frontend phases in
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
attribution, and supporter features.

### 2.1 Session model

- Opaque bearer tokens (random ≥128-bit), stored **hashed** at rest, with
  scopes: `rider` (default), `admin`, `supporter` (derived, see §5).
- Rider sessions: long-lived — 30-day sliding expiry via
  `POST /api/v1/auth/refresh` (returns a rotated token + new expiry;
  invalidate the old token). Nobody re-logs-in on a street corner.
- Admin sessions: same mechanics, shorter fixed expiry (24 h, no sliding).
- Response shape stays compatible with what the frontend's `map-auth`
  plumbing already stores: `{ token, expires }` (ISO timestamp).
- `GET /api/v1/auth/session` → `{ email, scopes, supporter, expires }` for
  UI state; 401 when invalid/expired.
- `POST /api/v1/auth/signout` → revoke the presented token.

### 2.2 Google sign-in

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
- Fields (server-computed, read-only): `supporter: boolean`,
  `badges: [{ id, label, earned_at }]` (see §5.3).

### 2.5 Retirements

- Once Google + magic links are live and the admin allowlist works,
  retire the GitHub OAuth app and its callback route. The frontend removes
  its hidden-tab gate in the same release.

---

## 3. Report ingestion (unblocks frontend Phase 4)

### 3.1 Device failure reports

- `POST /api/v1/reports/device` with
  `{ vehicle_identifier, report_type: "failed_unlock" | "dead_battery" | "damaged", observed_at?, lat?, lng? }`.
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

## 4. Supporter tier (unblocks frontend Phase 5)

### 4.1 Stripe webhook

- `POST /webhooks/stripe` — verify the Stripe signature.
- **Recurring plan (current):** a single fixed-price monthly subscription
  with a 30-day free trial (Payment Links can't do pay-what-you-want on
  recurring prices, only one-time). Handle `checkout.session.completed`
  (mode=subscription: link `client_reference_id` → the Stripe
  subscription/customer id) plus `customer.subscription.created/updated`
  (status/trial_end/current_period_end) and `customer.subscription.deleted`
  (cancellation). `supporter: true` while status is `trialing` or `active`
  — a trial counts as supporter access, no payment required yet. Event
  ordering between the checkout and subscription events isn't guaranteed;
  both sides upsert by `stripe_subscription_id` (see `src/stripe_webhook.py`
  docstring).
- **One-time Payment Link (legacy):** still honored for anyone who
  supported that way before the switch — `checkout.session.completed`
  (mode=payment) sets `supporter: true` and stores amount + timestamp;
  `charge.refunded` clears it only on full refund.
- No other Stripe surface needed — no products API, no customer portal in
  v1.

### 4.2 Ride history

- `POST /api/v1/rides` (scope `rider`, supporter required) with
  `{ started_at, ended_at, duration_s, distance_m, est_cost_cents, rate_plan, started_in_zone: bool, ended_in_zone: bool, polyline }`
  (polyline = encoded lat/lng string).
- `GET /api/v1/rides` — owner-only list, paginated.
- `GET /api/v1/rides/export?format=geojson|csv` — owner-only.
- `DELETE /api/v1/rides/:id` and `DELETE /api/v1/rides` — **hard delete**,
  immediate. Route polylines are the most sensitive data this system will
  hold; no soft-delete, no analytics reuse, and say both in the privacy
  note.

### 4.3 Badges

- Server-computed on profile read (no separate endpoint): reports filed,
  ghost scooters confirmed (report later corroborated by another user or
  API inference), discount reports, miles logged, ride streaks. Earned
  badges are available to every account — only the `supporter` badge is
  tied to payment.

---

## 5. Cross-cutting

- **Rate limiting:** per-IP and per-account buckets on all POST endpoints;
  429 with `Retry-After`.
- **Secrets/env:** `ADMIN_EMAILS`, `GOOGLE_OAUTH_CLIENT_ID`,
  `POSTMARK_TOKEN`, `STRIPE_WEBHOOK_SECRET`, R2 credentials.
- **Privacy page data:** the API should serve
  `GET /api/v1/meta/privacy` (or a static doc) enumerating retention:
  sessions (30 d idle), magic-link tokens (15 min), receipts (18 mo),
  rides (until user deletes), reports (indefinite, aggregated).
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
| 6 | §4 Stripe + rides + badges | Phase 5 |

Items 1–2 are read-only and deployable independently of everything else;
start there.

---

## Status

| Item | Status |
|---|---|
| §1.1 plate promotion | **Reverted.** Shipped in PR #8, then rolled back — `vehicle_plate` is no longer exposed on the public `/api/v1/devices/current`; it stays private-only (`/api/v1/private/*`). Any frontend "Unlock in Veo" deep link must source the plate from an authenticated endpoint or Veo's own GBFS `rental_uris`. |
| §1.2 reliability tier + raw fields | Implemented (PR #8). Formula documented in `src/quality.py` and API.md. |
| §2.1–§2.4 accounts, sessions, profile | Implemented (PR #9): `src/accounts.py`, `src/api_auth.py`, `src/api_profile.py`, `sql/012`. |
| §2.5 GitHub OAuth retirement | Pending — gated on Google + magic links being live in prod and the frontend removing its hidden-tab gate in the same release. |
| §3 reports + aggregates | Implemented (PR #9): `src/api_frontend_reports.py`, `src/receipts.py`, `src/geo.py`, `sql/013`. Device reports feed `has_negative_report`/`reliability_tier`. |
| §4 Stripe + rides + badges | Implemented (PR #9): `src/stripe_webhook.py`, `src/api_rides.py`, `src/badges.py`, `sql/014`. |
| §5 rate limits, env, privacy endpoint | Implemented (PR #9): `src/ratelimit.py`, `src/api_meta.py`, `.env.example`. |
| Repo rename | Pending — GitHub settings change (`veo-audit` → `scooter-fyi-api`), operator action. |
| Equity boundary migration (new §1.1a) | In progress — see note below. `er1`–`er6` per-rank layers now tracked with full metric parity to v1/v2 (snapshot + daily SLA). `v1` retirement and the compliance-metric cutoff are still pending a DOTI decision. |
| Vehicle classification + trip tracking (new §1.1b) | Implemented — see note below. `vehicle_use_type`/`vehicle_model_name` on devices/current + device_state/history; `sitting`/`standing` compliance parity with `bicycle`/`scooter`; `trip_events` + daily popularity rollup at 9am. |

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
   scooter), `id=3` (Cosmo, sitting e-bike, no pedals), and `id=4`
   (Apollo, sitting e-bike, pedals, ~18mph — `form_factor` corrected to
   `bicycle`). `vehicle_model_name` and `vehicle_use_type` are new fields
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

**§1.1 QR verification note:** the stored `vehicle_plate` is parsed from
the `&number=` query param of Veo's own `rental_uris.android/.ios` deep
links in the GBFS feed (see `src/ingest.py`). The frontend deep link
(`https://gmjc.adj.st/?adj_t=622qh4&number=<plate>`) uses the same
adjust.com host and `number` param, so equality with the QR code's
`number` is expected by construction — but the on-device scan of one
physical scooter remains a required human check before the frontend
ships Phase 2.
