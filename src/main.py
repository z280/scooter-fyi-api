"""FastAPI app entry — mounts routers, sets up CORS, sessions, and schedules jobs."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from datetime import datetime, timezone

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
    # Wall-clock-pinned cron schedule: fires at :00, :10, :20, :30, :40, :50
    # (assuming cycle_minutes=10). Using IntervalTrigger here would re-anchor
    # the schedule to boot-time on every restart, so a deploy at 14:23 would
    # cause cycles at 14:33, 14:43, 14:53… instead of the intuitive wall-clock
    # cadence. CronTrigger is stable across restarts: the next fire is always
    # the next aligned minute.
    _scheduler.add_job(
        run_once,
        trigger=CronTrigger(minute=f"*/{cfg.schedule.cycle_minutes}", timezone="UTC"),
        id="ingest_cycle",
        max_instances=1,
        coalesce=True,         # if multiple fires were missed, only catch up once
        misfire_grace_time=300,  # tolerate up to 5 min of worker downtime
    )
    # Archive cadence anchored to a fixed UTC point so restarts don't reset
    # the 48h timer. APScheduler computes next-fire as
    # ARCHIVE_ANCHOR + ceil((now - anchor) / interval) * interval, which is
    # stable across reboots. Without an anchor, every push to main would
    # reset the timer and the archive would never fire.
    _archive_anchor = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    _scheduler.add_job(
        run_archive,
        trigger=IntervalTrigger(
            hours=cfg.schedule.archive_hours,
            start_date=_archive_anchor,
        ),
        id="archive_job",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
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
        "Scheduler started: ingest at minute */%d UTC (wall-clock pinned), "
        "archive every %d hr, daily SLA at 09:00 America/Denver",
        cfg.schedule.cycle_minutes, cfg.schedule.archive_hours,
    )

    # Run one cycle immediately at startup so a fresh deploy doesn't wait up
    # to cycle_minutes for the first read. Offset 5s so app startup completes
    # first. `replace_existing=True` keeps repeated restarts from piling up
    # boot jobs.
    import asyncio
    asyncio.get_event_loop().call_later(
        5,
        lambda: _scheduler.add_job(run_once, id="boot_cycle", replace_existing=True),
    )

    yield

    log.info("Shutting down scheduler…")
    if _scheduler:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="veo-audit", version="3.2", lifespan=lifespan)

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
