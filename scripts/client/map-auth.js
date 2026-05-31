// map-auth.js — minimal helper for talking to data.scooter.fyi private endpoints.
//
// Pair this with /auth-callback.html. After the OAuth flow, the callback
// page stashes {token, expires} under sessionStorage["scooter_fyi.map_auth"];
// this module reads it back and attaches it as a Bearer to fetch() calls.
//
// Drop into the denver.scooter.fyi site (or any client that needs map-auth).
// Zero dependencies, no build step required.

const STORAGE_KEY = "scooter_fyi.map_auth";
const API_BASE = "https://data.scooter.fyi";
const SIGNIN_URL = API_BASE + "/map-auth/denver";

/** Read the stashed auth blob; returns null if missing or expired. */
export function getAuth() {
  let raw;
  try { raw = sessionStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
  if (!raw) return null;
  let parsed;
  try { parsed = JSON.parse(raw); } catch (e) { return null; }
  if (!parsed || !parsed.token || !parsed.expires) return null;
  if (new Date(parsed.expires) <= new Date()) {
    // Expired — clear so we don't keep sending dead tokens.
    try { sessionStorage.removeItem(STORAGE_KEY); } catch (e) {}
    return null;
  }
  return parsed;
}

/** True if a non-expired token is present. */
export function isAuthenticated() {
  return getAuth() !== null;
}

/** Kick off the OAuth flow. After GitHub returns, the user lands on
 *  /auth-callback (this site) and is then redirected to `nextPath`. */
export function signIn(nextPath = "/") {
  const ret = encodeURIComponent(
    location.origin + "/auth-callback?next=" + encodeURIComponent(nextPath)
  );
  location.assign(SIGNIN_URL + "?return=" + ret);
}

/** Best-effort server-side revoke + local clear. */
export async function signOut() {
  const auth = getAuth();
  if (auth) {
    try {
      await fetch(API_BASE + "/map-auth/logout", {
        method: "POST",
        headers: { "Authorization": "Bearer " + auth.token },
      });
    } catch (e) { /* network error — we still clear locally */ }
  }
  try { sessionStorage.removeItem(STORAGE_KEY); } catch (e) {}
}

/**
 * fetch wrapper that injects the bearer token. Throws on 401 so the caller
 * can redirect to signIn(). Returns the parsed JSON body on success.
 *
 *   const data = await apiFetch("/api/v1/private/devices/current");
 */
export async function apiFetch(path, init = {}) {
  const auth = getAuth();
  if (!auth) {
    const err = new Error("not authenticated");
    err.code = "NO_AUTH";
    throw err;
  }
  const headers = new Headers(init.headers || {});
  headers.set("Authorization", "Bearer " + auth.token);
  const resp = await fetch(API_BASE + path, { ...init, headers });
  if (resp.status === 401) {
    try { sessionStorage.removeItem(STORAGE_KEY); } catch (e) {}
    const err = new Error("token rejected");
    err.code = "TOKEN_REJECTED";
    throw err;
  }
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    const err = new Error("HTTP " + resp.status + ": " + text.slice(0, 200));
    err.code = "HTTP_ERROR";
    err.status = resp.status;
    throw err;
  }
  return resp.json();
}
