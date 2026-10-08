"""Meta endpoints (docs/API_REQUIREMENTS.md §5).

GET /api/v1/meta/pricing — the sales-tax rate Ride Mode's cost breakdown
applies, config-driven (see the "Pricing" section below). Rate PLANS stay
client-side; only the tax does not, because it is a legal rate that changes
on a city council's schedule rather than a deploy's.

GET /api/v1/meta/privacy — machine-readable retention policy. It is a
hand-maintained copy of what the HTML policy says (nothing generates one
from the other), so the two must agree on every category, retention rule
and processor. When a retention rule changes in code (e.g.
cleanup_receipts), CHANGE THIS PAYLOAD IN THE SAME COMMIT.

That instruction has one more address than it used to admit. There are
THREE places a retention rule is written down and they must move together:

  1. the cleanup job in src/cli.py, which is what actually happens;
  2. this payload, which is what the API says happens;
  3. src/templates/legal/privacy_policy.html, the human-readable policy
     served at /legal/privacy-policy — the version a rider or a regulator reads.

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
    "updated": "2026-10-08",
    "contact": "mhtc@z280.com",
    "retention": [
        {
            "data": "sessions",
            "retention": "30 days idle",
            "detail": "Bearer tokens are stored hashed (sha256). Rider sessions "
                      "expire 30 days after last refresh; admin sessions after 24 "
                      "hours. Revoked and expired rows older than 30 days "
                      "are deleted when someone next refreshes a session. "
                      "Rider tokens live in browser local storage; admin "
                      "sign-in uses GitHub OAuth and a first-party session "
                      "cookie.",
        },
        {
            "data": "magic_link_tokens",
            "retention": "links 15 minutes; emailed and texted codes 10 minutes",
            "detail": "Sign-in links (emailed) and sign-in codes (emailed or "
                      "texted). Single-use, stored hashed, burned on "
                      "redemption; each row also holds the email or phone "
                      "number and the requesting IP. Old rows are deleted no "
                      "sooner than a day after they expire, when the next "
                      "sign-in request is made.",
        },
        {
            "data": "rate_limit_events",
            "retention": "until the same key is checked again; may be indefinite",
            "detail": "Rate-limit entries are keyed by IP address, email "
                      "address or phone number. An entry over 2 days old is "
                      "deleted the next time the same key is rate-checked "
                      "for the same action; if that never happens, it is "
                      "never deleted.",
        },
        {
            "data": "server_access_logs",
            "retention": "no fixed limit",
            "detail": "Our web server's access log records each request URL "
                      "with your IP address. Route, walking-route and "
                      "place-search requests put your current location (and "
                      "what you typed) in the URL, so the log holds location "
                      "next to IP. Kept until the server container is "
                      "replaced.",
        },
        {
            "data": "sms_messages",
            "retention": "indefinite",
            "detail": "Texts go from our own messaging service (z280 comms, "
                      "shared with our other sites) to a self-hosted SMS "
                      "gateway on an Android phone, then through your mobile "
                      "carrier; each sees your phone number and the message "
                      "text. The messaging service keeps a log of every "
                      "message sent (recipient and text) indefinitely. "
                      "Replies (e.g. STOP) are stored with the sender number, "
                      "the text and how we classified it, with no expiry, so "
                      "opt-outs keep being honoured. STOP applies to every "
                      "site sharing that sender number.",
        },
        {
            "data": "receipts",
            "retention": "18 months",
            "detail": "The receipt screenshot you upload lives in a private "
                      "bucket, EXIF-stripped on upload (full re-encode — GPS and "
                      "camera metadata cannot survive). Deleted by a daily job "
                      "after 18 months; the report row outlives the image. We no "
                      "longer ask for a screenshot of your Veo plan: you tell us "
                      "the plan and we take your word for it. We do not "
                      "currently read receipt images automatically. If we "
                      "start, we will update this policy first; it may happen "
                      "on your device or on our servers via OpenRouter, and "
                      "only for an image you uploaded.",
        },
        {
            "data": "rides",
            "retention": "until you delete them",
            "detail": "Rides you log on vehicles that are not in the "
                      "operator's feed (including route polylines) exist only "
                      "for your own account. GET /api/v1/rides/export exports "
                      "them; DELETE /api/v1/rides[/:id] is an "
                      "immediate hard delete. Ride routes are never used for "
                      "analytics or shared in any aggregate.",
        },
        {
            "data": "reports",
            "retention": "indefinite, aggregated; discount reports and receipt claims until account deletion",
            "detail": "Device failure reports, device feature and condition "
                      "reports (plate, QR value), route feedback (free text up "
                      "to 2,000 characters), negative reports and quality "
                      "feedback, recommendations, photo flags with any comment, "
                      "and QR-scan first/last scanner are the audit evidence "
                      "base and are kept indefinitely, with the description, "
                      "any coordinates, and the IP and user agent they arrived "
                      "with (several forms accept anonymous submissions). On "
                      "account deletion they stay, with the account link "
                      "removed. Missed-discount reports and receipt claims "
                      "require an account and are deleted with it. A discount "
                      "report records which "
                      "Equity Area the ride ended in, and a receipt claim what the "
                      "receipt says (scooter code, minutes, costs, charge date), "
                      "the plan you say you were on — on trust, with no screenshot "
                      "asked for — and optionally an approximate start time and "
                      "where you think the ride started and ended. "
                      "Public aggregates and the CSV export never include reporter "
                      "identity (no IP, no email — only an authenticated yes/no "
                      "flag), never the scooter code itself (only its one-way "
                      "identifier), and round any point to about 100 m.",
        },
        {
            "data": "accounts",
            "retention": "until deletion is requested",
            "detail": "An account stores your email and/or phone number (at "
                      "least one is required), how you signed in (Google, "
                      "emailed link, emailed code, texted code), a phone-"
                      "verified flag and an SMS opted-out flag, rate-plan "
                      "choice, theme, a public username (an "
                      "adjective + emoji you can choose or re-roll), display "
                      "name, royalty title and colour choices, last login, "
                      "and two visibility toggles (public username, "
                      "leaderboards) which are ON by default. Saved places "
                      "(Home, Work and any others you name; synced from your "
                      "device while signed in) are ENCRYPTED before storage "
                      "with a key the database does not hold, so they are "
                      "unreadable in the database and in backups of it. We "
                      "hold the key and can decrypt them to serve them back "
                      "to your devices: encryption at rest, not end-to-end. "
                      "Older plaintext copies (accounts.favorites and the "
                      "home/work coordinate columns, written before "
                      "2026-10-08) are still stored for accounts that had "
                      "them: a profile read copies them into the encrypted "
                      "list but does not clear them, and the home/work "
                      "columns are cleared only when you remove Home or "
                      "Work. A later migration drops them. Email "
                      "mhtc@z280.com to delete an account until self-serve "
                      "deletion ships.",
        },
        {
            "data": "google_user_data",
            "retention": "not stored beyond your email address",
            "detail": "When you sign in with Google, Google sends us an ID "
                      "token containing your email address, whether it is "
                      "verified, your name, profile picture and a Google "
                      "account ID. We use only the email address and the "
                      "verified flag, to create or find your account; name, "
                      "picture and account ID are not stored. We request no "
                      "other Google data, do not use Google user data for "
                      "advertising, and do not sell or transfer it; it is "
                      "used only to sign you in. Our use complies with the "
                      "Google API Services User Data Policy, including the "
                      "Limited Use requirements. Disconnect at "
                      "myaccount.google.com/connections.",
        },
        {
            "data": "favorite_devices",
            "retention": "until you delete them",
            "detail": "My Scooters was retired on 2026-10-06. Nothing new "
                      "can be kept, and kept vehicles are no longer shown "
                      "or tracked. Rows kept before then held the vehicle "
                      "identifier, your nickname for it, when you last "
                      "proved at the kerb that you were standing at it, and "
                      "whether you wanted telling when it came free. Where "
                      "you were standing was never stored. To delete them: "
                      "GET /api/v1/profile/favorite-devices/retired lists "
                      "what you kept (identifiers, nicknames and dates "
                      "only), and DELETE "
                      "/api/v1/profile/favorite-devices/:vehicle_identifier "
                      "removes one immediately. Every row is also deleted "
                      "with your account, or on request by email.",
        },
        {
            "data": "user_preferences",
            "retention": "until you delete them",
            "detail": "Rider-owned preference blobs: named map settings, "
                      "ride-mode 'Usuals' (saved "
                      "ride-option presets), and ride specs (your saved "
                      "'ideal scooter' — which models, features, battery and "
                      "quality you will ride). Opaque client-owned JSON, "
                      "stored verbatim, never read into analytics or any "
                      "aggregate, and never visible to another account. "
                      "DELETE /api/v1/profile/map-settings/:name, "
                      "/ride-usuals/:name and "
                      "/ride-specs/:name are immediate hard deletes, and "
                      "every row cascades when the account is deleted.",
        },
        {
            "data": "tracked_rides",
            "retention": "until you delete them",
            "detail": "Created automatically for any ride started in Ride "
                      "Mode while signed in (none is created for a ride "
                      "started signed out). Server-detected ride tracking "
                      "(start location, GBFS "
                      "watch results, waypoints, your reported end location/"
                      "cost/battery, the ride-mode options you chose, your "
                      "reported ride minutes and rate-plan tier, and a "
                      "per-ride signing key issued to your device). List "
                      "them with GET /api/v1/tracked-rides (there is no "
                      "export yet). A "
                      "separate mechanism from the `rides` entry above — but "
                      "the same commitment applies: DELETE "
                      "/api/v1/tracked-rides[/:id] is an immediate hard "
                      "delete, cascading to its waypoints and watch record, "
                      "and — if you donated this ride's track — to that "
                      "donation record too, as long as you delete before "
                      "the donation's own de-identification sweep runs "
                      "(see 'donated_tracks' below; within about 29 hours "
                      "of donation). Once a donated track has been "
                      "de-identified it is no longer linked to your "
                      "account at all, so deleting the ride after that "
                      "point can no longer reach it — there is no owner "
                      "left for the delete to cascade from.",
        },
        {
            "data": "donated_tracks",
            "retention": "account link removed within about 29 hours of donation; track kept indefinitely",
            "detail": "When you opt in to 'Improve battery modeling' or "
                      "'Navigation Improvement' and donate your saved ride "
                      "track at the end of a ride, the signed waypoint chain "
                      "is verified once, then stored as a trip record "
                      "(start/end points, distance, timing) linked to your "
                      "account. An hourly sweep removes that account link 4 "
                      "hours after your points for the trip settle, with a "
                      "hard floor of 28 hours after donation even if points "
                      "never settle; the sweep runs hourly, so the link is "
                      "gone within about 29 hours. Recorded waypoint "
                      "timestamps are coarsened to the minute in the same "
                      "sweep. What remains afterward, with no account or "
                      "ride linkage, kept indefinitely: the GPS track itself "
                      "(each point's latitude, longitude and accuracy, timed "
                      "to the minute) and the trip's derived battery "
                      "observation (vehicle model, start/end battery "
                      "percentage, distance, and duration), used to improve "
                      "range predictions and navigation.",
        },
        {
            "data": "ride_routes",
            "retention": "account link removed within about 29 hours",
            "detail": "When you turn on 'Navigation Improvement' before a ride, each "
                      "route you pick on Screen 4 (including a mid-ride reselect) is "
                      "stored -- profile, origin/destination, the route geometry, and "
                      "your distance/duration/battery estimates -- linked to your "
                      "account and, once known, the ride. The same hourly sweep that "
                      "de-identifies donated tracks removes this link once the route "
                      "is 28 hours old (within about 29 hours), whether or not you ever donated or "
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
                      "account id, a location for the award, the coarse H3 "
                      "resolution-8 area cell it falls in, the action and point value, and (for a "
                      "device-tied award) the vehicle id — indefinitely. "
                      "The location is exact for scooter-position awards "
                      "(report, QR scan, photo), ride-located awards (ride "
                      "start or end) and dibs referrals/stand-downs. The "
                      "profile-completion award stores only the centre of "
                      "the H3 resolution-8 cell around your saved Home or "
                      "Work. Profile-completion awards recorded before "
                      "2026-10-08 may still hold the exact Home or Work "
                      "location; we are removing those. "
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
            "data": "vehicle_state",
            "retention": "no personal data; current value only",
            "detail": "One row per vehicle, derived from the operator's "
                      "public vehicle feed, describing the vehicle and not "
                      "whoever rides it. Its newest fields (2026-10) record "
                      "how far the vehicle got during the rental in "
                      "progress, its feed id when that rental began, where "
                      "it was last seen parked, and whether each of its "
                      "last three rentals went anywhere. They hold no "
                      "account id or rider identity, and nothing is "
                      "appended over time: the rental fields are cleared "
                      "when the rental ends, the outcome record keeps only "
                      "the last three rentals, and the parked position is "
                      "overwritten every time the vehicle is seen. There is "
                      "no history of them for a cleanup job to prune.",
        },
        {
            "data": "device_photos",
            "retention": "indefinite (public content)",
            "detail": "Rider-uploaded photos of physical devices "
                      "(POST /api/v1/devices/{vid}/photos) are public content: "
                      "shown to other signed-in riders, capped at 3 per "
                      "device, attributed to the uploader's public username "
                      "if they show it, and served by link (anyone with a "
                      "link can open it while it works). Optional lat/lng "
                      "of where it was taken is stored. EXIF/GPS is stripped "
                      "from the image on upload. Kept indefinitely as "
                      "community reference material until removed; deleting "
                      "the account removes them from the app, but the image "
                      "file itself may not be deleted at the same time.",
        },
        {
            "data": "dibs",
            "retention": "indefinite (public while live)",
            "detail": "A dibs stores your display name (or public username), "
                      "the vehicle's name, plate and type, where you were, "
                      "and the time. A live dibs is visible to anyone, "
                      "including people without an account, via "
                      "/api/v1/dibs/live and the shareable /dibs/{id} "
                      "certificate, which stays reachable after expiry. "
                      "There is no deletion job.",
        },
        {
            "data": "referrals",
            "retention": "indefinite",
            "detail": "Someone at a dibs certificate (/dibs/{id}/refer, "
                      "/dibs/{id}/stand-down) may give us an email address or "
                      "phone number, their own or another person's. It is "
                      "stored with the claim's location and no expiry; a "
                      "stand-down texts that number a sign-in code. If "
                      "someone gave us your contact details, email "
                      "mhtc@z280.com to have them removed.",
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
                      "to the uploader. Images are deleted automatically "
                      "after 18 months.",
        },
        {
            "data": "telemetry_events",
            "retention": "90 days raw",
            "detail": "First-party, cookieless usage events from the web app "
                      "(which drawer opened, which mode, which wizard screen "
                      "— allowlisted names, no free text, no coordinates, no "
                      "ride content, no saved preference contents; some "
                      "events note which option of a fixed control was "
                      "picked, e.g. light/dark or a filter on/off). Each "
                      "event also carries a random per-tab session id "
                      "(sessionStorage), a millisecond timestamp, theme, "
                      "device class, OS family, viewport bucket and referrer "
                      "domain. No account id is "
                      "ever stored — only a signed-in yes/no flag. Visitor "
                      "counting uses sha256(daily salt + IP + user-agent); "
                      "the salt is destroyed after 2 days, after which the "
                      "hash cannot be recomputed by anyone. Neither the IP "
                      "nor the user-agent is stored. Opt out any time via "
                      "the About panel toggle (stored on your device); "
                      "browsers sending Global Privacy Control or Do Not "
                      "Track are opted out automatically.",
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
    "on_device": "Kept in your browser, not sent to us: session token, "
                 "settings, saved places (copied to your account, encrypted, "
                 "while signed in), recent places, dibs, watched scooters, "
                 "the live ride, unsent story drafts, and — if 'save tracks' "
                 "is on (the default) — recorded ride tracks with their "
                 "signing keys. A track leaves the device only if you donate "
                 "it. Clear via Account → Local Data or by clearing site data.",
    "processors": [
        {"name": "Google Identity Services", "purpose": "sign-in",
         "sees": "your email and verified flag, only if you sign in with Google"},
        {"name": "Postmark", "purpose": "sign-in emails (links and codes)",
         "sees": "your email address and the email content"},
        {"name": "z280 comms (our messaging service), a self-hosted SMS "
                 "gateway, and your mobile carrier",
         "purpose": "sign-in codes and service texts",
         "sees": "your phone number and the message text"},
        {"name": "Cloudflare", "purpose": "hosting, CDN, tunnel, map tiles, "
                                          "object storage",
         "sees": "network request metadata; stored images"},
        {"name": "OpenRouter", "purpose": "receipt reading — not yet active",
         "sees": "nothing today; if switched on, only an uploaded receipt "
                 "image or the text read from it"},
        {"name": "Sentry", "purpose": "error monitoring",
         "sees": "error reports, which may incidentally include request "
                 "metadata"},
    ],
    "contacted_by_your_browser": [
        {"name": "Google Identity Services",
         "when": "the sign-in script loads for every visitor who is not "
                 "signed in; Google may show One Tap",
         "sees": "IP and browser information; Google may read or set its "
                 "own cookies even if you never sign in"},
        {"name": "OpenStreetMap Foundation (Nominatim)",
         "when": "turning saved home/work/places or a parking-report "
                 "location into a street address",
         "sees": "those coordinates and your IP"},
        {"name": "We See You Veo (weseeyouveo.com)",
         "when": "a separate rider-advocacy site run by scooter.fyi's "
                 "founder; its logo loads on every page, story options load "
                 "on the story screen, and your story is sent only if you "
                 "tick to send it",
         "sees": "IP; if you send a story: your words, neighbourhood, time, "
                 "vehicle model, our rating of that scooter, source, and your "
                 "email only if you turn off anonymous. Governed by its own "
                 "privacy policy"},
        {"name": "sunrise-sunset.org", "when": "only with the sun-sync theme",
         "sees": "IP (fixed Denver coordinates are sent, not yours)"},
        {"name": "Links you follow (Veo app via Adjust, Google Maps, Veo "
                 "support)", "when": "only if you tap them",
         "sees": "whatever those services collect once you are there"},
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
