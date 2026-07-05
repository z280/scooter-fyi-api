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
    "updated": "2026-07-04",
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
            "detail": "An account stores your email, rate-plan choice, theme, "
                      "and favorites. Email zneill@gmail.com to delete an "
                      "account until self-serve deletion ships.",
        },
    ],
}


@router.get("/api/v1/meta/privacy")
def privacy(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "public, max-age=3600"
    return _PRIVACY
