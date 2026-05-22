"""GitHub OAuth (acts as OIDC IdP) for the admin panel."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import HTTPException, Request
from starlette.responses import RedirectResponse

from .config import allowed_github_orgs, oidc_credentials

log = logging.getLogger(__name__)

oauth = OAuth()


def init_oauth() -> bool:
    """Register the GitHub provider. Returns True if credentials are present."""
    creds = oidc_credentials()
    if not creds:
        log.warning("OIDC credentials absent — admin panel will reject all requests.")
        return False

    if "github" not in oauth._clients:  # type: ignore[attr-defined]
        oauth.register(
            name="github",
            client_id=creds["client_id"],
            client_secret=creds["client_secret"],
            access_token_url="https://github.com/login/oauth/access_token",
            authorize_url="https://github.com/login/oauth/authorize",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "read:user read:org"},
        )
    return True


def _user_orgs(token: str) -> set[str]:
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    with httpx.Client(timeout=10.0) as client:
        r = client.get("https://api.github.com/user/orgs", headers=headers)
        r.raise_for_status()
        return {o.get("login", "").lower() for o in r.json() if o.get("login")}


async def login(request: Request) -> RedirectResponse:
    if not init_oauth():
        raise HTTPException(503, "Admin auth not configured (missing OIDC_CLIENT_ID/SECRET)")
    redirect_uri = request.url_for("auth_callback")
    return await oauth.github.authorize_redirect(request, str(redirect_uri))


async def callback(request: Request) -> RedirectResponse:
    if not init_oauth():
        raise HTTPException(503, "Admin auth not configured")
    try:
        token = await oauth.github.authorize_access_token(request)
    except OAuthError as e:
        raise HTTPException(401, f"OAuth failed: {e.description}")

    access = token.get("access_token")
    if not access:
        raise HTTPException(401, "no access_token in OAuth response")

    # Verify org membership
    allowed = {o.lower() for o in allowed_github_orgs()}
    if not allowed:
        raise HTTPException(403, "no orgs configured in AUTH_ALLOWED_GITHUB_ORGS")
    user_orgs = _user_orgs(access)
    if not (user_orgs & allowed):
        raise HTTPException(
            403, f"user is not a member of any allowed org ({sorted(allowed)})"
        )

    # Stash minimal identity in the session
    headers = {"Authorization": f"token {access}", "Accept": "application/vnd.github+json"}
    with httpx.Client(timeout=10.0) as client:
        u = client.get("https://api.github.com/user", headers=headers).json()
    request.session["admin_user"] = {
        "login": u.get("login"),
        "id": u.get("id"),
        "orgs": sorted(user_orgs & allowed),
    }
    return RedirectResponse(url="/admin/cycles", status_code=302)


def require_admin(request: Request) -> dict[str, Any]:
    user = request.session.get("admin_user")
    if not user:
        raise HTTPException(401, "login required", headers={"Location": "/admin/login"})
    return user


def logout(request: Request) -> RedirectResponse:
    request.session.pop("admin_user", None)
    return RedirectResponse(url="/", status_code=302)
