# Feature plan — profiles, SMS auth, ride receipts, area leaders

Planned 2026-07-28 against `feat/operator-hard-invariants` (5c59ba7).
Four independent features, rush delivery. Written to be folded into
`API_REQUIREMENTS.md` as §8–§11 once agreed.

**`sql/042_auth_session_methods.sql` is already written and verified** —
the §9.1 fix below, shipped ahead of the rest because it is a live 500.
Next free migration number is therefore **043**. Migrations are applied once and
recorded in `schema_migrations` (`src/pg.py:run_migrations`), and the
`_pg` tests replay the whole `sql/` directory — so every file below is
idempotent/guarded in the style `sql/041` established.

Production is small today (5 accounts, 8 `user_points` rows, 0
`tracked_rides`), so none of these migrations carry backfill risk. The
one big table involved is `device_history` (7.3M rows, 2.2 GB) and
nothing here rewrites it.

## Decisions already taken

| Question | Decision |
|---|---|
| `reported_cost` on tracked rides | **Dropped.** `total_cost_cents` already stores exactly this (rider-reported at `PATCH /end`). No second cost column. |
| Report resolution | **r8**, not r10 (~0.74 km² cells; 720 distinct r8 cells observed all-time vs 4 515 at r10). |
| Hexagon universe | r8 cells with **observed devices OR points history** — not a full polygon tiling. |
| `ruling_color` | Curated palette table (≥128 options), plus a curated **inner border color**. The (fill, border) **pair** is globally unique. Rider-settable **alpha**. |
| `royalty_title` | Curated list, free choice for any signed-in rider — same posture as adjective/emoji today. |
| SMS code format | **`AA000AA`**, identical to the email door, not six digits. Easier to read back, and the whole generate/normalize/hash path is shared rather than duplicated. |

---

## Blocking prerequisites (operator, not code)

1. **SMS gateway credentials.** The private server at
   `/home/ubuntu/sms-gateway` (`ghcr.io/android-sms-gateway/server:latest`,
   v1.45.2, `127.0.0.1:3000`) is live and healthy, with 1 device and 1
   user already registered and 1 message sent. The 3rd-party API user's
   **username/password are auto-generated on the Android app's first
   connection and are only readable in the app** — they are not the
   `GATEWAY__PRIVATE_TOKEN` (that token authenticates the *phone* to the
   server, not us to the API). Needed as `SMS_GATEWAY_USERNAME` /
   `SMS_GATEWAY_PASSWORD` before §2 can be tested end-to-end.
2. **Container networking.** `pipeline_worker` is on `veo-audit_default`
   (172.16.1.0/24); the gateway is on `sms-gateway_default` (172.18.0.0/16)
   and publishes only to the host's loopback. They cannot reach each other
   today, and `host.docker.internal` will not help against a
   `127.0.0.1`-bound port. Fix: declare `sms-gateway_default` as an
   `external: true` network in `docker-compose.yml` and attach
   `pipeline_worker` to it, then address the gateway as
   `http://server:3000` (its network alias). No host port changes, no new
   exposure.

---

## §8 — Profile expansion

### 8.1 Saved map settings + find-ride preference

One table, two kinds, cardinality enforced by partial unique indexes
rather than by application code:

```sql
-- sql/043_user_preferences.sql
CREATE TABLE IF NOT EXISTS user_preferences (
    id          BIGSERIAL PRIMARY KEY,
    account_id  BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('saved_map_settings', 'find_ride_pref')),
    name        TEXT CHECK (name IS NULL OR (length(name) BETWEEN 1 AND 64)),
    settings    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT user_preferences_name_matches_kind CHECK (
        (kind = 'saved_map_settings' AND name IS NOT NULL) OR
        (kind = 'find_ride_pref'     AND name IS NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_prefs_map_name
    ON user_preferences (account_id, name) WHERE kind = 'saved_map_settings';
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_prefs_find_ride
    ON user_preferences (account_id)       WHERE kind = 'find_ride_pref';
```

`find_ride_pref` is **at most one**, upserted — "exactly one" isn't
expressible without inventing a default blob for every account, and a
fabricated preference is worse than an absent one. `PUT` creates or
replaces; `GET` returns `null` when unset. Flagging in case "exactly one"
meant "seed every account with a default".

Application-side guards (in `src/api_preferences.py`, new router):
max 50 saved entries per account, max 16 KB serialized per blob. Both
return 400 with the limit named. Blob contents are never interpreted by
the API — it is client-owned state, stored and handed back.

Endpoints (all `require_session`, caller-scoped — no cross-account read):

```
GET    /api/v1/profile/map-settings              list names + blobs
GET    /api/v1/profile/map-settings/{name}
PUT    /api/v1/profile/map-settings/{name}       create or replace
DELETE /api/v1/profile/map-settings/{name}
GET    /api/v1/profile/find-ride-pref            null when unset
PUT    /api/v1/profile/find-ride-pref            create or replace
DELETE /api/v1/profile/find-ride-pref
```

### 8.2 Royalty title, ruling colors, display name

```sql
-- sql/044_royalty_titles_and_ruling_colors.sql
CREATE TABLE IF NOT EXISTS royalty_titles (
    title       TEXT PRIMARY KEY,
    sort_order  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ruling_colors (
    hex         TEXT PRIMARY KEY CHECK (hex ~ '^#[0-9a-f]{6}$'),
    name        TEXT NOT NULL,          -- 'crimson-500'
    hue_family  TEXT NOT NULL,          -- picker grouping
    sort_order  INTEGER NOT NULL
);

ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS royalty_title       TEXT REFERENCES royalty_titles(title),
    ADD COLUMN IF NOT EXISTS ruling_color        TEXT REFERENCES ruling_colors(hex),
    ADD COLUMN IF NOT EXISTS ruling_border_color TEXT REFERENCES ruling_colors(hex),
    ADD COLUMN IF NOT EXISTS ruling_alpha        NUMERIC(3,2) NOT NULL DEFAULT 0.60
        CHECK (ruling_alpha BETWEEN 0.10 AND 1.00);

-- Fill and border must differ, and must be set together or not at all.
ALTER TABLE accounts ADD CONSTRAINT accounts_ruling_colors_coherent CHECK (
    (ruling_color IS NULL AND ruling_border_color IS NULL) OR
    (ruling_color IS NOT NULL AND ruling_border_color IS NOT NULL
     AND ruling_color <> ruling_border_color)
);
-- The PAIR is globally unique — two riders' territories can never render
-- identically. 128 colors -> 16 256 distinct pairs.
CREATE UNIQUE INDEX IF NOT EXISTS accounts_ruling_pair_key
    ON accounts (ruling_color, ruling_border_color)
    WHERE ruling_color IS NOT NULL;

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS display_name TEXT
    GENERATED ALWAYS AS (
        COALESCE(royalty_title || ' ', '') || username_adjective || username_emoji
    ) STORED;
```

Notes that matter:

* `display_name` is built from the **parts**, not from `public_username`
  — Postgres forbids a stored generated column referencing another
  generated column. Same NULL propagation as `public_username` (an
  un-backfilled account has neither).
* `display_name` is deliberately **not unique**. `public_username` keeps
  its uniqueness constraint and stays the identity key; the title is
  decoration, and two Kings with different animals are fine.
* **Palette generation.** 128 entries = 16 hues × 8 steps, generated in
  OKLCH for perceptually even spacing, converted to sRGB hex by a
  one-shot `scripts/gen_ruling_palette.py`, whose output is pasted into
  the migration as literals. Same convention as `sfw_adjectives` — seed
  data lives in SQL, the generator is provenance. Steps are chosen so any
  color works as a fill under alpha 0.6 over a map tile *and* as a 2 px
  border: nothing above ~0.92 or below ~0.25 lightness.
* **Alpha applies to the fill only**; the border renders opaque, because
  a border whose whole job is separating adjacent territories shouldn't
  be able to fade out. If riders should control both, split into
  `ruling_alpha` / `ruling_border_alpha` — one extra column, no other
  change.
* Royalty titles seeded from the operator's list (King, Queen, Emperor,
  Empress, Duke, Duchess, Majesty, Prince, Princess, Reverend, Emissary,
  The Speediest, The Cool Dude, The Coolest, Honorable, Sir, Dame, Humble
  Servant, His Highness, Her Highness, Their Highness, The Very
  Esteemed, …), `ON CONFLICT DO NOTHING` so the list can be extended by a
  later migration. Gendered pairs are seeded together and a neutral
  variant included wherever one exists, so no rider is forced into a
  gendered title.

Code:

* `src/api_profile.py` — `ProfileUpdate` gains `royalty_title`,
  `ruling_color`, `ruling_border_color`, `ruling_alpha`; `_profile_payload`
  returns those plus `display_name`. Colors move together (both or
  neither, both nullable to clear) — reuse the exact `_apply_coord_pair`
  idiom already there for home/work coords. `psycopg.errors.UniqueViolation`
  on `accounts_ruling_pair_key` → **409 "that color combination is
  already claimed"**, matching how the phone/email/username collisions
  are already surfaced. `ForeignKeyViolation` → 400 naming the bad value.
* `src/api_lexicon.py` — `GET /api/v1/royalty-titles`,
  `GET /api/v1/royalty-titles/search?q=`, `GET /api/v1/ruling-colors`.
  The colors response also carries `taken_pairs` (one entry per account
  with colors set — bounded by account count, not by 128²) so a picker
  can grey out claimed combinations instead of discovering them by 409.

Tests: `test_api_profile_royalty.py` (validation, coherence, 409 on a
taken pair, alpha bounds), `test_accounts_display_name_pg.py` (generated
column incl. the no-title and no-username cases),
`test_api_preferences.py` + `test_user_preferences_pg.py` (cardinality
indexes actually reject a second `find_ride_pref`), palette assertions
(≥128 rows, all distinct, all lowercase hex).

---

## §9 — SMS sign-in codes

### 9.1 A live bug this feature has to fix first

`auth_sessions.method` is `CHECK (method IN ('google', 'magic_link'))`
(`sql/012`) and was never widened. `src/api_auth.py:auth_code_verify`
mints with `method="email_code"`. Verified against production: the
constraint is still that pair, and `SELECT DISTINCT method FROM
auth_sessions` returns only `google` and `magic_link` — **the emailed
type-a-code sign-in door has never successfully minted a session**; it
raises `CheckViolation` → 500 at the last step, after burning the user's
code. `sql/042` widens the constraint to
`('google', 'magic_link', 'email_code', 'sms_code')` using the
read-the-live-definition guard shape from `sql/040`/`sql/041`. Worth
shipping on its own even if the rest of §9 slips.

### 9.2 Schema

```sql
-- sql/045_sms_login_codes.sql
ALTER TABLE login_codes ADD COLUMN IF NOT EXISTS phone_number TEXT;
ALTER TABLE login_codes ALTER COLUMN email DROP NOT NULL;
ALTER TABLE login_codes ADD CONSTRAINT login_codes_one_destination
    CHECK ((email IS NULL) <> (phone_number IS NULL));
CREATE INDEX IF NOT EXISTS idx_login_codes_phone_live
    ON login_codes (phone_number, created_at DESC) WHERE used_at IS NULL;

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS phone_verified_at TIMESTAMPTZ;
```

Reusing `login_codes` rather than adding a parallel table keeps one
attempt-cap/burn/prune implementation. `_hash_code` generalizes from
`(email, code)` to `(destination, code)` — still HMAC-keyed on
`session_secret()`, still bound to the destination so a code is only ever
valid for the number it was sent to.

### 9.3 Phone verification — a hole to close while we're here

`PUT /api/v1/profile` lets any signed-in rider write any E.164 string to
`accounts.phone_number` with no proof of ownership. Once SMS sign-in
exists, that unverified column becomes an authentication key: claim
someone's number first and their SMS sign-in lands in *your* account.

Fix, in this feature: `phone_verified_at` is set **only** by a successful
SMS code verification, and SMS sign-in matches only rows where it is
non-NULL. A number written through the profile PUT starts unverified and
is claimable by whoever proves the number. `GET /api/v1/profile` exposes
`phone_verified` so the UI can prompt.

### 9.4 Codes

* **`AA000AA`, the same format the email door already issues** — two
  letters, three digits, two letters, letters excluding I/O because they
  read as 1/0. `_generate_code`, `_normalize_code`, `_CODE_RE` and
  `_hash_code` are reused **verbatim**; SMS adds no code-generation code
  at all, and a rider who has used both doors types the same shape of
  thing either way. `_normalize_code` already strips spaces/hyphens and
  uppercases, so `ab-123-cd` typed off a phone screen verifies fine.
* TTL 10 minutes (`SMS_CODE_TTL_MINUTES = 10`); reuse `MAX_CODE_ATTEMPTS = 5`.
* Issuing a new code burns prior unused codes for that number — same
  single-live-target rule the email path uses.
* Entropy is 24⁴·10³ ≈ 3.3×10⁸, identical to the email door, so no
  format-driven compensation is needed. The SMS limits are tighter than
  email's for a different reason — **each send costs a real message on one
  physical handset**, so the constraint being protected is the operator's
  device and plan, not the code: **3 sends/hour per phone, 5 sends/hour
  per IP**, **10 verify attempts/hour per phone** (new; the email path
  only caps per IP), 30 verify/hour per IP, and a **global 250 sends/day**
  ceiling so a distributed attempt can't drain the plan. All via the
  existing `src/ratelimit.py:enforce`.
* US only: normalize `+1` / `1` / bare 10 digits to E.164, then validate
  NANP structure (`^\+1[2-9][0-8]\d[2-9]\d{6}$`). Anything else → 400
  "US numbers only". Lives in `src/accounts.py` next to
  `normalize_phone_number`, which stays the general E.164 normalizer for
  the profile column.

Message body, the specified template with the code substituted:

```
Use code AB123CD to login at denver.scooter.fyi
```

(The template was originally written with a six-digit example, `Use code
123456 to login at denver.scooter.fyi`; only the code token changes
shape. 46 characters — comfortably one SMS segment, and GSM-7 safe, so
no accidental split into two billed messages.)

### 9.5 Transport — `src/sms_gateway.py`

Mirrors `src/postmark.py` one-for-one: `sms_gateway_credentials()`
returns `None` when unconfigured (endpoint 503s, `/auth/config` reports
`sms_enabled: false`), `SmsGatewayError` on failure, mapped to 502 by the
route, and the send happens **after commit** so a gateway outage can't
roll back rate-limit events into a free retry loop.

Request shape pinned against the server's own `api/requests.http`
(android-sms-gateway/server @ master):

```
POST {SMS_GATEWAY_URL}/3rdparty/v1/messages
Authorization: Basic base64(user:pass)
{"textMessage": {"text": "Use code AB123CD to login at denver.scooter.fyi"},
 "phoneNumbers": ["+13035551234"],
 "ttl": 600}
```

`ttl: 600` matches the code's own expiry — an SMS that surfaces after the
code is dead is worse than no SMS. New env (`.env.example` + compose):
`SMS_GATEWAY_URL=http://server:3000/api`, `SMS_GATEWAY_USERNAME`,
`SMS_GATEWAY_PASSWORD`.

Delivery is **asynchronous** — the gateway returns `queued` and the
handset sends when it can. A 2xx therefore means "accepted", not
"delivered", and the endpoint's 202 says so. If the phone is offline the
code silently never lands; `deviceActiveWithin=240` on the query string
makes the gateway reject rather than queue for a handset unseen in 4
hours, which turns a silent failure into a 502 the rider can act on.

### 9.6 Endpoints

```
POST /api/v1/auth/sms/code          {phone_number}          -> 202 {sent: true}
POST /api/v1/auth/sms/code/verify   {phone_number, code}    -> {token, expires}
GET  /api/v1/auth/config                                    gains sms_enabled
```

Separate paths from the email code door rather than a polymorphic
`{email|phone}` body — the two have different validation, different
limits, and different failure modes, and the email path is load-bearing
today. `auth_code_request`/`auth_code_verify` refactor into shared
`_issue_code(destination_column, …)` / `_verify_code(…)` helpers so the
attempt-claim TOCTOU handling (the atomic `UPDATE … RETURNING attempts`)
has exactly one implementation.

New in `src/accounts.py`: `upsert_account_by_phone(cur, phone)` —
`INSERT … ON CONFLICT (phone_number) DO UPDATE SET last_login_at = NOW()`,
setting `phone_verified_at`, assigning a `public_username` on the insert
arm exactly like `upsert_account`. A phone with no account creates one
(mirrors the email doors); `accounts_email_or_phone_required` is
satisfied by the phone alone (`sql/025` already made email nullable).
Sessions mint with `method='sms_code'` and never carry the `admin` scope
— `session_scopes()` already restricts that to Google, no change needed.

Tests: `test_sms_login_code.py` (US validation table incl. rejected
non-US/short/N11 numbers, TTL, attempt cap, burn-on-reissue,
lowercase/hyphenated input normalizing to a valid code, exact message
text — code *generation* itself stays covered by the existing
`test_login_code.py`, since it is now literally the same function),
`test_sms_gateway.py` (request shape, auth header,
unconfigured → None, HTTP error → `SmsGatewayError`),
`test_auth_method_constraint_pg.py` (all four method values insert —
this is the regression test for §9.1), `test_phone_verification_pg.py`
(profile-written number stays unverified; sign-in claims it).

---

## §10 — Tracked ride reported fields

> **SHIPPED AS `sql/047_tracked_rides_reported_fields.sql`, AND NOT WITH THE DDL
> BELOW.** Two corrections, both made when the Ride Mode program picked this
> section up (`PLAN_RIDE_MODE_API.md` Phase A1):
>
> 1. **Number.** `046` was taken by `sql/046_comms_replies.sql` before this
>    section shipped. `047` is the file that exists; do not create a second one.
> 2. **Shape.** The DDL below inlines both CHECKs inside
>    `ADD COLUMN IF NOT EXISTS`, which Postgres skips *in its entirety* —
>    constraint included — once the column exists, so neither CHECK would ever
>    be installed on a database where the column arrived first, and re-running
>    the file could not repair it. `sql/047` therefore adds the columns bare and
>    installs `tracked_rides_reported_minutes_range` (conname-only guard, the
>    `sql/041` step-4 shape for a numeric bound) and
>    `tracked_rides_reported_plan_allowed` (value-checked guard, the
>    `sql/040`/`042` shape for an enumerated list) as separate named
>    constraints. The columns, bounds and vocabulary below are otherwise
>    exactly what shipped.

```sql
-- Illustrative only — see the note above. The file is
-- sql/047_tracked_rides_reported_fields.sql and it does NOT inline these CHECKs.
ALTER TABLE tracked_rides
    ADD COLUMN IF NOT EXISTS reported_minutes INTEGER
        CHECK (reported_minutes IS NULL OR reported_minutes BETWEEN 0 AND 1440),
    ADD COLUMN IF NOT EXISTS reported_plan TEXT
        CHECK (reported_plan IS NULL OR reported_plan IN ('resident', 'visitor', 'equity'));
```

`reported_cost` is **not** added — `total_cost_cents` is that field, and
two rider-reported cost columns could disagree about what someone paid.
Per decision above.

* `reported_minutes` is what the operator's app told the rider, stored as
  reported. It is *not* reconciled against
  `user_reported_ended_at - started_at`; the whole point of a reported
  field is that it can differ from what we observed, and the comparison
  is an analytics question, not a validation one. Capped at 24 h for the
  same reason the distance cap exists — a number we won't stand behind
  shouldn't enter the table.
* `reported_plan` reuses the `('resident','visitor','equity')` vocabulary
  from `accounts.rate_plan` and `rides.rate_plan` — **confirmed by the
  operator, 2026-07-28**: it is the rate-plan tier, not a Veo pass
  product. The CHECK stays. Note the asymmetry this creates on purpose:
  `accounts.rate_plan` is the plan the rider says they are *on*,
  `tracked_rides.reported_plan` is the plan they say they *rode under*,
  and the two can legitimately disagree on any given ride.

Code: `EndRideIn` gains both fields; the `UPDATE` in
`end_tracked_ride` sets them; `_RIDE_COLS` / `_row_to_ride` return them.
Nothing about the existing close-out logic (distance, clamping, points)
changes — these are inert stored facts. `GET /api/v1/tracked-rides*`
payloads gain the two keys.

Tests: extend `test_api_tracked_rides_validation.py` (bounds, plan
vocabulary, absence stays NULL) and `test_tracked_rides_lifecycle_pg.py`
(round-trip through PATCH `/end`).

---

## §11 — H3 r8 area leader report

"All r8 hexagons in the local network, with the user who earned the most
points there in the last four weeks, recalculated."

### 11.1 Universe and window

* **Local network** = `spatial_status = 'denver_core'` observations —
  the buffered Denver polygon that `src/compute.py` makes authoritative
  each cycle, not the rough bbox.
* **Cells** = r8 cells with observed devices **or** points history:
  `SELECT DISTINCT h3_8_index FROM device_history` ∪
  `SELECT DISTINCT current_h3_8_index FROM device_state` ∪
  `SELECT DISTINCT h3_8_index FROM user_points`. 720 cells all-time
  today. `user_points` **already stores `h3_8_index`** (`sql/028`), so no
  new column and no backfill — this is the main dividend of moving from
  r10 to r8.
* **Window** = trailing 28 days ending at the run's start, stamped into
  the run row so the report says what it measured.
* `device_history` is 7.3M rows / 2.2 GB and has no r8 index. The
  `DISTINCT` is a seq scan of a few seconds, once a day, off-peak —
  deliberately cheaper than building and maintaining a ~150 MB index at
  boot in every environment for one daily query.

### 11.2 Schema

> **NUMBER CORRECTION: this migration is `sql/048_h3_r8_area_leaders.sql`.**
> `046` and `047` were both taken before §11 shipped (`046_comms_replies.sql`,
> then `047_tracked_rides_reported_fields.sql` for §10 above), and §11 itself is
> now delivered by `PLAN_RIDE_MODE_API.md` **Phase A4**, which owns `sql/048`.
> Creating `sql/047_h3_r8_area_leaders.sql` would collide with a file that
> already exists and holds something else.

```sql
-- sql/048_h3_r8_area_leaders.sql
CREATE TABLE IF NOT EXISTS h3_r8_area_report (
    h3_8_index        BIGINT PRIMARY KEY,
    has_devices       BOOLEAN NOT NULL,
    has_points        BOOLEAN NOT NULL,
    total_points      INTEGER NOT NULL DEFAULT 0,
    distinct_earners  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS h3_r8_area_leaders (
    h3_8_index      BIGINT NOT NULL REFERENCES h3_r8_area_report(h3_8_index) ON DELETE CASCADE,
    rank            SMALLINT NOT NULL CHECK (rank BETWEEN 1 AND 3),
    account_id      BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    points          INTEGER NOT NULL CHECK (points > 0),
    first_point_at  TIMESTAMPTZ NOT NULL,   -- tie-break provenance
    PRIMARY KEY (h3_8_index, rank)
);
CREATE TABLE IF NOT EXISTS h3_r8_area_leader_runs (
    id            BIGSERIAL PRIMARY KEY,
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    window_start  TIMESTAMPTZ NOT NULL,
    window_end    TIMESTAMPTZ NOT NULL,
    cell_count    INTEGER NOT NULL,
    led_cells     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_points_h3_8_created
    ON user_points (h3_8_index, created_at DESC);
```

**Top 3 per cell, not top 1.** Privacy is applied at *read* time
(`show_in_leaderboards` / `show_public_username` can flip at any moment
and must take effect immediately, not at the next daily run), so the
report has to carry runners-up for the reader to fall through to.
Storing only the winner would mean a rider opting out blanks a hex until
tomorrow.

**Tie-break** (deterministic, documented in the module): `points DESC`,
then `first_point_at ASC` — whoever got there first holds the territory —
then `account_id ASC` as a final total order. Only `status = 'confirmed'`
ledger rows count.

**Replacement, not accumulation:** one transaction does
`DELETE FROM h3_r8_area_report` (cascading the leaders) → `INSERT` →
run row. Same idiom as `src/daily_trips.py:compute_for_date`, so a re-run
or backfill always reflects current data.

### 11.3 Job

`src/area_leaders.py:recompute(window_days=28)`, exposed as
`python -m src.cli recompute_area_leaders`, crontab:

```
# H3 r8 area leader report: trailing 28 days, full replace. Runs after the
# 9:00 daily rollups so it never contends with them.
15 9 * * * cd /app && python -m src.cli recompute_area_leaders
```

### 11.4 Read endpoints

```
GET /api/v1/leaderboard/map        rider-facing choropleth feed
GET /api/v1/private/area-leaders   admin: full ranks, ties, account ids
```

`/leaderboard/map` returns, per cell, the highest-ranked **still-eligible**
leader joined live to `accounts` for `display_name`, `ruling_color`,
`ruling_border_color`, `ruling_alpha` — so a re-rolled username or a
recolored territory shows up instantly without waiting for a recompute.
Cells with no eligible leader return `leader: null` and render uncolored.
Cell keys are canonical **h3 strings** via `h3.int_to_str`, never raw
64-bit ints — `src/api_h3.py` documents why (JS `MAX_SAFE_INTEGER`).
ETag keyed on the latest `h3_r8_area_leader_runs.computed_at`, with the
same `public, max-age=600` header `/api/v1/h3/aggregates` uses.

A rider with no `ruling_color` set leads their cell with a `null` color —
the frontend needs a neutral default; that's a frontend decision, and the
API says `null` rather than inventing one.

**Expectation-setting:** production has 8 `user_points` rows total. At
launch essentially every one of the ~720 cells will report `leader: null`.
This is correct behavior, not a broken job.

Tests: `test_area_leaders_logic.py` (tie-break ordering, window
boundaries, confirmed-only), `test_area_leaders_pg.py` (universe union,
full-replace idempotence, cascade), `test_api_leaderboard_map.py`
(privacy fall-through to rank 2/3, all-opted-out → null, ETag/304, h3
string keys).

---

## Sequencing

Four independent PRs; only the last has a cross-dependency.

| PR | Contents | Depends on |
|---|---|---|
| 1 | **Done** — `sql/042` §9.1 constraint fix + `tests/test_auth_method_constraint_pg.py` | — (shipped first, it was a live 500) |
| 2 | §8 profiles: `sql/043`, `sql/044`, preferences router, lexicon endpoints, palette generator | — |
| 3 | §9 SMS: `sql/045`, `sms_gateway.py`, auth endpoints, phone verification, compose network | PR 1 |
| 4 | §10 tracked-ride fields | — |
| 5 | §11 report + `/leaderboard/map` | PR 2 (reads the color columns) |

PRs 2, 3 and 4 can run fully in parallel. PR 5's rollup job and schema
are independent of PR 2; only its read endpoint needs the color columns,
so it can start immediately and merge after.

Per-PR doc duties, matching existing convention: endpoint tables in
`README.md`, full request/response shapes and error codes in `API.md`,
new env in `.env.example` **and** `docker-compose.yml`, a row in the
`API_REQUIREMENTS.md` status table, and the crontab comment block for
PR 5.

## Open items

1. ~~`reported_plan` vocabulary~~ — **settled 2026-07-28**: rate-plan tier.
2. "Exactly one `find_ride_pref`" implemented as at-most-one + upsert; no
   account is seeded with a default blob (§8.1).
3. Alpha governs the fill only; the border stays opaque (§8.2).
4. SMS gateway API username/password must come from the Android app
   before §9 can be tested end-to-end (prereq 1).

## Done

**PR 1 — emailed-code door fix (§9.1).** `sql/042_auth_session_methods.sql`
widens `auth_sessions.method` to
`('google','magic_link','email_code','sms_code')` with the
read-the-live-definition guard, pre-authorizing the SMS door in the same
statement so the list has one owner.
`tests/test_auth_method_constraint_pg.py` covers the full emailed-code
round trip (request → verify → a stored session row actually carrying
`method='email_code'`), every mintable value, rejection of an unknown
value, and replay safety.

Verified against a throwaway Postgres 15, not production: 4/4 pass with
the migration, and with it withheld on a fresh database the round-trip
and constraint tests fail — so the test demonstrably catches the bug
rather than merely passing alongside the fix. Full suite: 684 passed.
