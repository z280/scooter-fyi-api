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
from starlette.middleware.sessions import SessionMiddleware

from .api_admin import router as admin_router
from .api_private import router as private_router
from .api_public import router as public_router
from .api_reports import router as reports_router
from .config import load, session_https_only, session_secret
from .map_auth import router as map_auth_router
from .pg import run_migrations
from .sentry import init as sentry_init

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
    # GET for read-only data, POST for /map-auth/logout. Bearer tokens travel
    # in Authorization (covered by allow_headers="*"), not cookies — so
    # allow_credentials stays false.
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    allow_credentials=False,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret(),
    https_only=session_https_only(),
    same_site="lax",
)

app.include_router(public_router)
app.include_router(admin_router)
app.include_router(map_auth_router)
app.include_router(private_router)
app.include_router(reports_router)


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
            "/api/v1/boundaries",
            "/api/v1/compliance/daily/latest",
            "/admin",
        ],
    }
