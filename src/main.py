"""FastAPI app entry — mounts routers, sets up CORS + sessions, runs migrations.

Scheduling has moved out of this process and into a dedicated `scheduler`
container (supercronic + the crontab at /app/crontab). This keeps the
schedule alive even when the API process crashes, and lets the worker
container restart freely without resetting cron timing.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .api_admin import router as admin_router
from .api_auth import router as auth_router
from .api_device_features import router as device_features_router
from .api_dibs import router as dibs_router
from .api_device_photos import router as device_photos_router
from .api_device_recommendations import router as device_recommendations_router
from .api_frontend_reports import router as frontend_reports_router
from .api_geocode import router as geocode_router
from .api_h3 import router as h3_router
from .api_leaderboard import router as leaderboard_router
from .api_legal import router as legal_router
from .api_lexicon import router as lexicon_router
from .api_meta import router as meta_router
from .api_points import router as points_router
from .api_favorites import router as favorites_router
from .api_preferences import router as preferences_router
from .api_qr import router as qr_router
from .api_rides import router as rides_router
from .api_ride_routes import router as ride_routes_router
from .api_ride_screenshots import router as ride_screenshots_router
from .api_ride_surveys import router as ride_surveys_router
from .api_route_feedback import router as route_feedback_router
from .api_device_history import router as device_history_router
from .api_private import router as private_router
from .api_profile import router as profile_router
from .api_public import router as public_router
from .api_reports import router as reports_router
from .api_telemetry import router as telemetry_router
from .api_tracked_rides import router as tracked_rides_router
from .api_route import router as route_router
from .api_user import router as user_router
from . import request_metrics
from .config import load, session_https_only, session_secret
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
    metrics_stop = asyncio.Event()
    metrics_task = asyncio.create_task(request_metrics.flush_loop(metrics_stop))
    yield
    metrics_stop.set()
    await metrics_task
    try:
        request_metrics.flush_pending()
    except Exception:  # noqa: BLE001 — shutdown must not hang on a dead DB
        log.exception("final request_metrics flush failed")


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
    # for /api/v1/rides, PATCH for /api/v1/tracked-rides/{id}/end. Bearer
    # tokens travel in Authorization (covered by allow_headers="*"), not
    # cookies — so allow_credentials stays false.
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
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
# Registered AFTER GZip: add_middleware wraps outermost-last, so this sits
# innermost — timing the app's own work, not compression or CORS handling.
app.middleware("http")(request_metrics.middleware)

app.include_router(public_router)
app.include_router(h3_router)
app.include_router(user_router)
app.include_router(admin_router)
app.include_router(private_router)
app.include_router(reports_router)
app.include_router(auth_router)
app.include_router(profile_router)
app.include_router(preferences_router)
app.include_router(favorites_router)
app.include_router(frontend_reports_router)
app.include_router(rides_router)
app.include_router(tracked_rides_router)
app.include_router(points_router)
app.include_router(device_recommendations_router)
app.include_router(device_photos_router)
app.include_router(device_features_router)
app.include_router(dibs_router)
app.include_router(qr_router)
app.include_router(ride_screenshots_router)
app.include_router(ride_surveys_router)
app.include_router(route_feedback_router)
app.include_router(device_history_router)
app.include_router(ride_routes_router)
app.include_router(meta_router)
app.include_router(legal_router)
app.include_router(lexicon_router)
app.include_router(route_router)
# Address search for Ride Mode's ride wizard. Sits beside the routing router
# because it feeds it: /geocode/search's in_coverage flag is membership in the
# same graph_bbox /route rejects on.
app.include_router(geocode_router)
app.include_router(leaderboard_router)
app.include_router(telemetry_router)


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
            "/api/v1/route?from=lat,lon&to=lat,lon&profile=safe",
            "/api/v1/route/profiles",
            "/api/v1/geocode/search?q=…",
            "/api/v1/user/devices/current",
            "/api/v1/equity-estimate?ranks=1,2",
            "/api/v1/h3/aggregates?res=9",
            "/api/v1/boundaries",
            "/api/v1/compliance/daily/latest",
            "/api/v1/auth/config",
            "/api/v1/auth/{google,magic-link,redeem,code,code/verify,refresh,session,signout}",
            "/api/v1/profile",
            "/api/v1/profile/username/regenerate",
            "/api/v1/profile/map-settings",
            "/api/v1/profile/map-settings/{name}",
            "/api/v1/profile/find-ride-pref",
            "/api/v1/profile/ride-usuals",
            "/api/v1/profile/ride-usuals/{name}",
            "/api/v1/emoji-nouns",
            "/api/v1/emoji-nouns/search?q=…",
            "/api/v1/adjectives",
            "/api/v1/adjectives/search?q=…",
            "/api/v1/royalty-titles",
            "/api/v1/royalty-titles/search?q=…",
            "/api/v1/ruling-colors",
            "/api/v1/reports/{device,discount,summary,export/monthly.csv}",
            "/api/v1/rides",
            "/api/v1/rides/start",
            "/api/v1/rides/active",
            "/api/v1/rides/export?format=geojson",
            "/api/v1/rides/{ride_id}/end",
            "/api/v1/rides/{ride_id}/waypoints",
            "/api/v1/tracked-rides",
            "/api/v1/tracked-rides/active",
            "/api/v1/tracked-rides/{ride_id}",
            "/api/v1/tracked-rides/{ride_id}/end",
            "/api/v1/tracked-rides/{ride_id}/track",
            "/api/v1/tracked-rides/{ride_id}/waypoints",
            "/api/v1/tracked-rides/{ride_id}/screenshots",
            "/api/v1/tracked-rides/{ride_id}/survey",
            "/api/v1/ride-routes",
            "/api/v1/points",
            "/api/v1/points/schedule",
            "/api/v1/devices/{vehicle_identifier}/recommend",
            "/api/v1/devices/{vehicle_identifier}/photos",
            "/api/v1/devices/qr-scan",
            "/api/v1/photos/{photo_id}/reports",
            "/api/v1/photos/mine",
            "/api/v1/meta/privacy",
            "/api/v1/meta/pricing",
            "/api/v1/leaderboard/map",
            "/api/v1/leaderboard/regional",
            "/api/v1/leaderboard/regional/live",
            "/legal/terms-of-service",
            "/legal/privacy-policy",
            "/admin",
        ],
    }
