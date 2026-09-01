"""Meta endpoints (API_REQUIREMENTS.md §5).

GET /api/v1/meta/pricing — the sales-tax rate Ride Mode's cost breakdown
applies, config-driven (see the "Pricing" section below). Rate PLANS stay
client-side; only the tax does not, because it is a legal rate that changes
on a city council's schedule rather than a deploy's.

GET /api/v1/meta/privacy — machine-readable retention policy. The frontend
privacy page renders this, so the published policy and the enforced policy
share one source of truth. When a retention rule changes in code (e.g.
cleanup_receipts), CHANGE THIS PAYLOAD IN THE SAME COMMIT.

That instruction has one more address than it used to admit. There are
THREE places a retention rule is written down and they must move together:

  1. the cleanup job in src/cli.py, which is what actually happens;
  2. this payload, which is what the API says happens;
  3. src/templates/legal/privacy_policy.html, the human-readable policy
     served at /legal/privacy — the version a rider or a regulator reads.

sql/038 stored model-report photos and touched none of the three, so the
photos were retained forever while all three documents were silent. A new
STORED FIELD counts as a retention rule, not just a new deletion schedule:
if the system starts keeping something, it belongs here.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Response

from . import config as config_module
from .config import load

log = logging.getLogger(__name__)

router = APIRouter()

_PRIVACY = {
    "updated": "2026-08-05",
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
            "data": "favorite_devices",
            "retention": "until you delete them",
            "detail": "Vehicles you kept in My Scooters: the vehicle "
                      "identifier, your nickname for it, when you last "
                      "proved at the kerb that you were standing at it, and "
                      "whether you want telling when it comes free. WHERE "
                      "you were standing is NOT stored — the 75 m check runs "
                      "when you keep the scooter and the position is then "
                      "discarded. A kept vehicle's position is withheld "
                      "while somebody is riding it: you can see where yours "
                      "is parked, never where it is going. DELETE "
                      "/api/v1/profile/favorite-devices/:vehicle_identifier "
                      "is an immediate hard delete, and every row cascades "
                      "when the account is deleted.",
        },
        {
            "data": "user_preferences",
            "retention": "until you delete them",
            "detail": "Rider-owned preference blobs: named map settings, the "
                      "find-ride preference, and ride-mode 'Usuals' (saved "
                      "ride-option presets). Opaque client-owned JSON, stored "
                      "verbatim, never read into analytics or any aggregate, "
                      "and never visible to another account. DELETE "
                      "/api/v1/profile/map-settings/:name, /find-ride-pref and "
                      "/ride-usuals/:name are immediate hard deletes, and every "
                      "row cascades when the account is deleted.",
        },
        {
            "data": "tracked_rides",
            "retention": "until you delete them",
            "detail": "Server-detected ride tracking (start location, GBFS "
                      "watch results, waypoints, your reported end location/"
                      "cost/battery, the ride-mode options you chose, your "
                      "reported ride minutes and rate-plan tier, and a "
                      "per-ride signing key issued to your device). A "
                      "separate mechanism from the `rides` entry above — but "
                      "the same commitment applies: DELETE "
                      "/api/v1/tracked-rides[/:id] is an immediate hard "
                      "delete, cascading to its waypoints and watch record, "
                      "and — if you donated this ride's track — to that "
                      "donation record too, as long as you delete before "
                      "the donation's own de-identification sweep runs "
                      "(see 'donated_tracks' below; within 28 hours of "
                      "donation). Once a donated track has been "
                      "de-identified it is no longer linked to your "
                      "account at all, so deleting the ride after that "
                      "point can no longer reach it — there is no owner "
                      "left for the delete to cascade from.",
        },
        {
            "data": "donated_tracks",
            "retention": "account link removed within 28 hours of donation",
            "detail": "When you opt in to 'Improve battery modeling' or "
                      "'Navigation Improvement' and donate your saved ride "
                      "track at the end of a ride, the signed waypoint chain "
                      "is verified once, then stored as a trip record "
                      "(start/end points, distance, timing) linked to your "
                      "account. An hourly sweep removes that account link 4 "
                      "hours after your points for the trip settle, with a "
                      "hard floor of 28 hours after donation even if points "
                      "never settle — so the account link never survives "
                      "past 28 hours. Recorded waypoint timestamps are "
                      "coarsened to the minute in the same sweep. What "
                      "remains afterward, with no account or ride linkage: "
                      "the trip's derived battery observation (vehicle "
                      "model, start/end battery percentage, distance, and "
                      "duration), kept indefinitely to improve our range "
                      "predictions for that vehicle model — matching the "
                      "in-app 'Our Usage' explanation for that feature.",
        },
        {
            "data": "ride_routes",
            "retention": "account link removed within 28 hours",
            "detail": "When you turn on 'Navigation Improvement' before a ride, each "
                      "route you pick on Screen 4 (including a mid-ride reselect) is "
                      "stored -- profile, origin/destination, the route geometry, and "
                      "your distance/duration/battery estimates -- linked to your "
                      "account and, once known, the ride. The same hourly sweep that "
                      "de-identifies donated tracks removes this link 28 hours after "
                      "the route was stored, whether or not you ever donated or "
                      "surveyed that ride; the route geometry itself is kept "
                      "afterward with no link back to you.",
        },
        {
            "data": "ride_surveys",
            "retention": "until you delete the ride, or your account",
            "detail": "Your end-of-ride feedback (scooter-condition answers, "
                      "free-text navigation comments, route ratings). It carries no "
                      "geometry of its own, so unlike ride routes above it is never "
                      "de-identified -- it stays linked to your account and ride "
                      "under the same rule as your ride history: deleting the ride "
                      "cascades to its survey, and deleting your account removes "
                      "every survey you wrote.",
        },
        {
            "data": "user_points",
            "retention": "indefinite; deleted only with the account",
            "detail": "The points ledger keeps every earned-points row — "
                      "account id, the coarse H3 resolution-8 area cell "
                      "your location falls in, your ride's start "
                      "coordinates, the action and point value, and (for a "
                      "device-tied award) the vehicle id — indefinitely. "
                      "These rows are the leaderboard record: the H3 area "
                      "leaderboard is computed directly from them, so "
                      "unlike donated tracks above they are never "
                      "de-identified. The only way to remove your own "
                      "ledger rows is to delete your account, which "
                      "cascades to them. Public exposure through the "
                      "leaderboard is subject to your account's visibility "
                      "toggles (public username, leaderboard "
                      "participation) — turning those off removes you "
                      "from the public view without deleting the "
                      "underlying rows.",
        },
        {
            "data": "h3_r8_area_report",
            "retention": "no personal data; refreshed weekly",
            "detail": "The list of map hexagons -- every H3 "
                      "resolution-8 area that has ever had a scooter "
                      "observed in it or a point earned in it. It holds "
                      "cell identifiers and two yes/no flags, and no "
                      "account ids, names or points: nothing in it is "
                      "about you. The leaderboard standings themselves are "
                      "no longer stored anywhere. They are computed from "
                      "the points ledger above at the moment someone loads "
                      "the map, so there is no second copy of your ranking "
                      "to retain, and deleting your account removes you "
                      "from every board on the very next request. Whether "
                      "a rank you hold is shown publicly is likewise "
                      "decided fresh on every request from your account's "
                      "current visibility toggles (public username, "
                      "leaderboard participation) -- turning either off "
                      "removes you from the public view immediately.",
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
            "data": "model_reports",
            "retention": "report indefinite; photo 18 months",
            "detail": "A model report is a catalog correction — 'you're "
                      "showing this scooter as the wrong model'. The "
                      "correction itself (your description, the device id, "
                      "coordinates if you sent them, and the IP and user "
                      "agent the report arrived with) is kept indefinitely "
                      "as part of the catalog's history; anonymous reports "
                      "are accepted and carry no account. An attached photo "
                      "lives in the same private bucket as receipts, is "
                      "EXIF-stripped on upload (full re-encode — GPS and "
                      "camera metadata cannot survive), and is deleted by a "
                      "daily job after 18 months, matching the receipts "
                      "window; the report row outlives the image.",
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
        {
            "data": "telemetry_events",
            "retention": "90 days raw",
            "detail": "First-party, cookieless usage events from the web app "
                      "(which drawer opened, which mode, which wizard screen "
                      "— allowlisted names, no free text, no coordinates, no "
                      "ride content, no preference blobs). No account id is "
                      "ever stored — only a signed-in yes/no flag. Visitor "
                      "counting uses sha256(daily salt + IP + user-agent); "
                      "the salt is destroyed after 2 days, after which the "
                      "hash cannot be recomputed by anyone. Neither the IP "
                      "nor the user-agent is stored. If you arrive via a "
                      "link we published tagged with a campaign code "
                      "(utm_campaign), that code is stored with events — "
                      "matched against our own fixed list of campaigns, "
                      "never free text, and identifying only the link, not "
                      "you. Opt out any time via "
                      "the About panel toggle (stored on your device).",
        },
        {
            "data": "request_metrics",
            "retention": "30 days raw",
            "detail": "Per-request API metrics: route template (never the "
                      "raw path), method, status, duration, and a coarse "
                      "device class/OS bucket derived from the user-agent. "
                      "No IP, no raw user-agent, no account id — only "
                      "whether a bearer token was presented.",
        },
        {
            "data": "analytics_rollups",
            "retention": "indefinite, aggregated",
            "detail": "Daily aggregate tables (event counts, distinct-"
                      "visitor counts, latency percentiles) computed from "
                      "the two raw tables above before they are pruned. "
                      "Contain no identifiers of any kind.",
        },
    ],
}


@router.get("/api/v1/meta/privacy")
def privacy(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "public, max-age=3600"
    return _PRIVACY


# --- Pricing ----------------------------------------------------------------
# Ride Mode's Screen 8 cost breakdown needs one number the client cannot
# derive: the sales-tax rate applied to a Veo ride. Veo's own rate plans stay
# client-side (they are marketing terms, and the client already has them);
# the tax rate does not, because it changes when a ballot measure passes and
# every installed client would otherwise be wrong until it updated.
#
# DEFAULT = 0.0915 — Denver's combined sales-tax rate, itemized:
#     2.90 %  Colorado state
#     1.00 %  RTD (Regional Transportation District)
#     0.10 %  SCFD / cultural facilities district
#     5.15 %  City & County of Denver
#     ------
#     9.15 %  effective 2025-01-01, when Denver's own rate rose from 4.81 %
#             (ballot measure 2Q, Denver Health, +0.34 %).
# The pre-2025 combined rate was 8.81 %, which is the figure most third-party
# tables and the frontend's own api.ts doc-comment example still quote — if
# you are reconciling the two, that is why they differ.
#
# The rate is FRACTIONAL, not a percentage: 0.0915, never 9.15. A config
# carrying 9.15 would multiply a rider's tax by 100, so the loader below
# rejects anything outside [0, 1) and serves the default instead of a bill
# nobody owes.
#
# Operator-tunable in config.json; nothing here needs a code change when the
# rate moves, only `as_of` and the number.
_DEFAULT_TAX_RATE = 0.0915
_DEFAULT_CURRENCY = "USD"
# Effective date of the rate above — NOT "when this payload was generated".
# The client shows it so a rider comparing a stale offline default against a
# refreshed one can tell which is which.
_DEFAULT_AS_OF = "2025-01-01"


@lru_cache(maxsize=1)
def _raw_pricing_block() -> dict[str, Any]:
    """The `"pricing"` block straight out of config.json.

    `src/config.py` is expected to grow a typed `pricing` block; until then
    (and if a deployment's config.py ever lags its config.json) this reads the
    raw JSON, so an operator who edits config.json gets the rate they typed
    either way rather than a silently ignored edit. Cached like
    `config.load()` — config.json is read at boot in this codebase and is
    mounted read-only.
    """
    try:
        with open(config_module.CONFIG_PATH) as fh:
            block = json.load(fh).get("pricing")
    except (OSError, ValueError) as exc:
        # ValueError covers json.JSONDecodeError; a malformed or unreadable
        # config.json degrades to the baked defaults rather than 500ing an
        # endpoint whose whole job is publishing one number.
        log.warning("could not read the pricing config block from %s: %s",
                    config_module.CONFIG_PATH, exc)
        return {}
    return dict(block) if isinstance(block, dict) else {}


def _configured_pricing() -> dict[str, Any]:
    """Configured pricing values, typed config first, raw JSON second."""
    block = getattr(load(), "pricing", None)
    if block is not None:
        return {
            "tax_rate": getattr(block, "tax_rate", None),
            "currency": getattr(block, "currency", None),
            "as_of": getattr(block, "as_of", None),
        }
    return _raw_pricing_block()


def _tax_rate(raw: Any) -> float:
    """A fractional rate in [0, 1), or the default with a loud log.

    The failure this guards is a config carrying `9.15` (a percentage) where
    a fraction belongs — which would not error anywhere, it would just charge
    every rider a hundredfold tax in the breakdown.
    """
    if raw is None:
        return _DEFAULT_TAX_RATE
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        log.warning("pricing.tax_rate %r is not a number — serving %s",
                    raw, _DEFAULT_TAX_RATE)
        return _DEFAULT_TAX_RATE
    if not (0.0 <= rate < 1.0):
        log.warning(
            "pricing.tax_rate %r is not a fraction in [0, 1) (a percentage "
            "like 9.15 belongs in config as 0.0915) — serving %s",
            raw, _DEFAULT_TAX_RATE,
        )
        return _DEFAULT_TAX_RATE
    return rate


def pricing_payload() -> dict[str, Any]:
    """`GET /api/v1/meta/pricing`'s body. Split out so it is testable and so
    anything else that needs the rate reads it from one place."""
    configured = _configured_pricing()
    return {
        "tax_rate": _tax_rate(configured.get("tax_rate")),
        "currency": str(configured.get("currency") or _DEFAULT_CURRENCY),
        "as_of": str(configured.get("as_of") or _DEFAULT_AS_OF),
    }


@router.get("/api/v1/meta/pricing")
def pricing(response: Response) -> dict[str, Any]:
    """Public — no bearer. Cached for an hour like `/meta/privacy`: a tax
    rate changes on a ballot measure's schedule, and the client bakes its own
    offline default anyway, so this is a refresh, never a dependency."""
    response.headers["Cache-Control"] = "public, max-age=3600"
    return pricing_payload()
