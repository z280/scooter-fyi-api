"""FastAPI app entry — mounts routers, sets up CORS + sessions, runs migrations.

Scheduling has moved out of this process and into a dedicated `scheduler`
container (supercronic + the crontab at /app/crontab). This keeps the
schedule alive even when the API process crashes, and lets the worker
container restart freely without resetting cron timing.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .api_admin import router as admin_router
from .api_auth import router as auth_router
from .api_frontend_reports import router as frontend_reports_router
from .api_h3 import router as h3_router
from .api_legal import router as legal_router
from .api_meta import router as meta_router
from .api_rides import router as rides_router
from .api_private import router as private_router
from .api_profile import router as profile_router
from .api_public import router as public_router
from .api_reports import router as reports_router
from .api_user import router as user_router
from .config import load, session_https_only, session_secret
from .pg import run_migrations
from .sentry import init as sentry_init
from .stripe_webhook import router as stripe_router

log = logging.getLogger("veo")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    sentry_init()
    # The vehicle_identifier salt is checked lazily by src/identity.hash_plate
    # the first time it's called. Fail-fast on missing env var happens there.
    log.info("Running migrations…")
    applied = run_migrations()
    log.info("Migrations applied this boot: %s", applied or "(none new)")
    log.info(
        "Worker started. Scheduling lives in the `scheduler` container "
        "(supercronic + /app/crontab) — this process serves HTTP only."
    )
    yield


app = FastAPI(title="veo-audit", version="3.3", lifespan=lifespan)

_cfg = load()
# Combine pattern entries into a single alternation regex if any exist —
# FastAPI's CORSMiddleware takes one regex via allow_origin_regex.
# Exact-match origins continue to work via allow_origins (cheaper than regex).
_cors_regex: str | None = None
if _cfg.cors_origin_patterns:
    _cors_regex = "|".join(f"(?:{p})" for p in _cfg.cors_origin_patterns)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_cfg.cors_origins),
    allow_origin_regex=_cors_regex,
    # GET for reads, POST for auth/reports, PUT for /api/v1/profile, DELETE
    # for /api/v1/rides. Bearer tokens travel in Authorization (covered by
    # allow_headers="*"), not cookies — so allow_credentials stays false.
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
    allow_credentials=False,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret(),
    https_only=session_https_only(),
    same_site="lax",
)
# Origin-side gzip so big JSON payloads (devices/current is the heavy one)
# are compressed even for clients that bypass the CDN; behind Cloudflare
# the edge re-encodes to brotli for browsers that prefer it.
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.include_router(public_router)
app.include_router(h3_router)
app.include_router(user_router)
app.include_router(admin_router)
app.include_router(private_router)
app.include_router(reports_router)
app.include_router(auth_router)
app.include_router(profile_router)
app.include_router(frontend_reports_router)
app.include_router(rides_router)
app.include_router(stripe_router)
app.include_router(meta_router)
app.include_router(legal_router)


@app.get("/", include_in_schema=False)
def root():
    return {
        "service": "veo-audit",
        "version": "3.3",
        "endpoints": [
            "/health",
            "/api/v1/snapshots/latest",
            "/api/v1/spatial-snapshot?layer=…",
            "/api/v1/analytics/trend?layer=…&name=…&range=7d",
            "/api/v1/devices/current",
            "/api/v1/user/devices/current",
            "/api/v1/equity-estimate?ranks=1,2",
            "/api/v1/h3/aggregates?res=9",
            "/api/v1/boundaries",
            "/api/v1/compliance/daily/latest",
            "/api/v1/auth/config",
            "/api/v1/auth/{google,magic-link,redeem,code,code/verify,refresh,session,signout}",
            "/api/v1/profile",
            "/api/v1/reports/{device,discount,summary,export/monthly.csv}",
            "/api/v1/rides",
            "/api/v1/meta/privacy",
            "/legal/terms-of-service",
            "/legal/privacy-policy",
            "/admin",
        ],
    }
