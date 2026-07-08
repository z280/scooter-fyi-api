"""Authenticated rider endpoints — gated by account sessions (magic-link /
Google) via require_session.

`GET /api/v1/user/devices/current` is the signed-in map feed. It returns
the same GeoJSON as the public `/api/v1/devices/current` for any rider,
and — for an `admin`-scope session — the extra private fields that used to
live behind the (now retired) `/api/v1/private/devices/current`: raw
`vehicle_plate`, `first_ever_observed_at`, and the observed max range.

Admin gate: raw membership of the session email in `ADMIN_EMAILS`, so the
private fields unlock for an allowlisted email signed in via EITHER door —
magic-link or Google. This is intentionally broader than the `admin`
*scope* (which src/accounts.py grants only via Google): the operator wants
both doors viable (and may drop the Google door entirely). Both doors
prove ownership of the email, and this is a read-only plate view, not an
admin write action. (The `/api/v1/private/*` endpoints still gate on the
`admin` scope — those are operator tooling, not the rider map.)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from .accounts import SessionUser, admin_emails, normalize_email, require_session
from .api_public import _devices_current_impl

router = APIRouter()

# Per-user, plate-bearing responses must not be shared-cached by a CDN.
_USER_DEVICES_CACHE_HEADER = "private, max-age=30"


def _wants_plate(user: SessionUser) -> bool:
    # Either door: any session (magic-link OR Google) whose email is on the
    # ADMIN_EMAILS allowlist. Broader than the Google-only `admin` scope.
    return normalize_email(user.email) in admin_emails()


@router.get("/api/v1/user/devices/current")
def user_devices_current(
    request: Request,
    response: Response,
    user: SessionUser = Depends(require_session),
    form_factor: str | None = Query(None),
    spatial_status: str | None = Query(None),
    include_outliers: bool = Query(False),
    bbox: str | None = Query(None),
    include: str | None = Query(None),
) -> Any:
    """Signed-in map feed. Same shape as `/api/v1/devices/current`; adds
    `vehicle_plate`, `first_ever_observed_at`, `max_observed_range_meters`,
    and `max_observed_range_at` when the session email is in `ADMIN_EMAILS`
    (either sign-in door). `metadata.admin` reports whether those were
    included.

    Requires a rider session (`Authorization: Bearer <token>`); `401` when
    missing/invalid/expired.
    """
    return _devices_current_impl(
        request,
        response,
        form_factor=form_factor,
        spatial_status=spatial_status,
        include_outliers=include_outliers,
        bbox=bbox,
        include=include,
        include_plate=_wants_plate(user),
        resource="user-devices",
        cache_header=_USER_DEVICES_CACHE_HEADER,
        viewed_by=user.email,
    )
