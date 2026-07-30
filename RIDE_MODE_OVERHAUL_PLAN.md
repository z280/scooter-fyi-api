# Ride Mode Overhaul — Program Plan (Master)

Status: **planning approved 2026-07-29**. This master document is committed **byte-identical** to both
repositories:

- `denver-scooter-fyi` → `docs/RIDE_MODE_OVERHAUL_PLAN.md`
- `scooter-fyi-api` → `RIDE_MODE_OVERHAUL_PLAN.md`

Each repository additionally carries its own detailed, actionable plan, structured as a few **big**
phases ready for division across multiple implementing agents:

- API: `PLAN_RIDE_MODE_API.md` (phases **A1–A4**)
- Frontend: `docs/PLAN_RIDE_MODE_FRONTEND.md` (phases **F1–F4**)

Where this master narrative and a per-repo plan disagree on a detail, the per-repo plan wins for its
own repo; where either disagrees with the owner's vision below, the vision wins unless a numbered
**Decision** or **Risk/Reconciliation** in this document explicitly supersedes it.

---

## Part 0 — The Vision (owner's copy, authoritative)

Ride mode start becomes a multi-screen **modal wizard** — each screen is a phase of the modal —
unless started from a specific device on the map (a device deep link fast-forwards the wizard).
`${provider}` is Veo today; copy is written to be provider-parameterized.

> Numbering note: the owner's numbering has **no Screen 5** — the gap is intentional and preserved.
> Never renumber. Screen 2.5 is the "Usuals" picker.

### Screen 1 — Auth & GPS (skippable if logged in; a device link fast-forwards the wizard but does **not** bypass these gates)

- IF not logged in: **[Ride as Guest]** or login options — **Email Me a Link** | **Email Me a Code** |
  **Login w/ Google**.
- IF no GPS: an **Enable GPS** prompt.
- If neither applies the screen never appears; proceed to Screen 2. A device deep link changes
  the landing screen, not these gates — a signed-out or GPS-ungranted deep-link entry still sees
  Screen 1 (Ride as Guest keeps it one tap).

### Screen 2 — Select your ride + Ride Mode Options

Layout: left half / right half in **landscape**; top half / bottom half in **portrait**.

**Left (landscape) / top (portrait):**
- Top ~10%: **"Select your ride:"**
- Center: a list of buttons styled like the find-my-ride results — the nearest scooters around the
  resolved GPS position, ordered by distance. Bottom of the list: **[ My own Device ]**.
- Bottom ~20% — **Confirm:**
  - `Plate# _ _ _ _ _ _ _`
  - `Battery _ _ %`
  - Tapping an entry area reveals a numeric keyboard — **native in portrait, our own in landscape**.

> Scale clarification (owner): this list is *disambiguation*, not discovery — "there should be
> literally 1 device within 4m of the user in most cases. We're talking **'next to' vs 'nearby'**."
> The existing 🛴 Find wheels wizard and Recommended drawer remain the discovery surfaces and are
> untouched by this program.

**Right (landscape) / bottom (portrait):**
- During plate entry: the numeric keypad (custom, well designed, easy to use in landscape; in
  portrait render nothing here and let the native keyboard show).
- Otherwise, **Ride Mode Options**:

  | Control | Option |
  |---|---|
  | [ On \| Off ] | Est. Veo Cost HUD ℹ |
  | [ Classic \| Digital HUD \| None ] | Speedometer ℹ |
  | [ ☀️ \| 🌘 \| auto ] | Theme ℹ |
  | [ On \| Off ] | Destination Navigation ℹ |
  | [ On \| Off ] | Save ride tracks locally ℹ |
  | [ On \| Off ] | Improve battery modeling 🏆 ℹ |
  | [ On \| Off ] | Navigation Improvement 🏆 ℹ |
  | [ On \| Off ] | End ride survey 🏆 ℹ |

  Footnote: **🏆 Earns points for leaderboards**. Buttons: **[Usuals] [NEXT >>]**

**Logic:**
- When using **own device**: *Improve Battery Modeling* and *End Ride Survey* are disabled.
- When *Save ride tracks* is **off**: both *battery improvement* and *nav improvement* are disabled.
- **[Usuals]** appears only for users with ≥1 saved ride-mode express settings; selecting it shows
  **Screen 2.5** (pick a saved Usual, apply, return).

**ℹ info-modal copy (owner's text, verbatim except noted corrections):**

- **Estimate ${provider} Cost HUD** — "The app can show a Heads Up Display with your expected ride
  cost, based on what we know about the duration of your trip and the rate provided. This helps
  avoid end of ride surprises. Note: The ${provider} app will always be the authority on ride cost."
- **Speedometer** — "We've found that the speedometers on the Veo devices are really hard to read,
  especially in the bright colorado sun. So, we provide ON by default both a classic and digital
  readout of your speed tracked by GPS. Disable if you don't like fun or convenience. Always keep
  your eyes on where you're going!"
- **Theme** — "Show the map in dark mode, light mode, or auto based on the time of day."
- **Destination Navigation** — "We're trying to make not just 'good' directions, but, THE BEST
  directions for scooters and eBikes in Denver! Unlike the big name providers, we specifically AVOID
  paths that City of Denver reports as High Injury Network (HIN) roads. Our primary route type
  provides a direct safe route using safe infrastructure as much as possible, and we also give you
  options to avoid hills and save battery, stay out of the sun as much as possible, and just take
  the most direct route."
- **Save Ride Tracks** — "This option allows you to trace where you've been on the map display, and
  also save waypoints of your location to your local device. Tracking information is not persisted
  to Scooter.fyi unless you opt to share."
- **Improve battery modeling** — "*Why*: Veo's data seems to suggest that every single one of their
  fleet has the same distance capability on a full charge. We think that's kind of fake, and we want
  to build a more accurate prediction of device range. *How*: This feature requires association with
  a specific Veo scooter, and saved ride tracks donated at the end of your trip. You'll need to
  start the scooter approximately at the location where you started ride mode, and end the scooter
  ride where you end the ride mode, report the battery percentage showed in the Veo app at the end
  of your trip, and donate your saved ride tracks (stored waypoints). With all conditions met,
  you'll earn **8 pts** for a valid trip + **2 points per 2 kilometer** tracked (rounded up).
  *Our Usage*: After awarding points, the stored trip data is disassociated from your personal
  account and used along with the provided start and end percentages to improve our understanding of
  expected range vs reported battery for Veo devices."
- **Improve Navigation** — "We want to provide the BEST navigation for users in Denver, and we need
  your help. At the end of your ride return to the app to complete a quick survey about your route,
  and donate your trip data in order to earn points. Earn **4 points** for following the selected
  route and providing a rating, **6 pts** for qualitative feedback, plus **2 points per 3 km** of
  valid trip data (rounded up, so a 1 km trip gets 2 points). After points award, navigation records
  used for navigation improvement are disassociated with your account."
  *(Correction: the original draft said 5 pts for qualitative feedback; the owner's even-points rule
  — see Decision 6 — raises it to 6.)*
- **End Survey** — "Collect details about the scooter/glider/bike you just rode in order to help
  Scooter.fyi users to continue to find the best scooters available. Survey provides **4 pts**."

### Screen 3 — Where to? (shown IF navigation on)

Centered title **"Where to?"**; a wide text bar at the top. As the user types, suggested addresses
appear; tapping an address selects it and moves to Screen 4.

### Screen 4 — Route choice (address selected + nav on)

- **Loading**: one side shows a 2D overview of source and destination points; the other side shows
  **four route-option tombstones** with a left-to-right border/content shining wipe effect.
- **Loaded**: **40% side** — toggle between the prepared routes; **60% side** — all routes drawn on
  the map, colored by their specific type.
- Route types are the API's deployed Valhalla profiles: **safe** (Safe & Protected — the primary,
  HIN-avoiding), **range** (The Range Maximizer — hills/battery), **shade** (The Shaded Canopy),
  **express** (Commuter Express — most direct). *(This resolves the owner's "TODO: Improve plan with
  route types from API work.")*

### Screen 5 — (intentionally unused)

Owner numbering skips 5; preserved.

### Screen 6 — Start in Veo (specific Veo device + cost-HUD tracking opted)

Most similar to the current pre-start page. Since the scooter and its start link are known: offer
Android and Apple **"Start in Veo"** buttons which trigger a default **10 s countdown**; an
**"I already started"** button skips the countdown. After countdown or manual start, ride mode
initiates.

### Screen 7 — Main in-ride view

Nearly identical to today's HUD, with key improvements:

- Move the **timer to the top left**, with the **≈ cost just below it**.
- In the **wrench menu**: add a **clock** above the adjustment buttons; add a **Stop tracking**
  button.
- **Hide all scooters and close all tooltips by default** upon ride-mode start.
- When in navigation mode, show a **step-by-step navigation HUD in the center**. The nav corners
  carry an **arrow insignia**: pressing the **left** arrow opens step-by-step directions as a panel
  on the left side; pressing the **right** arrow, the same on the right — compressing the existing
  UI to make room. **Press and hold** closes/removes navigation guidance.

### Screen 8 — Post-ride begins (user ended ride + a Veo device was selected, i.e. not a private ride)

> "End your ride in the Veo app. Note the cost and battery % of your scooter after ending, and don't
> forget to come back here though to contribute and earn points."

- `Ride time: __:__ (stop)`
- `Est Cost: Unlock $ + Per Min $ + Tax $ = Total $`
- `** The Veo app is your bill **`
- Buttons: **[Rush Quit] [ New Destination ] [ I ended my ride in Veo ]**
- Clarification (program): "Note the cost and battery %" is an *instruction to the rider* that
  the **[I ended my ride in Veo]** flow acts on — that flow collects the end battery % and actual
  cost as inputs before reporting the end (battery modeling's burn math and the cost record both
  depend on them).

### Screen 9 — Surveys (on a Veo scooter + tracked ride, regardless of navigation)

50/50 split.

**Left — Scooter Feedback (+4 pts):**
- Would you ride this device again? [Yes | No]
- Was it absolutely perfect? [Yes | No]
- IF no — what wasn't? (choose any): App (Veo), Acceleration, Basket, Battery, Bell, Brakes,
  Connectivity, Customer Service Experience, Dirty device, Kickstand, Pedals, Phone Holder, Price,
  Speedometer, Scooter.fyi issue, Vandalized.
- Bonus `${deviceType}` questions — **COSMO**: Does it have a front basket? (yes/no); **APOLLO**:
  What was your top speed? (numeric); **ASTRO**: Is there a landscape phone holder that works?
  (yes/no).

**Right — Navigation Feedback (up to 10 pts + distance bonus):**
- How was the `${selectedRoute}`? 1/10 – 10/10
- Did you deviate from the proposed routing?
- Was that because the routing needs improvement?
- How likely are you to recommend navigating via Scooter.fyi to other `${provider}` users? (0–10)

*(Correction: the owner's header said "+6 points"; the itemized values — 4 route+rating,
6 qualitative, 2 per 3 km — are authoritative, and the header is generated from
`GET /api/v1/points/schedule` so copy can never drift.)*

Bottom of both panes: **[Skip] [Submit]** — proceed if there is collected trip data to manage.

### Screen 10 — Contribution eligibility (IF waypoints were tracked)

Generated text:

> Your ride {may be | is} {eligible | ineligible} for community contribution points
> [ **because** {the start location did not align with the veo feed record | the end location did
> not align with the veo feed record | you did not opt to track your route | your device did not
> collect the requisite number of waypoints successfully | your trip was too short | your saved
> track failed integrity verification | there was an internal error} ]
> ‖ [ **but** we're waiting on validation from the live feed[, **and**] you'll need to donate your
> trip data to earn these points. ]

*(Program addition: the "failed integrity verification" clause covers the `chain_invalid` reason,
which the validation vocabulary needs but the original six-clause skeleton lacked — wording is
program-proposed, owner may re-voice it.)*

Buttons: **[Donate This Trip's Data] [See recent trips] [ Return to Main App ]**

### Trip-data page (profile pane; possible follow-up, outside ride mode)

- General trip metadata (vehicle, user, start, end) is stored securely on the scooter.fyi API server
  and **de-identified 4 h after points settle, with a hard floor of 28 h after donation even if
  points never settle** — so there is a limited window in which points can be earned on a trip.
- Specific GPS points recorded while travelling ("waypoints") are **stored locally on the rider's
  device**. The server has no access to these unless the rider chooses to share.
- In the future we may offer a way to self-encrypt trip data and upload or download it.
- Sharing data is **opt-in, always**, and trip waypoint data is de-identified once earned points are
  recorded. Contributions build a better platform for `${locale}` users.

### Leaderboard view (owner addition, "rough bang out" scope)

Similar to the nav and theme icons right of the hamburger menu: a **🏆 Trophy icon left of the
Person menu** opens the leaderboard view. **Zero devices** shown while open. **Choropleth of H3 r8
cells**; click a cell for details; cells **colored by the leader's color preference** (their claimed
ruling colors); the detail shows **rankings per zone under a generous-size section for the leader**.

---

## Part 1 — Program architecture

### 1.1 Goals

1. Replace the single-button ride arm with the Screens 1–10 modal wizard.
2. Move waypoint custody to the **client**: tracks recorded locally in IndexedDB as tamper-evident,
   hash-chained, HMAC-signed batches. **Zero mid-ride network traffic for the track** — the server
   sees nothing between ride start and ride end; the chain is verified server-side **only at
   donation** (Decision 1: chain only, no live checkpoint pings; format stays forward-compatible
   with checkpoints).
3. Turn donated data into two feedback loops: **battery modeling** (donated distance + reported end
   battery → `battery_trip_observations`) and **navigation improvement** (chosen route + ratings →
   `ride_routes` / `ride_surveys`).
4. Reshape the **points economy**: battery contribution 8 + 2 per 2 km (ceil); nav improvement 4
   (route + rating) + 6 (qualitative) + 2 per 3 km (ceil); end-ride survey 4. Per-waypoint 2 pts and
   `gbfs_trip_validated` 20 pts stop being awarded (superseded). `MAX_POINTS_PER_RIDE = 100` stays.
   **All point values are even, everywhere, enforced** (Decision 6).
5. Honor privacy: the server keeps only trip **metadata** under the account; donated geometry is
   **de-identified 4–28 h after points settle**; the points ledger keeps only the coarse h3 r8 cell —
   exactly what the leaderboard consumes.
6. Ship `FEATURE_PLAN_2026-07.md` **§10** (reported ride fields) and **§11** (H3 r8 area
   leaderboard) in this program — plus the rider-facing **🏆 Leaderboard view**.
7. Self-host geocoding: a **Photon sidecar** in the compose stack, fronted by
   `GET /api/v1/geocode/search`, following the Valhalla sidecar pattern exactly.

### 1.2 Owner decisions (locked)

1. **Waypoint tamper-resistance: "chain only, no pings."** Server-issued per-ride HMAC key + nonce
   at ride start; hash-chained signed batches stored locally; verified server-side only at donation.
   No mid-ride checkpoint traffic. The chain format must permit adding live checkpoints later with
   zero format change.
2. **Geocoding: self-host Photon now** — compose sidecar (expose-only, R2-seeded index, one-shot
   fetch gate), fronted by `GET /api/v1/geocode/search` with Denver bias + rate limiting.
3. **Leaderboard: include FEATURE_PLAN §11** in this program.
4. **Screen 2 is "next to", not "nearby"**: a disambiguation list (typically one device within
   ~4 m), distances shown in feet, plain distance sort; the plate/battery confirm fields are the
   authoritative device check; a manual-plate path is always available. Find wheels / Recommended
   untouched.
5. **Leaderboard view UI** per Part 0 (topbar 🏆, choropleth in leaders' ruling colors, zero
   devices, per-cell rankings). Rough cut acceptable.
6. **Even-points rule**: the owner never intended odd point values anywhere — "anywhere I offered 5
   make it 6. Intentionally points should always be even." Enforced structurally: a
   `CHECK (points % 2 = 0)` on `user_points`, an assertion in `credit_points()`, and a test sweeping
   every constant and formula output.

### 1.3 Glossary

| Term | Meaning |
|---|---|
| **Screen N** | Owner's wizard numbering, 1–10. No Screen 5 (intentional). Screen 2.5 = Usuals picker. |
| **Ride** | A `tracked_rides` row (GBFS-anchored, points-eligible) or a **private ride** ("My own Device" / guest — local-only, never points-eligible; the legacy off-feed `rides` table is untouched by this program). |
| **Trip** | Veo's billing concept / GBFS `trip_events` transitions. The Veo app is the bill; we only estimate. |
| **Track** | The locally recorded waypoint chain: sealed, signed batches in IndexedDB. Local until donated; the server never sees waypoints mid-ride. |
| **Batch** | ≤25 waypoints or ≤60 s of track, sealed as one compact JWS, hash-chained to its predecessor. |
| **Donation** | Explicit opt-in bulk upload of the signed track for verification and points. Irrevocable **after** de-identification; hard-deletable before. |
| **Usual** | A saved ride-options preset (`user_preferences` kind `ride_mode_usual`). |
| **De-id** | The sweep that nulls account linkage on donated artifacts: 4 h after points settle, with a hard floor of 28 h after donation even if points never settle. Ledger rows keep h3 cell + account (they ARE the leaderboard record); geometry loses the account. |
| **Disambiguation list** | Screen 2's device list — "which one am I standing next to", not discovery. |
| **Leaderboard view** | The 🏆 choropleth: h3 r8 cells colored by the leading account's ruling colors, devices hidden while open. Reads `GET /api/v1/leaderboard/map` only. |

### 1.4 System-level data flow

```
Modal wizard (S1 auth → S2 disambiguate device + options → [S3 dest → S4 route] → S6 start)
  → POST /tracked-rides (+ ride_options, reported_start_battery)
  → response carries track_signing {key, nonce}            [authed channel, at start]
  → ride-session.ts persists session; track-store.ts records GPS locally
       └─ seals JWS batches into IndexedDB (hash chain) — NO network traffic
  → S7 in-ride HUD (+ nav HUD from /route maneuvers)
  → user ends → S8 (PATCH /end: minutes, plan, cost, end battery)
  → ride_watch resolves gbfs_* (0–3 h)
  → S9 surveys (POST /survey → survey points)
  → S10 eligibility (validation_status from GET detail)
       └─ [Donate] → POST /track (bulk JWS) → server verifies chain
            → validation → points (battery / nav / distance bonuses)
            → battery_trip_observations ingestion
  → de-id sweep (hourly cron): 4 h after points settle, hard floor
       28 h after donation even if points never settle —
       donated track / ride_routes lose account + ride linkage
  → leaderboard recompute (§11, daily) reads user_points h3 cells
  → 🏆 Leaderboard view renders /leaderboard/map choropleth (ruling colors)
```

### 1.5 Cross-repo contract summary (what the frontend consumes)

| Endpoint | Change | Frontend phase |
|---|---|---|
| `POST /api/v1/tracked-rides` | request +`ride_options`, `reported_start_battery_percent`; response +`track_signing`, +`validation` | F2/F3 |
| `GET /api/v1/tracked-rides/active`, `/{id}` | response +`track_signing` (owner-only), `ride_options`, `validation`; +`survey_submitted` (A3) | F3/F4 |
| `POST /api/v1/tracked-rides/{id}/track` | **new** — bulk donation, the sole track upload path | F4 |
| `PATCH /api/v1/tracked-rides/{id}/end` | +`reported_minutes`, `reported_plan` (§10) | F4 |
| `POST /api/v1/tracked-rides/{id}/survey` | **new** | F4 |
| `POST /api/v1/ride-routes` | **new** — persist chosen route when nav-improvement is on. Ships in **A3**, later than F2's A1 baseline: F2 calls it non-blocking and tolerates 404 until A3 deploys (nav points forfeited in that window) | F2 |
| `GET /api/v1/route` | +`maneuvers=true` passthrough; + IP rate limit | F2/F3 |
| `GET /api/v1/geocode/search` | **new** — fronts the self-hosted Photon sidecar | F2 |
| `GET /api/v1/meta/pricing` | **new** — tax rate, config-driven | F2 |
| `GET/PUT/DELETE /api/v1/profile/ride-usuals[/{name}]` | **new** — prefs kind `ride_mode_usual` | F2 |
| `GET /api/v1/points/schedule` | **new** — authoritative action → points map for UI copy | F2/F4 |
| `GET /api/v1/leaderboard/map` | **new** — §11 with leader **+ runners-up** per cell (one fetch serves choropleth and click-through detail) | Leaderboard lane |

### 1.6 Sequencing / dependency graph

```
API A1 (sessions+signing, geocode/Photon, /route maneuvers, usuals, pricing, §10 fields)
  ├──> FE F2 (wizard screens)          FE F1 (foundations) has NO API dependency
  └──> FE F3 (in-ride + local tracking)
API A2 (donation, verification, validation, points reshape, de-id) ──> FE F4 (screens 8–10)
API A3 (surveys + ride_routes + nav points)                        ──> FE F4
API A4 (§11 leaderboard)               ──> FE Leaderboard view (independent of all ride-session work)
```

Deploy order: **A1 → (F1 ‖) → F2/F3 → A2 + A3 → F4 → A4 any time after A2** (earlier is fine — it
touches only the ledger read side). F1 starts immediately, in parallel with A1. A2 and A3 are
independently mergeable after A1. One deliberate cross-edge: F2 calls `POST /ride-routes` (an A3
endpoint) **non-blocking** — route choice proceeds on a 404 until A3 deploys, forfeiting only nav
points in that window, so no F2→A3 ordering edge exists. The Leaderboard view is deliberately
decoupled from all ride-session work — the ideal parallel-agent work item.

Migration numbering: `sql/045` (SMS sign-in codes, FEATURE_PLAN §9) and `sql/046` (comms replies)
are already on main and untouched here; this program ships `047` (§10) and `048` (§11) itself and
owns **049–053**. Migrations apply in sorted order at boot and are tracked in `schema_migrations`, so
numbering order vs. landing order is safe.

---

## Part 2 — Track chain format (single source of truth)

Golden test vectors implementing this spec are committed **byte-identically to both repos**
(frontend Vitest and API pytest consume the same JSON fixtures) at the canonical path
**`tests/fixtures/track-chain-vectors.json`** — one file, the same literal path in each repo.

Byte-encoding rules (normative — the two implementations must hash the same bytes): all sha256
inputs and intermediate values are **raw bytes**; the nonce is hex-decoded to its 16 raw bytes
before hashing; `sha256(jws_n)` is over the ASCII bytes of the compact JWS string; `prev` and
`chain_root_hash` serialize as lowercase hex of the raw digest.

- Waypoint tuple: `[dt_ms, lat, lon, acc_m]` — `dt_ms` relative to batch `t0`; lat/lon at 6
  decimals; accuracy rounded to an integer.
- Seal a batch at **25 waypoints or 60 s**, whichever comes first, and at ride end (final partial
  batch).
- Each batch is a **compact JWS**, `alg: HS256`, key = the per-ride server-issued key. Private/guest
  rides use a client-random key — tamper-evident only, never points-eligible.
  - Protected header: `{"alg":"HS256","typ":"sfyi-track+jws","kid":"<ride_id>"}`
  - Payload: `{"v":1, "rid":"<ride_id|'private'>", "non":"<nonce>", "seq":n,
    "prev":"<hex sha256 of previous batch's compact JWS, '' for seq 0>", "t0":ms, "t1":ms,
    "pts":[[...],...], "rec":false}` — `rec:true` marks a batch sealed from crash-recovered
    unsealed points.
- Rolling chain hash: `H_n = sha256(H_{n-1} || sha256(jws_n))`, `H_-1 = sha256(nonce)`. The client
  computes and stores `H_n` per batch and reports the final value as `chain_root_hash` at donation.
- **Forward compatibility (deliberate)**: nothing transmits mid-ride, but the rolling hash is
  defined and computed now, so a future live-checkpoint endpoint would only need to transmit
  `(seq, H_n)` — **no change to the chain or batch format**. Do not redesign the format to add
  checkpoints later; they already fit.
- **Known, accepted limit**: no field marks the final batch, so a verifier cannot distinguish a
  complete chain from a truncated prefix — silently dropping *trailing* batches is undetectable
  by the chain itself. It only shrinks the claimable distance, and the surviving last point must
  still pass the GBFS end correlation, so truncation buys a forger nothing. If ever hardened, a
  `fin:true` field on the final batch is a backward-compatible addition (same argument as the
  checkpoint note).

Server verification (`src/track_verify.py`, detailed in `PLAN_RIDE_MODE_API.md` §A2): signature →
chain integrity → time monotonicity within the server-stamped ride window → speed plausibility →
GBFS start/end correlation → volume minimums. Reason vocabulary (drives Screen 10's generated
copy): `start_mismatch`, `end_mismatch`, `tracking_not_opted`, `too_few_waypoints`,
`trip_too_short`, `chain_invalid`, `internal_error`; plus status `pending_feed`.

---

## Part 3 — Risks & reconciliations

1. **"NO ROUTE EVER LEAVES ITS OWNER" vs donation** — the commitment in `src/api_rides.py` is
   stated generally, over "waypoints, polylines and ride endpoints", not scoped to a table — so
   this program does not argue it away on scoping. Donation supersedes it by **explicit,
   per-ride, per-donation consent** with disclosed de-identification: the default remains that no
   route ever leaves its owner unless the rider affirmatively donates that ride's track. The API
   phase that ships donation (A2) must update the `api_rides.py` docstring context, `_PRIVACY`,
   and the privacy-policy template together (three-address rule). Screen 10 consent copy says
   "anonymous and irrevocable after de-identification (≤28 h)".
2. **Hard-delete vs de-id** — pre-de-id, deletes cascade (commitment intact); post-de-id, artifacts
   have no owner to delete from. Disclosed at donation; `DELETE /tracked-rides/{id}` before the
   sweep removes everything.
3. **`user_points` geography vs de-id** — resolved: ledger rows keep account + h3 r8 + start coords
   forever (they are the §11 leaderboard record); everything with fine geometry loses account
   linkage within ≤28 h. The privacy page must say the ledger keeps a coarse cell.
4. **600/h per-waypoint POST vs bulk** — donation replaces streaming entirely; the per-waypoint
   endpoint is deprecated. It has **no known client callers** — the frontend never wired it — so
   it is kept one release purely as caution for unknown external callers (no points from A2
   onward), decoupled from any frontend phase. No mid-ride track traffic at all.
5. **Points supersession** — `waypoint` (2) and `gbfs_trip_validated` (20) stop being awarded; GBFS
   alignment becomes an eligibility **gate**, not an award. The action CHECK retains old values
   (history is forever); `MAX_POINTS_PER_RIDE` unchanged. Riders who maximized the old scheme see
   lower ceilings on short rides; the new scheme pays for useful data.
6. **Nav feedback itemization + even-points rule** — authoritative values are 4 + 6 + 2/3 km
   (qualitative raised 5 → 6 by the even-points rule); UI headers are generated from
   `/points/schedule` so copy cannot drift; the owner's Screen 9 "+6 points" header and the original
   "5 pts qualitative" are both superseded by the corrected itemization.
7. **Screen numbering** — no Screen 5; owner numbering preserved with an explicit "(intentionally
   unused)" row.
8. **Tamper-resistance honesty (chain-only)** — the key holder is also the adversary of interest,
   and without live checkpoints there is no server-side receipt anchor during the ride: the chain
   proves the data was constructed by the key holder *for this ride, inside its server-stamped
   window* — it cannot prove waypoints were recorded live rather than fabricated at donation time.
   **GBFS correlation + speed plausibility are the primary anti-fabrication controls**, and the
   fabrication attack is priced above the point value (~40 pts max on a 10 km ride). Named future
   hardening: live checkpoints slot in with zero chain-format change.
9. **Browser capabilities** — IndexedDB unavailable/evicted (Safari private mode; 7-day PWA
   eviction): in-memory fallback + "tracks won't survive reload — donate right after your ride"
   banner; the signing key is recoverable via `GET active.track_signing`. WebCrypto HMAC is
   universal on secure contexts. `orientation.lock` is unsupported on iOS Safari — best-effort; the
   layout handles both orientations regardless. Background-tab GPS throttling: wake lock already
   exists; batches tolerate gaps (monotonicity is per-point, not fixed-rate).
10. **Self-hosted Photon ops** — new failure surface: index build correctness, JVM memory,
    staleness. Mitigations: Colorado-scoped index (~low-GB, ≤2 GiB RAM); healthcheck + 3 s proxy
    timeout → clean 503 → client degrades to "type an address, no suggestions"; ETag-gated fetch
    mirrors the proven Valhalla path; quarterly refresh documented. The proxy contract is
    upstream-agnostic — a hosted fallback is a config swap.
11. **Screen 2 GPS precision** — consumer GPS accuracy (5–20 m) can exceed inter-scooter spacing at
    a corral, so the disambiguation list can rank the wrong device first. Mitigations: the
    plate/battery confirm fields are the authoritative device check (mismatch switches selection);
    auto-preselect only within 8 m with ≤15 m accuracy; the manual-plate path is always visible.
12. **Guest/private rides** — no server key, no points; tracks stay local under a client-random key
    (tamper-evident only). Own-device disables battery modeling + end survey (no GBFS ground
    truth), per the owner's Screen 2 logic.
13. **Migration numbering** — 045 stays reserved for SMS (harmless if it never ships); this program
    owns 046–052 minus 045.
14. **Route maneuver indices** — Valhalla shape indices are leg-local, and the existing
    `trip_shape()` drops the duplicated shared vertex between legs **conditionally** (only when
    the boundary vertex actually repeats; empty-shape legs are skipped entirely); the maneuver
    passthrough must re-offset indices in the same pass with the same conditional logic — never
    a fixed one-drop-per-join formula (a named A1 test). Getting this wrong silently misplaces
    every turn cue.
15. **Leaderboard launch emptiness + color legibility** — production has ~8 ledger rows: at launch
    nearly all ~720 cells render unclaimed. That is correct, not broken — the view should look
    intentional when empty ("Unclaimed territory" copy). Rider-chosen ruling colors carry no
    contrast guarantee against either basemap theme; accepted for the rough cut (the colors are the
    point), with the opaque-border convention doing the legibility work. Runners-up in the public
    payload expose nothing beyond what §11 already stores; read-time privacy filters every entry.
16. **Screen 9 pane gates** — the vision's header ("on a Veo scooter + tracked ride, regardless of
    navigation") governs when Screen 9 *as a whole* may appear; the two panes then gate
    **individually**: the right (Navigation Feedback) pane renders only when a route was selected
    and stored ("How was the `${selectedRoute}`?" presupposes one, and the API awards route
    feedback only when a `ride_route_id` resolves), and the left (Scooter Feedback) pane renders
    only when the Screen 2 "End ride survey" option is on — that toggle exists to control exactly
    this pane, and the survey award gates on the same option. Both panes gated off → Screen 9 is
    skipped entirely.
