"""FastAPI dependency that verifies a map-auth bearer token.

Usage:
    from .map_auth_dep import require_map_user

    @router.get("/api/v1/private/whatever")
    def handler(user = Depends(require_map_user)):
        ...

Returns a small dict {"login": str, "orgs": list[str]} describing the
authenticated user. Raises 401 on any failure (missing header, malformed,
unknown token, expired, revoked).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from fastapi import HTTPException, Request

from .pg import connection

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MapUser:
    login: str
    orgs: tuple[str, ...]


def require_map_user(request: Request) -> MapUser:
    authz = request.headers.get("Authorization") or ""
    if not authz.lower().startswith("bearer "):
        raise HTTPException(
            401,
            "missing or malformed Authorization header (expected: Bearer <token>)",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raw = authz.split(" ", 1)[1].strip()
    if not raw:
        raise HTTPException(401, "empty bearer token")
    digest = sha256(raw.encode("utf-8")).hexdigest()

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT github_login, github_orgs, expires_at, revoked_at
                FROM api_tokens
                WHERE token_sha256 = %s
                """,
                (digest,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(401, "invalid token")
            login, orgs, expires_at, revoked_at = row
            if revoked_at is not None:
                raise HTTPException(401, "token revoked")
            # The DB driver returns timezone-aware datetimes for TIMESTAMPTZ;
            # compare in UTC.
            import datetime as dt
            if expires_at < dt.datetime.now(dt.timezone.utc):
                raise HTTPException(401, "token expired")

            # Best-effort touch of last_used_at. A failure here MUST NOT
            # fail the request — it's instrumentation, not gating.
            try:
                cur.execute(
                    "UPDATE api_tokens SET last_used_at = NOW() WHERE token_sha256 = %s",
                    (digest,),
                )
                conn.commit()
            except Exception:  # noqa: BLE001
                log.exception("touching api_tokens.last_used_at failed")

    return MapUser(login=login, orgs=tuple(orgs or ()))
