"""Public legal pages — Terms of Service and Privacy Policy.

Served as plain HTML at clean, extensionless paths so links from the
frontend, Stripe checkout, and app stores don't expose that these are
template files. Content is static (no per-request data), so it's read
once at import time rather than re-read from disk on every request.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/legal")

_LEGAL_DIR = Path(__file__).parent / "templates" / "legal"
_TERMS_OF_SERVICE = (_LEGAL_DIR / "terms_of_service.html").read_text()
_PRIVACY_POLICY = (_LEGAL_DIR / "privacy_policy.html").read_text()


@router.get("/terms-of-service", response_class=HTMLResponse, include_in_schema=False)
def terms_of_service() -> HTMLResponse:
    return HTMLResponse(_TERMS_OF_SERVICE)


@router.get("/privacy-policy", response_class=HTMLResponse, include_in_schema=False)
def privacy_policy() -> HTMLResponse:
    return HTMLResponse(_PRIVACY_POLICY)
