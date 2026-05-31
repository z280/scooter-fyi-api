# Instructions for the denver.scooter.fyi agent

You are working on the **denver.scooter.fyi** frontend (a Cloudflare Pages
site). Your task is to add **GitHub-OAuth-based sign-in** to the site so
that authorized users can call private endpoints on `data.scooter.fyi`
that expose non-anonymized scooter data.

You have **no prior context on this work** — this document is
self-contained.

---

## Context (read this first)

- `data.scooter.fyi` is a FastAPI backend (separate repo, `veo-audit`)
  that polls Veo's GBFS feed every 10 minutes and serves both public and
  private endpoints.
- Public endpoints are unauthenticated and already in use by this site.
- A new set of **private endpoints** has been added under `/api/v1/private/*`
  that require a `Authorization: Bearer <token>` header. Those endpoints
  expose:
  - `vehicle_plate` — the raw printed plate number on each scooter
  - `first_observed_at_location` — how long the scooter has been parked
  - `number_failed_starts` — bike_id rotations without movement
  - full per-scooter location history
- Bearer tokens are minted by the backend's `/map-auth/denver` flow, which
  does GitHub OAuth against a **separate** OAuth app gated by the
  `scooter-club` GitHub org.
- Tokens are valid for **8 hours**.
- The backend has already been configured to allow `denver.scooter.fyi`
  (and its Pages preview URLs) as a valid `return` origin for the flow.
  No backend change is required for you to do this work.

---

## The flow, end-to-end

```
[user clicks Sign In on denver.scooter.fyi]
        |
        v
location.assign("https://data.scooter.fyi/map-auth/denver?return=https://denver.scooter.fyi/auth-callback?next=/map")
        |
        v
[data.scooter.fyi redirects to GitHub OAuth]
        |
        v
[user approves; GitHub redirects back to data.scooter.fyi/map-auth/callback]
        |
        v
[backend verifies scooter-club membership, mints token, redirects browser to:]
        |    https://denver.scooter.fyi/auth-callback#token=...&expires=...
        v
[/auth-callback page (THIS SITE) reads location.hash, stores in sessionStorage, scrubs URL, navigates to ?next=]
        |
        v
[user lands back at /map, now authenticated; map-auth.js apiFetch() attaches the bearer]
```

Critical properties:
- The token **only appears in the URL fragment** (`#`), never the query string.
- The token is **stored in `sessionStorage`** under key `scooter_fyi.map_auth`
  as JSON: `{token, expires, issued_at}`.
- The token never appears in HTTP logs, referer headers, or the
  browser's address bar after the callback page scrubs it.

---

## Files to drop in

This directory contains two ready-to-use files. Copy them into the
denver.scooter.fyi site verbatim:

### 1. `auth-callback.html` → deploy at `/auth-callback`

Static HTML that handles the OAuth return. Inline JS only, no deps.
Reads `location.hash`, persists to sessionStorage, scrubs the URL, and
redirects to `?next=` (which it validates as same-origin to prevent
open-redirect).

Place at the site root so it serves from `https://denver.scooter.fyi/auth-callback`.
If your build pipeline strips `.html` extensions, the file name is fine
as-is — just ensure the path resolves.

### 2. `map-auth.js` → import wherever you talk to the private API

ES module exporting:

| Export | Purpose |
|---|---|
| `getAuth()` | Returns `{token, expires, issued_at}` or `null`. Auto-clears expired tokens. |
| `isAuthenticated()` | Convenience wrapper around `getAuth()`. |
| `signIn(nextPath = "/")` | Redirects to data.scooter.fyi's OAuth init. `nextPath` is where to land after success — must start with `/`. |
| `signOut()` | Server-side revoke + local clear. Best-effort: works even if network fails. |
| `apiFetch(path, init)` | `fetch()` wrapper that attaches bearer. Throws on 401 (with `err.code === "TOKEN_REJECTED"`). Returns parsed JSON. |

---

## What you need to build

### A. Sign-in affordance

Add a "Sign in" UI element somewhere prominent (suggested: top-right of
the navigation bar). When clicked:

```js
import { signIn } from "./map-auth.js";
signIn(location.pathname + location.search);  // come back to where we were
```

### B. Logged-in indicator

When `isAuthenticated()` is true, show:
- The authenticated user (you have `expires` from `getAuth()`, but you do
  NOT have the GitHub username from the client — if you want to display
  it, call `apiFetch("/api/v1/private/devices/lookup?plate=does-not-exist")`
  once after sign-in; that endpoint returns a 404 but the response
  metadata for the request shows `viewed_by: <login>`. Or simpler: just
  show "Signed in · expires in 7h 23m" without the username.)
- A countdown to expiry (re-render every minute).
- A "Sign out" button calling `signOut()` then `location.reload()`.

### C. Wire private API calls

Replace any existing `fetch("https://data.scooter.fyi/api/v1/...")`
calls that target the new private data with `apiFetch()`:

```js
import { apiFetch, isAuthenticated, signIn } from "./map-auth.js";

async function loadEnhancedMap() {
  if (!isAuthenticated()) {
    return loadPublicMap();
  }
  try {
    const geo = await apiFetch("/api/v1/private/devices/current");
    renderMapWithExtraFields(geo);   // shows vehicle_plate, dwell time, failed starts
  } catch (err) {
    if (err.code === "TOKEN_REJECTED") {
      // Token died mid-session — prompt re-auth.
      signIn(location.pathname + location.search);
      return;
    }
    console.error(err);
    return loadPublicMap();
  }
}
```

### D. Graceful fallback

Anyone NOT signed in must still get the existing public map experience.
Sign-in is **additive** — it unlocks extra data, not a gate to the site.

---

## Endpoints you can call once authenticated

All require `Authorization: Bearer <token>` (handled by `apiFetch`):

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/private/devices/current` | Full fleet snapshot. Same shape as public `/api/v1/devices/current` plus `vehicle_plate`, `first_observed_at_location`, `number_failed_starts`, `first_ever_observed_at`. |
| `GET /api/v1/private/devices/lookup?plate=1025543` | Resolve a physical plate → identifier + current state. |
| `GET /api/v1/private/devices/lookup?vehicle_identifier=8c4a...` | Inverse — resolve identifier → plate + state. |
| `GET /api/v1/private/devices/{vehicle_identifier}/history?since=ISO&until=ISO` | Time-ordered list of stops (positions the scooter dwelt at). |
| `POST /map-auth/logout` | Revoke the current token. `signOut()` calls this for you. |

Query-param details, response shapes, and field meanings are in the
backend's `API.md`. If a field's meaning is unclear, ask before guessing.

---

## Requirements & gotchas

### MUST
- **sessionStorage, not localStorage.** The token must die when the tab
  closes. `sessionStorage` is correct; `localStorage` is forbidden.
- **Storage key is exactly `scooter_fyi.map_auth`.** Do not rename. It's
  shared between `auth-callback.html` and `map-auth.js`.
- **Scrub the fragment after reading.** `auth-callback.html` already does
  this via `history.replaceState`. Don't break it.
- **Validate `?next=` against same-origin.** Already done in the callback.
  If you modify the callback page, preserve the `next[0] === "/" && next.indexOf("//") !== 0` check — without it, an attacker can craft a sign-in link that redirects to evil.example.com after success.
- **Never log the token.** Don't `console.log(auth.token)`, don't put it
  in error messages sent to Sentry/etc., don't include it in URLs.

### MUST NOT
- **Don't introduce a build step just for these two files.** They're
  hand-written to work as static drops. If your site already has a
  bundler, the import statements work natively in ES modules — no
  transpilation needed.
- **Don't pre-fetch the token from any other URL.** The fragment handoff
  is the only legitimate path.
- **Don't cache `apiFetch` responses across sign-in/sign-out boundaries.**
  Make sure your data-loading code re-runs after auth state changes.

### Watch out for
- **Token expiry mid-session:** if the user keeps a tab open for 8+
  hours, `apiFetch` will start throwing `TOKEN_REJECTED`. Handle this in
  every call site, or wrap your data-loading layer to catch it
  centrally.
- **CORS:** the backend already allows your origin (`denver.scooter.fyi`
  + `*.denver-scooter-fyi.pages.dev`) for both GET and POST. If you see
  CORS errors, you're calling from an unrecognized origin — confirm
  what URL you're testing from.
- **Preview deploys:** the backend allows `*.denver-scooter-fyi.pages.dev`
  as return origins, so PR previews can sign in. If you set up a fresh
  custom domain that's NOT one of those, sign-in will 403 with
  "return origin not allowed" until the backend config is updated.

---

## Test checklist

After implementing, verify:

- [ ] Unauthenticated visit to `/map` shows the public map exactly as before.
- [ ] Clicking "Sign in" → GitHub → returns to `/auth-callback` → final landing on `/map` (or wherever `nextPath` was set).
- [ ] After sign-in, `sessionStorage.getItem("scooter_fyi.map_auth")` shows valid JSON; address bar shows no `#token=`.
- [ ] After sign-in, the map shows additional fields (plate, dwell time, etc.).
- [ ] Refreshing the tab keeps the user signed in (sessionStorage persists across reload).
- [ ] Closing the tab → reopening: user is signed out (sessionStorage gone).
- [ ] Clicking "Sign out": local data cleared, page reflects unauthenticated state, server-side revoke happened (check `/admin/map-tokens` on data.scooter.fyi — token shows `revoked_at`).
- [ ] After 8h: next `apiFetch` throws, UI prompts re-auth.
- [ ] A user **not** in the `scooter-club` GitHub org is rejected with 403 at the OAuth callback and never gets a token.
- [ ] A malformed return URL (e.g. `?return=https://evil.com`) is rejected by the backend with 403 — confirm this with curl before deploying.

---

## Implementation order suggestion

1. Drop both files in as-is. Don't modify them yet.
2. Wire `signIn` to a button. Confirm the flow lands you back with a token in sessionStorage.
3. Add one `apiFetch` call to verify the bearer travels correctly. `/api/v1/private/devices/lookup?plate=1025543` is a good probe — small response, easy to eyeball.
4. Add the logged-in indicator + sign-out button.
5. Refactor the existing map fetch to use `apiFetch` with public fallback.
6. Add the expiry countdown and 401-handling polish.

Do not skip step 2's manual verification. If sign-in doesn't put a token
into sessionStorage, all later steps are debugging a phantom.

---

## Questions you might have, answered

**Q: Can I use localStorage instead of sessionStorage?**
A: No. The token expiring with the tab is a load-bearing privacy
property. Use sessionStorage.

**Q: Can I make the token long-lived (weeks)?**
A: No — TTL is enforced on the server side (`api_tokens.expires_at`).
You'd need to coordinate with the backend agent.

**Q: Can I show the GitHub username in the UI?**
A: Not directly from the callback — the username never reaches your code.
You can call a private endpoint and read `metadata.viewed_by` from the
response. Or skip the username display entirely; "Signed in · 7h 23m
left" is enough.

**Q: What if I want server-side rendering / SSG for the authed map?**
A: You can't. The token is browser-only by design. The map render must
be client-side.

**Q: Where do I report bugs in this auth flow?**
A: Open an issue against the `veo-audit` repo and tag it `map-auth`.
Include the full URL of the failed redirect (with fragment) and any
response from `/map-auth/callback`.

---

## What's in this directory

- `auth-callback.html` — deploy at `/auth-callback`
- `map-auth.js` — import wherever you make private API calls
- `AGENT_INSTRUCTIONS.md` — this file
