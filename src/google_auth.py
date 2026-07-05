"""Google ID token verification for POST /api/v1/auth/google (§2.2).

Verifies a Google Identity Services credential (an RS256 JWT) locally
against Google's published JWKS — no per-request Google API call. The
JWKS is fetched once and cached for the lifetime advertised by Google's
Cache-Control header (fallback 1 hour); an unknown `kid` forces one
immediate refetch to handle Google's key rotation without waiting out
the cache.

Checks enforced (all required, per API_REQUIREMENTS.md §2.2):
    * signature against Google's JWKS (RS256 only)
    * iss ∈ {https://accounts.google.com, accounts.google.com}
    * aud == our OAuth client id (GOOGLE_OAUTH_CLIENT_ID)
    * exp (60 s leeway for clock skew)
    * email present and email_verified is True
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx
from authlib.jose import JsonWebToken, KeySet, JsonWebKey
from authlib.jose.errors import JoseError

log = logging.getLogger(__name__)

_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
_DEFAULT_TTL_S = 3600
_LEEWAY_S = 60

_jwt = JsonWebToken(["RS256"])

# (key_set, fetched_at_monotonic, ttl_seconds)
_cache: tuple[KeySet, float, float] | None = None


class GoogleAuthError(Exception):
    """Any verification failure. Message is safe to surface in a 401 detail."""


def _fetch_jwks() -> tuple[KeySet, float]:
    r = httpx.get(_JWKS_URL, timeout=10.0)
    r.raise_for_status()
    ttl = _DEFAULT_TTL_S
    m = re.search(r"max-age=(\d+)", r.headers.get("cache-control", ""))
    if m:
        ttl = int(m.group(1))
    return JsonWebKey.import_key_set(r.json()), ttl


def _key_set(force_refresh: bool = False) -> KeySet:
    global _cache
    now = time.monotonic()
    if not force_refresh and _cache is not None:
        keys, fetched_at, ttl = _cache
        if now - fetched_at < ttl:
            return keys
    keys, ttl = _fetch_jwks()
    _cache = (keys, now, ttl)
    return keys


def verify_google_id_token(credential: str, audience: str) -> dict[str, Any]:
    """Return the verified claims for a Google ID token.

    Raises GoogleAuthError on any failure. The returned dict is the full
    claim set; callers use `email` (and may log `sub`).
    """
    if not audience:
        raise GoogleAuthError("google sign-in not configured (no client id)")

    claims = None
    for attempt in ("cached", "refreshed"):
        try:
            claims = _jwt.decode(
                credential,
                key=_key_set(force_refresh=(attempt == "refreshed")),
                claims_options={
                    "iss": {"essential": True, "values": _ISSUERS},
                    "aud": {"essential": True, "value": audience},
                    "exp": {"essential": True},
                },
            )
            break
        except ValueError as e:
            # authlib raises ValueError("Invalid JSON Web Key Set") /
            # unknown-kid errors before signature checking — refetch once,
            # Google rotates keys frequently.
            if attempt == "refreshed":
                raise GoogleAuthError(f"credential verification failed: {e}") from e
        except JoseError as e:
            raise GoogleAuthError(f"credential verification failed: {e.error}") from e

    try:
        claims.validate(leeway=_LEEWAY_S)
    except JoseError as e:
        raise GoogleAuthError(f"credential invalid: {e.error}") from e

    email = claims.get("email")
    if not email:
        raise GoogleAuthError("credential has no email claim")
    if claims.get("email_verified") is not True:
        raise GoogleAuthError("email not verified by Google")

    return dict(claims)
