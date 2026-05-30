"""FastAPI app entry — mounts routers, sets up CORS, sessions, and schedules jobs."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .archive import run_archive
from .api_admin import router as admin_router
from .api_public import router as public_router
from .config import load, session_https_only, session_secret
from .cycle import run_once
from .daily_sla import run_daily as run_daily_sla
from .pg import run_migrations
from .sentry import init as sentry_init

log = logging.getLogger("veo")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


_scheduler: AsyncIOScheduler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    sentry_init()
    log.info("Running migrations…")
    applied = run_migrations()
    log.info("Migrations applied this boot: %s", applied or "(none new)")

    cfg = load()
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        run_once,
        trigger=IntervalTrigger(minutes=cfg.schedule.cycle_minutes),
        id="ingest_cycle",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        next_run_time=None,  # first run after one interval; manual /admin trigger possible
    )
    _scheduler.add_job(
        run_archive,
        trigger=IntervalTrigger(hours=cfg.schedule.archive_hours),
        id="archive_job",
        max_instances=1,
        coalesce=True,
    )
    # Daily SLA window — runs at 9:00 AM Denver time (DST-aware via
    # zoneinfo). Computes the just-closed 6–9 AM compliance window.
    _scheduler.add_job(
        run_daily_sla,
        trigger=CronTrigger(hour=9, minute=0, timezone="America/Denver"),
        id="daily_sla_window",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,   # if worker was down, still run within an hour
    )
    _scheduler.start()
    log.info(
        "Scheduler started: ingest every %d min, archive every %d hr, "
        "daily SLA at 09:00 America/Denver",
        cfg.schedule.cycle_minutes, cfg.schedule.archive_hours,
    )

    # Trigger the first cycle immediately at startup (offset by 5s so the app
    # is fully ready). APScheduler's `next_run_time=datetime.now()` would work
    # but spinning it as a one-shot is cleaner.
    import asyncio
    asyncio.get_event_loop().call_later(5, lambda: _scheduler.add_job(run_once, id="boot_cycle", replace_existing=True))

    yield

    log.info("Shutting down scheduler…")
    if _scheduler:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="veo-audit", version="3.2", lifespan=lifespan)

_cfg = load()
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_cfg.cors_origins),
    allow_methods=["GET"],
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


@app.get("/", include_in_schema=False)
def root():
    return {
        "service": "veo-audit",
        "version": "3.2",
        "endpoints": [
            "/health",
            "/api/v1/snapshots/latest",
            "/api/v1/spatial-snapshot?layer=…",
            "/api/v1/analytics/trend?layer=…&name=…&range=7d",
            "/admin",
        ],
    }
