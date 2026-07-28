"""Meta endpoints (API_REQUIREMENTS.md §5).

GET /api/v1/meta/privacy — machine-readable retention policy. The frontend
privacy page renders this, so the published policy and the enforced policy
share one source of truth. When a retention rule changes in code (e.g.
cleanup_receipts), CHANGE THIS PAYLOAD IN THE SAME COMMIT.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

router = APIRouter()

_PRIVACY = {
    "updated": "2026-07-27",
    "contact": "zneill@gmail.com",
    "retention": [
        {
            "data": "sessions",
            "retention": "30 days idle",
            "detail": "Bearer tokens are stored hashed (sha256). Rider sessions "
                      "expire 30 days after last refresh; admin sessions after 24 "
                      "hours. Revoked/expired rows are pruned after 30 days.",
        },
        {
            "data": "magic_link_tokens",
            "retention": "15 minutes",
            "detail": "Single-use, stored hashed, burned on redemption; dead "
                      "tokens are pruned within a day.",
        },
        {
            "data": "receipts",
            "retention": "18 months",
            "detail": "Discount-report receipt images live in a private bucket, "
                      "EXIF-stripped on upload (full re-encode — GPS and camera "
                      "metadata cannot survive). Deleted by a daily job after 18 "
                      "months; the report row outlives the image.",
        },
        {
            "data": "rides",
            "retention": "until you delete them",
            "detail": "Ride history (including route polylines) exists only for "
                      "your own account. DELETE /api/v1/rides[/:id] is an "
                      "immediate hard delete. Ride routes are never used for "
                      "analytics or shared in any aggregate.",
        },
        {
            "data": "reports",
            "retention": "indefinite, aggregated",
            "detail": "Device and discount reports are the audit evidence base "
                      "and are kept indefinitely. Public aggregates and the CSV "
                      "export never include reporter identity (no IP, no email "
                      "— only an authenticated yes/no flag).",
        },
        {
            "data": "accounts",
            "retention": "until deletion is requested",
            "detail": "An account stores your email and/or phone number (at "
                      "least one is required), rate-plan choice, theme, "
                      "favorites, a public username (an adjective + emoji you "
                      "can choose or re-roll), optional home/work coordinates, "
                      "and two visibility toggles (public username, "
                      "leaderboards). Email zneill@gmail.com to delete an "
                      "account until self-serve deletion ships.",
        },
        {
            "data": "tracked_rides",
            "retention": "until you delete them",
            "detail": "Server-detected ride tracking (start location, GBFS "
                      "watch results, waypoints, your reported end location/"
                      "cost/battery). A separate mechanism from the `rides` "
                      "entry above — but the same commitment applies: DELETE "
                      "/api/v1/tracked-rides[/:id] is an immediate hard delete, "
                      "cascading to its waypoints and watch record.",
        },
        {
            "data": "device_photos",
            "retention": "indefinite (public content)",
            "detail": "Rider-uploaded photos of physical devices are public, "
                      "capped at 3 per device, and attributed to the "
                      "uploader's public username if they've enabled it. "
                      "EXIF/GPS is stripped on upload. Kept indefinitely as "
                      "community reference material, same as device and "
                      "discount reports.",
        },
        {
            "data": "ride_transaction_screenshots",
            "retention": "18 months",
            "detail": "Two screenshots per ride (overview, receipt) in a "
                      "private bucket, EXIF-stripped on upload, visible only "
                      "to the uploader. Mirrors the receipts retention window "
                      "above; a matching cleanup job removes the image after "
                      "18 months.",
        },
    ],
}


@router.get("/api/v1/meta/privacy")
def privacy(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "public, max-age=3600"
    return _PRIVACY
