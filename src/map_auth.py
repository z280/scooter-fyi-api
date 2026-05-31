"""Map-auth flow — issues short-lived bearer tokens for elevated map access.

This is a SECOND GitHub OAuth flow, distinct from src/auth.py (which gates
the admin panel). It uses a separate GitHub OAuth app + a separate org
allowlist, so the set of users who can view non-anonymized device data
is decoupled from the set of system admins.

Flow:
    1. Browser hits  GET /map-auth/{system}?return={url}
       — `system` is a stable name (currently only "denver" is recognized).
       — `return` MUST exactly match one of map_auth.allowed_return_origins
         (after URL parsing — path can vary, scheme+host must match).
    2. We stash (system, return) in the session cookie and redirect to GitHub.
    3. GitHub bounces back to /map-auth/callback with a code.
    4. We verify org membership in map_auth.allowed_github_orgs.
    5. We mint a 32-byte random token, store sha256(token) in api_tokens
       with an 8h expiry, then redirect the browser to:
           {return}#token={raw}&expires={iso8601}
       The raw token only ever appears in the URL fragment — never query
       string, never server log.

The token can be presented on subsequent requests as
    Authorization: Bearer {raw}
and is verified by require_map_user() in src/map_auth_dep.py.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
import urllib.parse
from hashlib import sha256
from typing import Any

import httpx
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import RedirectResponse

from .config import allowed_map_github_orgs, load, map_oidc_credentials
from .pg import connection

log = logging.getLogger(__name__)

# Separate OAuth instance from src/auth.py to avoid cross-talk: the admin
# provider and the map provider have different credentials and different
# allowed-org sets, and conflating them in one OAuth() singleton would risk
# a config bug authenticating an admin OAuth login as a map user (or vice
# versa).
_oauth: OAuth | None = None

# Recognized system names. Adding "boulder" / etc. later is one-line.
_KNOWN_SYSTEMS = {"denver"}

router = APIRouter()


def _init_oauth() -> bool:
    global _oauth
    creds = map_oidc_credentials()
    if not creds:
        log.warning("MAP_OIDC_CLIENT_ID/SECRET unset — /map-auth/* will refuse.")
        return False
    if _oauth is None:
        _oauth = OAuth()
        _oauth.register(
            name="github_map",
            client_id=creds["client_id"],
            client_secret=creds["client_secret"],
            access_token_url="https://github.com/login/oauth/access_token",
            authorize_url="https://github.com/login/oauth/authorize",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "read:user read:org"},
        )
    return True


def _validate_return_url(raw: str) -> str:
    """Match `return` against the configured allowlist by (scheme, host).

    The full URL is preserved (including path/query) so the caller's client
    can be deep-linked back to the page they came from, but a malicious
    caller can't redirect to evil.example.com.
    """
    allowed = load().map_auth.allowed_return_origins
    if not allowed:
        raise HTTPException(503, "map_auth.allowed_return_origins not configured")
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError as e:
        raise HTTPException(400, f"bad return URL: {e}")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "return URL must be absolute http(s)")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in allowed:
        # Also try pattern allowance — same patterns as CORS for Pages previews.
        import re
        cfg = load()
        if not any(re.match(p, origin) for p in cfg.cors_origin_patterns):
            raise HTTPException(403, f"return origin not allowed: {origin}")
    return raw


def _user_orgs(token: str) -> set[str]:
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    with httpx.Client(timeout=10.0) as client:
        r = client.get("https://api.github.com/user/orgs", headers=headers)
        r.raise_for_status()
        return {o.get("login", "").lower() for o in r.json() if o.get("login")}


def _user_profile(token: str) -> dict[str, Any]:
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    with httpx.Client(timeout=10.0) as client:
        r = client.get("https://api.github.com/user", headers=headers)
        r.raise_for_status()
        return r.json()


@router.get("/map-auth/{system}", include_in_schema=False)
async def map_auth_initiate(system: str, request: Request, return_: str | None = None):
    # FastAPI strips trailing underscore from query params — `return_` here
    # means the literal `?return=...`. Aliased below to keep the API natural.
    return_url = request.query_params.get("return") or return_
    if not return_url:
        raise HTTPException(400, "missing required ?return=... parameter")
    if system not in _KNOWN_SYSTEMS:
        raise HTTPException(404, f"unknown system '{system}' (known: {sorted(_KNOWN_SYSTEMS)})")
    _validate_return_url(return_url)
    if not _init_oauth():
        raise HTTPException(503, "map auth not configured (missing MAP_OIDC_CLIENT_ID/SECRET)")

    # Stash intent in the session — survives the GitHub round-trip.
    request.session["map_auth_pending"] = {
        "system": system,
        "return_url": return_url,
    }
    callback = load().map_auth.callback_url
    return await _oauth.github_map.authorize_redirect(request, callback)


@router.get("/map-auth/callback", include_in_schema=False)
async def map_auth_callback(request: Request):
    if not _init_oauth():
        raise HTTPException(503, "map auth not configured")
    pending = request.session.pop("map_auth_pending", None)
    if not pending:
        raise HTTPException(400, "no pending map_auth session — start at /map-auth/{system}")

    try:
        token = await _oauth.github_map.authorize_access_token(request)
    except OAuthError as e:
        raise HTTPException(401, f"OAuth failed: {e.description}")
    access = token.get("access_token")
    if not access:
        raise HTTPException(401, "no access_token in OAuth response")

    allowed = {o.lower() for o in allowed_map_github_orgs()}
    if not allowed:
        raise HTTPException(503, "no orgs configured in MAP_AUTH_ALLOWED_GITHUB_ORGS")
    user_orgs = _user_orgs(access)
    intersecting = user_orgs & allowed
    if not intersecting:
        raise HTTPException(
            403,
            f"user is not a member of any allowed map-auth org ({sorted(allowed)})",
        )

    profile = _user_profile(access)
    login = profile.get("login")
    if not login:
        raise HTTPException(500, "GitHub profile has no login")

    # Mint a token. 32 bytes of entropy → 256 bits, way more than enough.
    raw = secrets.token_urlsafe(32)
    digest = sha256(raw.encode("utf-8")).hexdigest()
    ttl = load().map_auth.token_ttl_hours
    expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=ttl)
    client_host = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO api_tokens (
                    token_sha256, github_login, github_user_id, github_orgs,
                    expires_at, issued_ip, user_agent
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    digest, login, profile.get("id"),
                    sorted(intersecting), expires_at, client_host, user_agent,
                ),
            )
        conn.commit()

    log.info(
        "map_auth: minted token for %s (orgs=%s, system=%s, expires=%s)",
        login, sorted(intersecting), pending["system"], expires_at.isoformat(),
    )

    # Hand off via URL fragment so the raw token never lands in HTTP logs
    # or referrer headers. The static JS at the return URL is responsible
    # for reading location.hash and scrubbing the URL.
    return_url = pending["return_url"]
    sep = "&" if "#" in return_url else "#"
    redirect_to = f"{return_url}{sep}token={raw}&expires={expires_at.isoformat()}"
    return RedirectResponse(url=redirect_to, status_code=302)


@router.post("/map-auth/logout", include_in_schema=False)
def map_auth_logout(request: Request):
    """Revoke the presented bearer token. Idempotent."""
    authz = request.headers.get("Authorization", "")
    if not authz.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    raw = authz.split(" ", 1)[1].strip()
    digest = sha256(raw.encode("utf-8")).hexdigest()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE api_tokens SET revoked_at = NOW() "
                "WHERE token_sha256 = %s AND revoked_at IS NULL",
                (digest,),
            )
        conn.commit()
    return {"revoked": True}
