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
    "updated": "2026-07-30",
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
                      "delete, cascading to its waypoints and watch record.",
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
