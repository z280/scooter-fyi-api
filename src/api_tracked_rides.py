"""Server-detected ride tracking (requirements items 5-9; sql/027_tracked_rides.sql).

    POST   /api/v1/tracked-rides                    start a ride + watch
    GET    /api/v1/tracked-rides                     owner-only paginated list
    GET    /api/v1/tracked-rides/active               the caller's one active ride, if any
    GET    /api/v1/tracked-rides/{ride_id}             one ride's full detail
    PATCH  /api/v1/tracked-rides/{ride_id}/end         rider-reported end (single-shot)
    POST   /api/v1/tracked-rides/{ride_id}/track       bulk track donation + verification (sql/051)
    POST   /api/v1/tracked-rides/{ride_id}/waypoints   append a waypoint (DEPRECATED, see below)
    GET    /api/v1/tracked-rides/{ride_id}/waypoints   paginated waypoint list
    DELETE /api/v1/tracked-rides/{ride_id}             hard-delete one ride
    DELETE /api/v1/tracked-rides                       hard-delete every ride the account owns

Deliberately separate from the `rides` table, which tracks OFF-FEED rides
on vehicles with no vehicle_identifier (src/api_rides.py,
sql/035_off_feed_rides.sql). Use this module when there IS a GBFS vehicle
to anchor to, that one when there isn't. Every endpoint here is
`require_session`
(open to all riders — signed-in is the only gate this product has).

RIDE SESSIONS (sql/049): a ride also carries the material ride mode needs
to record its GPS track locally and prove later that it did — a per-ride
HMAC key + nonce (`track_signing`), the rider's ride-mode option blob, a
feed-anchored start position/battery the rider cannot influence, and the
contribution-eligibility state (`validation`). track_key is a SECRET: it is
returned by the start call and by the two owner-only single-ride reads, and
never by the list endpoint — see _RIDE_COLS vs _RIDE_COLS_OWNER, where that
is structural rather than a redaction anyone has to remember.

TRACK DONATION (PLAN_RIDE_MODE_API.md phase A2, RIDE_MODE_OVERHAUL_PLAN.md
Part 2): ride mode records its GPS track LOCALLY (IndexedDB, hash-chained,
HMAC-signed batches) and sends nothing mid-ride — the chain is verified
server-side only at donation, POST .../track (sql/051). This SUPERSEDES the
old per-waypoint streaming transport: POST .../waypoints below has no known
client callers (the frontend never wired it) and is retained one release
purely as caution for unknown external callers. Waypoints it records stop
earning points as of A2 — the per-waypoint award (`credit_waypoint_points`)
was always granted at PATCH .../end, not by the waypoints endpoint itself,
so the supersession lives at /end (see that handler below), not here.

ANTI-FRAUD: the points system pays a bonus when the GBFS-observed
reappearance is within 20m of the rider's own reported end location. If a
rider could see the GBFS answer before submitting their report, they could
tune their guess to land inside that window — so the four `gbfs_*` fields
are nulled out in every response until `user_reported_ended_at` is set
(the underlying columns are always populated normally; this is a
response-layer redaction only), and the end-report endpoint is single-shot.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .accounts import SessionUser, require_session
from .battery_model import ingest_donated_observation
from .geo import distance_meters
from .identity import plate_display_code
from .pg import connection
# credit_waypoint_points / credit_gbfs_validation_points are RETAINED for
# history and their existing tests (tests/test_points_logic.py,
# tests/test_ride_hard_caps.py) but are no longer called from this module —
# PLAN_RIDE_MODE_API.md phase A2 supersedes both awards; see end_tracked_ride
# below and the module docstring's "TRACK DONATION" note.
from .points import credit_battery_contribution, credit_nav_distance_bonus
from .polyline import PolylineError, decode as decode_polyline, encode as encode_polyline
from .quality import compute_battery_percent
from .ratelimit import enforce
from .ride_limits import (
    MAX_LEG_METERS,
    MAX_RIDE_DISTANCE_METERS,
    clamp_distance,
    close_out_path as _close_out,
    leg_is_plausible,
    measure_path,
    partial_source,
)
from .track_verify import RideRow, verify_track_chain

router = APIRouter()

WATCH_DURATION_HOURS = 3
_LIMIT_START_RIDE_PER_ACCOUNT = (20, 3600)
_LIMIT_WAYPOINT_PER_ACCOUNT = (600, 3600)
_VEHICLE_IDENTIFIER_RE = r"^[0-9a-f]{16}$"

# Track donation (PLAN_RIDE_MODE_API.md phase A2). Body cap and batch-count
# cap sized against the longest honest ride, per the spec's own sanity
# check: the 3h watch window at 1Hz seals at most ~432 25-point batches
# (~650 KB of compact JWS), so 600 batches / 2 MB clears that with headroom
# while still bounding what one request can make this handler parse.
_LIMIT_TRACK_DONATION_PER_ACCOUNT = (6, 3600)
MAX_TRACK_DONATION_BYTES = 2 * 1024 * 1024
MAX_TRACK_DONATION_BATCHES = 600

# Ride-session signing material (sql/049). Per-ride, minted at start over
# the authenticated POST channel: a compromise is bounded to one ride and a
# key is never reused, so there is no rotation problem.
TRACK_SIGNING_ALG = "HS256"
TRACK_KEY_BYTES = 32    # base64url'd -> the JWS HMAC-SHA256 key
TRACK_NONCE_BYTES = 16  # hex'd -> seeds the rolling chain hash, H_-1 = sha256(nonce)

# ride_options is a client-owned blob: stored and echoed back verbatim, with
# the server reading only the booleans it gates on. The cap is measured on
# the SERIALIZED bytes for the same reason api_preferences.MAX_BLOB_BYTES is
# — the limit exists to bound what one account can make the database and
# every subsequent response carry, and that is a byte count.
MAX_RIDE_OPTIONS_BYTES = 4 * 1024
_RIDE_OPTION_BOOLS = (
    "cost_hud", "navigation", "save_tracks", "battery_modeling",
    "nav_improvement", "end_survey", "own_device",
)
_RIDE_OPTION_CHOICES = {
    "speedometer": ("classic", "digital", "none"),
    "theme": ("light", "dark", "auto"),
}

# How stale a feed observation may be and still describe "the vehicle the
# rider is standing next to right now". The ingest cadence is 2 minutes
# (crontab), so a vehicle actually in the feed is at most that old; 30
# minutes leaves room for a few missed cycles without ever letting the
# 48-hour raw_telemetry_points buffer hand back a position from before
# somebody else's ride. Past it we stamp NULL rather than a stale anchor —
# A2's start correlation falls back to the client-supplied start_lat/lon,
# which is the weaker check but an honest one.
FEED_START_MAX_AGE_MINUTES = 30

# sql/049's tracked_rides_validation_status_allowed, mirrored here so the
# provisional computation below cannot emit a value the column rejects.
VALIDATION_STATUSES = ("pending", "pending_feed", "eligible", "ineligible", "error")

_RIDE_COLS = (
    "id, status, started_at, start_lat, start_lon, watch_expires_at, "
    "gbfs_left_feed_at, gbfs_reappeared_at, gbfs_end_lat, gbfs_end_lon, "
    "gbfs_end_battery_percent, user_reported_ended_at, end_lat, end_lon, "
    "reported_battery_percent, total_cost_cents, metadata, path_polyline, "
    "vehicle_identifier, created_at, updated_at, distance_meters, "
    "distance_source, distance_clamped_from_m, reported_minutes, "
    "reported_plan, ride_options, validation_status, validation_reasons"
)
# THE SIGNING COLUMNS ARE NOT IN _RIDE_COLS, AND THAT IS THE ENFORCEMENT.
# track_key is a secret: anyone holding it can mint batches this ride will
# accept. The list endpoint selects _RIDE_COLS, so it does not even READ the
# key — track_signing cannot leak into a list response by someone forgetting
# to redact it, because there is nothing there to redact. The three
# single-ride owner-only responses (start, /active, /{id}) select
# _RIDE_COLS_OWNER and are the only places it appears.
_SIGNING_COLS = "track_key, track_nonce, track_key_issued_at"
_RIDE_COLS_OWNER = f"{_RIDE_COLS}, {_SIGNING_COLS}"
# Where the signing columns begin in an owner row. Derived from the string
# so it cannot drift out of step with an edit to either list.
_RIDE_COL_COUNT = _RIDE_COLS.count(",") + 1


class StartRideIn(BaseModel):
    vehicle_identifier: str = Field(..., min_length=16, max_length=16, pattern=_VEHICLE_IDENTIFIER_RE)
    start_lat: float = Field(..., ge=-90, le=90)
    start_lon: float = Field(..., ge=-180, le=180)
    # What the rider read off the vehicle's own display. Independent of the
    # feed-derived estimate the handler stamps alongside it (sql/049).
    reported_start_battery_percent: float | None = Field(default=None, ge=0, le=100)
    ride_options: dict[str, Any] | None = Field(
        default=None,
        description="Client-owned ride-mode options object, stored and returned verbatim.",
    )


class EndRideIn(BaseModel):
    ended_at: datetime
    end_lat: float = Field(..., ge=-90, le=90)
    end_lon: float = Field(..., ge=-180, le=180)
    reported_battery_percent: float | None = Field(default=None, ge=0, le=100)
    total_cost_cents: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] | None = Field(default=None)
    # FEATURE_PLAN §10. Inert stored facts: nothing in the close-out below
    # reads them, and reported_minutes is deliberately NOT reconciled
    # against user_reported_ended_at - started_at — a reported field exists
    # precisely so it can differ from what we observed. 1440 = 24 h, the
    # same "a number we won't stand behind doesn't enter the table" rule as
    # the 80 km distance cap.
    reported_minutes: int | None = Field(default=None, ge=0, le=1440)
    # The rate-plan tier the rider says they rode UNDER, which may
    # legitimately differ from the accounts.rate_plan they say they are ON.
    # Same vocabulary and same pydantic shape as api_rides.py's rate_plan.
    reported_plan: str | None = Field(default=None, pattern="^(resident|visitor|equity)$")


class WaypointIn(BaseModel):
    waypoint_at: datetime
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    metadata: dict[str, Any] | None = Field(default=None)


def _serialize_ride_options(options: dict[str, Any] | None) -> str:
    """JSON text for storage, shape- and size-checked.

    The blob is CLIENT-OWNED (sql/049): the server stores it, hands it back
    verbatim, and reads only the booleans it gates on. Two consequences that
    look inconsistent and are not:

    * The KNOWN keys are validated strictly. `save_tracks` gates whether a
      track may be donated at all and `battery_modeling` / `nav_improvement`
      / `end_survey` gate their awards, so a truthy string where a boolean
      belongs would silently decide a rider's eligibility. A gate the server
      acts on has to be the type it thinks it is.
    * UNKNOWN keys pass through untouched. The frontend owns this vocabulary
      and will add options; rejecting what this version has not heard of
      would put an API deploy in front of every new client-side toggle,
      which is exactly the cross-repo ordering edge the program plan avoids.
    """
    if options is None:
        return "{}"
    for key in _RIDE_OPTION_BOOLS:
        if key in options and not isinstance(options[key], bool):
            raise HTTPException(422, {
                "error": "bad_ride_options",
                "detail": f"ride_options.{key} must be true or false",
            })
    for key, allowed in _RIDE_OPTION_CHOICES.items():
        if key in options and options[key] not in allowed:
            raise HTTPException(422, {
                "error": "bad_ride_options",
                "detail": f"ride_options.{key} must be one of {list(allowed)}",
            })
    blob = json.dumps(options)
    if len(blob.encode("utf-8")) > MAX_RIDE_OPTIONS_BYTES:
        raise HTTPException(
            413,
            f"ride_options is larger than the {MAX_RIDE_OPTIONS_BYTES // 1024} KB limit",
        )
    return blob


def _provisional_validation(
    ride_options: Any, *, gbfs_reappeared_at: datetime | None,
) -> tuple[str, list[str]]:
    """The contribution eligibility we can already state at PATCH .../end.

    PROVISIONAL, and deliberately narrow: no track has been donated yet, so
    nothing here can reach 'eligible'. It answers only the questions the end
    report already settles, so the post-ride screen has something truthful to
    render instead of the bare 'pending' default. A2's donation handler and
    validation finisher own the authoritative status and overwrite this.

    Only two things are knowable now:

    * The rider never opted into saving tracks -> there will never be a
      track to donate, so this is TERMINAL: 'ineligible' /
      ['tracking_not_opted'], the same reason token A2's donation endpoint
      422s with.
    * The feed has not resolved where the vehicle reappeared -> the
      start/end correlation is undecidable, so 'pending_feed' (the post-ride
      screen's "waiting on validation from the live feed" branch).

    Otherwise the only thing outstanding is the rider's own donation, which
    is what 'pending' means.
    """
    opted = isinstance(ride_options, dict) and ride_options.get("save_tracks") is True
    if not opted:
        return "ineligible", ["tracking_not_opted"]
    if gbfs_reappeared_at is None:
        return "pending_feed", []
    return "pending", []


def _feed_start_observation(
    cur, vehicle_identifier: str,
) -> tuple[int | None, float | None, float | None]:
    """(battery_percent, lat, lon) from the vehicle's newest feed observation.

    All three NULL when the feed has no FRESH observation of this vehicle —
    which is the normal case for a rider who unlocked in the operator's app
    before hitting Start, since the vehicle leaves GBFS the moment it is
    rented.

    Source is raw_telemetry_points, not device_state: device_state carries no
    battery or range column (only max_observed_range_meters), so it cannot
    answer this. The battery derivation is compute_battery_percent over
    current_range_meters — the same call src/ride_watch.py makes to stamp
    gbfs_end_battery_percent, so the two ends of a ride are measured the same
    way and their difference means something.

    latitude/longitude are NUMERIC(9,6) (sql/001) and arrive as Decimal;
    they are cast to float here so what lands in the DOUBLE PRECISION
    feed_start_* columns matches what every other lat/lon in this module is.
    """
    cur.execute(
        """
        SELECT current_range_meters, latitude, longitude
        FROM raw_telemetry_points
        WHERE vehicle_identifier = %s
          AND snapshot_time >= NOW() - make_interval(mins => %s)
        ORDER BY snapshot_time DESC
        LIMIT 1
        """,
        (vehicle_identifier, FEED_START_MAX_AGE_MINUTES),
    )
    row = cur.fetchone()
    if row is None:
        return None, None, None
    current_range_meters, latitude, longitude = row
    return (
        compute_battery_percent(current_range_meters),
        float(latitude) if latitude is not None else None,
        float(longitude) if longitude is not None else None,
    )


def _track_signing(r: tuple, *, ride_id: str) -> dict[str, Any] | None:
    """The per-ride signing block, from an _SIGNING_COLS row slice.

    OWNER-ONLY, and never in a list response — see _RIDE_COLS_OWNER. None
    for a ride that predates sql/049 and therefore has no key: the client
    treats that as "this ride cannot be signed", not as an error.
    """
    track_key, track_nonce, track_key_issued_at = r
    if not track_key or not track_nonce:
        return None
    return {
        "alg": TRACK_SIGNING_ALG,
        "key_id": ride_id,
        "key": track_key,
        "nonce": track_nonce,
        "issued_at": track_key_issued_at.isoformat() if track_key_issued_at else None,
    }


def _survey_submitted_ids(cur, ride_ids: list) -> set[str]:
    """Which of `ride_ids` (raw tracked_rides.id values, straight off a
    fetched row) already have a ride_surveys row (PLAN_RIDE_MODE_API.md
    phase A3, sql/052) — one batched query per response rather than one
    per ride, so a list response of N rides costs one extra round trip,
    not N.

    Guarded on `to_regclass('ride_surveys')` the same way A2's
    donate_track / src/cli.py:deidentify_donations guard on
    `to_regclass('ride_routes')`: this read touches EVERY ride payload
    (start/list/active/get), not just the new survey endpoint, so it must
    not 500 every pre-existing ride response if this lane's PR reaches
    production ahead of the migration that creates ride_surveys.
    """
    if not ride_ids:
        return set()
    cur.execute("SELECT to_regclass('ride_surveys')")
    (relid,) = cur.fetchone()
    if relid is None:
        return set()
    cur.execute(
        "SELECT tracked_ride_id FROM ride_surveys WHERE tracked_ride_id = ANY(%s)",
        (list(ride_ids),),
    )
    return {str(r[0]) for r in cur.fetchall()}


def _owner_ride(
    r: tuple, *, path_geojson: bool = True, survey_submitted: bool = False,
) -> dict[str, Any]:
    """An _RIDE_COLS_OWNER row as a response: the ride plus track_signing."""
    ride = _row_to_ride(
        r[:_RIDE_COL_COUNT], path_geojson=path_geojson, survey_submitted=survey_submitted)
    ride["track_signing"] = _track_signing(r[_RIDE_COL_COUNT:], ride_id=ride["id"])
    return ride


def _row_to_ride(
    r: tuple, *, path_geojson: bool = True, survey_submitted: bool = False,
) -> dict[str, Any]:
    (ride_id, status, started_at, start_lat, start_lon, watch_expires_at,
     gbfs_left_feed_at, gbfs_reappeared_at, gbfs_end_lat, gbfs_end_lon,
     gbfs_end_battery_percent, user_reported_ended_at, end_lat, end_lon,
     reported_battery_percent, total_cost_cents, metadata, path_polyline,
     vehicle_identifier, created_at, updated_at, ride_distance_meters,
     distance_source, distance_clamped_from_m, reported_minutes,
     reported_plan, ride_options, validation_status, validation_reasons) = r

    # ANTI-FRAUD: see module docstring. Redacted as None in the API
    # response only — the underlying columns are untouched.
    reported = user_reported_ended_at is not None
    out = {
        "id": str(ride_id),
        "status": status,
        "started_at": started_at.isoformat(),
        "start_lat": start_lat,
        "start_lon": start_lon,
        "watch_expires_at": watch_expires_at.isoformat(),
        "gbfs_left_feed_at": gbfs_left_feed_at.isoformat() if (reported and gbfs_left_feed_at) else None,
        "gbfs_reappeared_at": gbfs_reappeared_at.isoformat() if (reported and gbfs_reappeared_at) else None,
        "gbfs_end_lat": gbfs_end_lat if reported else None,
        "gbfs_end_lon": gbfs_end_lon if reported else None,
        "gbfs_end_battery_percent": gbfs_end_battery_percent if reported else None,
        "user_reported_ended_at": user_reported_ended_at.isoformat() if user_reported_ended_at else None,
        "end_lat": end_lat,
        "end_lon": end_lon,
        "reported_battery_percent": float(reported_battery_percent) if reported_battery_percent is not None else None,
        "total_cost_cents": total_cost_cents,
        "metadata": metadata if isinstance(metadata, dict) else {},
        "vehicle_identifier": vehicle_identifier,
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        # Not redacted with the gbfs_* fields above: this is derived from
        # the rider's OWN waypoints or their own reported end, so showing
        # it back to them reveals nothing they didn't tell us.
        "distance_meters": (
            round(float(ride_distance_meters), 1)
            if ride_distance_meters is not None else None
        ),
        "distance_source": distance_source,
        # NULL unless the operator's 80 km ride cap bound; see api_rides.py.
        # Derived from the rider's own data like distance itself, so it is
        # not part of the gbfs_* redaction.
        "distance_clamped_from_m": (
            round(float(distance_clamped_from_m), 1)
            if distance_clamped_from_m is not None else None
        ),
        # FEATURE_PLAN §10 — the rider's own report, so not part of the
        # gbfs_* redaction: showing it back reveals nothing they didn't say.
        "reported_minutes": reported_minutes,
        "reported_plan": reported_plan,
        # sql/049. The options blob is echoed verbatim; validation is the
        # contribution-eligibility state the post-ride screen renders from.
        "ride_options": ride_options if isinstance(ride_options, dict) else {},
        "validation": {
            "status": validation_status,
            "reasons": validation_reasons if isinstance(validation_reasons, list) else [],
        },
        # PLAN_RIDE_MODE_API.md phase A3 (sql/052, src/api_ride_surveys.py):
        # an EXISTS against ride_surveys, computed by the caller and passed
        # in — NOT redacted like track_signing, since whether a survey was
        # submitted reveals nothing the rider didn't do themselves. Included
        # in every ride payload this function builds (single ride, active,
        # and list).
        "survey_submitted": survey_submitted,
    }
    if path_geojson:
        out["path_polyline"] = path_polyline
        if path_polyline:
            try:
                coords = [[lon, lat] for lat, lon in decode_polyline(path_polyline)]
            except PolylineError:
                coords = []
            out["path_geojson"] = {"type": "LineString", "coordinates": coords}
        else:
            out["path_geojson"] = None
    return out


def _plate_display_code_for(cur, vehicle_identifier: str) -> str | None:
    cur.execute(
        "SELECT vehicle_plate FROM device_state WHERE vehicle_identifier = %s",
        (vehicle_identifier,),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    return plate_display_code(row[0])


def _parse_ride_id(ride_id: str) -> UUID:
    try:
        return UUID(ride_id)
    except ValueError:
        raise HTTPException(400, "ride id must be a UUID")


def _parse_before(before: str | None, field: str = "before") -> datetime | None:
    if not before:
        return None
    try:
        parsed = datetime.fromisoformat(before.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(400, f"bad {field} timestamp: {e}")
    if parsed.tzinfo is None:
        raise HTTPException(400, f"{field} must include a timezone (e.g. trailing Z)")
    return parsed


def _track_points(cur, rid: UUID) -> list[tuple[float, float]]:
    """The ride's waypoints as (lat, lon), oldest first. Read whole rather
    than appended incrementally because waypoints can arrive out of order
    (client retry/offline buffering)."""
    cur.execute(
        "SELECT lat, lon FROM ride_waypoints WHERE tracked_ride_id = %s "
        "ORDER BY waypoint_at ASC, id ASC",
        (str(rid),),
    )
    return [(r[0], r[1]) for r in cur.fetchall()]


def _measured_path(
    start_lat: float | None, start_lon: float | None,
    track: list[tuple[float, float]],
    end_lat: float | None = None, end_lon: float | None = None,
) -> list[tuple[float, float]]:
    """The full path we are willing to claim we measured, in order:

        ride start -> every uploaded GPS fix -> rider-reported end

    BOTH ends matter, for the same reason. The rider was already moving
    between where they started and wherever their first GPS fix landed, and
    they kept moving between their LAST fix and where they parked — a phone
    that backgrounded, saved battery or went through a tunnel stops
    producing fixes long before the ride stops. Dropping either leg
    undercounts the ride by a sampling gap, and the trailing gap is
    routinely the whole ride.

    Byte-for-byte the same rule as src/api_rides.py:_measured_path, and it
    has to stay that way: src/badges.py sums distance across both tables, so
    a rider's mileage must not depend on which mechanism logged the ride.

    Callers now pass only start + track; closing the path with the reported
    end is ride_limits.close_out_path's job, which both tables share so the
    "must stay that way" above is enforced by there being one copy rather
    than by whoever edits next remembering.
    """
    points: list[tuple[float, float]] = []
    if start_lat is not None and start_lon is not None:
        points.append((start_lat, start_lon))
    points.extend(track)
    if end_lat is not None and end_lon is not None:
        points.append((end_lat, end_lon))
    return points


def _ordered_track(cur, rid: UUID) -> list[tuple[datetime, float, float]]:
    """(waypoint_at, lat, lon), oldest first — _track_points plus the
    timestamp, so a new fix can be placed where it will actually land."""
    cur.execute(
        "SELECT waypoint_at, lat, lon FROM ride_waypoints "
        "WHERE tracked_ride_id = %s ORDER BY waypoint_at ASC, id ASC",
        (str(rid),),
    )
    return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def _prospective_path(
    cur, rid: UUID, start_lat: float | None, start_lon: float | None,
    new_at: datetime, new_lat: float, new_lon: float,
) -> tuple[list[tuple[float, float]], int]:
    """The path this ride WOULD have if `new` were appended, and the index
    the new point takes in it. Same contract and same reasoning as
    src/api_rides.py:_prospective_path — waypoints arrive out of order, so
    a new fix can land mid-path and create two new adjacencies."""
    existing = _ordered_track(cur, rid)
    idx = sum(1 for at, _, _ in existing if at <= new_at)
    track = [(lat, lon) for _, lat, lon in existing]
    track.insert(idx, (new_lat, new_lon))
    points = _measured_path(start_lat, start_lon, track)
    lead = 1 if (start_lat is not None and start_lon is not None) else 0
    return points, idx + lead


def _check_appendable(points: list[tuple[float, float]], idx: int) -> None:
    """Operator leg cap + ride cap, enforced at append.

    Byte-for-byte the same rule as src/api_rides.py:_check_appendable, and
    it has to stay that way: src/badges.py sums distance across both
    tables, so what each will record must not depend on which one you are
    talking to.
    """
    for a, b in ((idx - 1, idx), (idx, idx + 1)):
        if a < 0 or b >= len(points):
            continue
        if not leg_is_plausible(points[a], points[b]):
            gap = distance_meters(*points[a], *points[b])
            raise HTTPException(422, {
                "error": "waypoint_too_far",
                "detail": f"this fix is {gap:.0f} m from the adjacent point on "
                          f"the ride's path, above the {MAX_LEG_METERS:.0f} m "
                          "limit between consecutive points. The fix was not "
                          "recorded; the ride is still active and the next one "
                          "will be accepted normally.",
            })

    measured, _ = measure_path(points, cap_legs=True)
    if measured > MAX_RIDE_DISTANCE_METERS:
        raise HTTPException(422, {
            "error": "ride_distance_cap_reached",
            "detail": f"this fix would put the ride at {measured:.0f} m, above "
                      f"the {MAX_RIDE_DISTANCE_METERS:.0f} m limit for a single "
                      "ride. The fix was not recorded. End this ride and start "
                      "a new one to keep logging.",
        })


@router.post("/api/v1/tracked-rides")
def start_ride(
    user: SessionUser = Depends(require_session),
    payload: StartRideIn = Body(...),
) -> dict[str, Any]:
    # Validated before the connection is taken: a malformed options blob is
    # a client bug, not a reason to hold a pooled connection open.
    ride_options = _serialize_ride_options(payload.ride_options)

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="tracked_ride_start_account", key=str(user.account_id),
                    limit=_LIMIT_START_RIDE_PER_ACCOUNT[0],
                    window_seconds=_LIMIT_START_RIDE_PER_ACCOUNT[1])

            cur.execute(
                "SELECT 1 FROM device_state WHERE vehicle_identifier = %s",
                (payload.vehicle_identifier,),
            )
            if cur.fetchone() is None:
                raise HTTPException(404, "unknown vehicle_identifier")

            # Advisory-lock the account to close the TOCTOU where two
            # near-simultaneous start requests both pass the active-ride
            # check before either commits (mirrors ratelimit.enforce's own
            # check-then-act technique).
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"tracked_ride_start:{user.account_id}",),
            )
            cur.execute(
                """
                SELECT 1 FROM tracked_rides
                WHERE account_id = %s AND user_reported_ended_at IS NULL
                  AND gbfs_reappeared_at IS NULL AND watch_expires_at > NOW()
                LIMIT 1
                """,
                (user.account_id,),
            )
            if cur.fetchone() is not None:
                raise HTTPException(409, "an active ride already exists")

            # The feed-anchored start (sql/049), stamped from the vehicle's
            # newest fresh observation. The rider cannot supply or influence
            # any of these three, which is the whole point: they are the
            # anti-fabrication anchor a donated track is correlated against.
            (feed_battery, feed_lat, feed_lon) = _feed_start_observation(
                cur, payload.vehicle_identifier)

            # Per-ride signing material. token_urlsafe(32) is base64url over
            # 32 random bytes; token_hex(16) is 16 random bytes as hex —
            # exactly the two shapes the chain format specifies.
            track_key = secrets.token_urlsafe(TRACK_KEY_BYTES)
            track_nonce = secrets.token_hex(TRACK_NONCE_BYTES)

            cur.execute(
                """
                INSERT INTO tracked_rides (
                    account_id, vehicle_identifier, start_lat, start_lon, watch_expires_at,
                    reported_start_battery_percent, ride_options,
                    feed_start_battery_percent, feed_start_lat, feed_start_lon,
                    track_key, track_nonce, track_key_issued_at
                ) VALUES (%s, %s, %s, %s, NOW() + make_interval(hours => %s),
                          %s, %s::jsonb, %s, %s, %s, %s, %s, NOW())
                RETURNING id, watch_expires_at
                """,
                (user.account_id, payload.vehicle_identifier,
                 payload.start_lat, payload.start_lon, WATCH_DURATION_HOURS,
                 payload.reported_start_battery_percent, ride_options,
                 feed_battery, feed_lat, feed_lon, track_key, track_nonce),
            )
            ride_id, watch_expires_at = cur.fetchone()
            cur.execute(
                """
                INSERT INTO user_device_watch_list (
                    tracked_ride_id, account_id, vehicle_identifier, watch_expires_at
                ) VALUES (%s, %s, %s, %s)
                """,
                (str(ride_id), user.account_id, payload.vehicle_identifier, watch_expires_at),
            )
            # _RIDE_COLS_OWNER: the start response carries track_signing, and
            # is one of only three places that does.
            cur.execute(
                f"SELECT {_RIDE_COLS_OWNER} FROM tracked_rides WHERE id = %s",
                (str(ride_id),),
            )
            ride = _owner_ride(cur.fetchone())
            ride["plate_display_code"] = _plate_display_code_for(cur, payload.vehicle_identifier)
        conn.commit()
    return ride


@router.get("/api/v1/tracked-rides")
def list_tracked_rides(
    user: SessionUser = Depends(require_session),
    limit: int = Query(50, ge=1, le=500),
    before: str | None = Query(None, description="ISO timestamp — return rides started before this"),
    status: str | None = Query(None, pattern="^(watching|left_feed|completed|expired)$"),
) -> dict[str, Any]:
    where = ["account_id = %s"]
    params: list[Any] = [user.account_id]
    parsed_before = _parse_before(before)
    if parsed_before is not None:
        where.append("started_at < %s")
        params.append(parsed_before)
    if status is not None:
        where.append("status = %s")
        params.append(status)
    params.append(limit)

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_RIDE_COLS} FROM tracked_rides
                WHERE {' AND '.join(where)}
                ORDER BY started_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
            submitted_ids = _survey_submitted_ids(cur, [r[0] for r in rows])
    # path_geojson omitted here (list view) to keep a multi-ride response
    # bounded — a ride with a long path would otherwise bloat every list
    # call. Full path is available from GET /{ride_id}.
    rides = [
        _row_to_ride(r, path_geojson=False, survey_submitted=str(r[0]) in submitted_ids)
        for r in rows
    ]
    return {"count": len(rides), "rides": rides}


@router.get("/api/v1/tracked-rides/active")
def active_tracked_ride(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    """Registered before /{ride_id} on purpose — Starlette matches
    path-shaped routes in registration order, so 'active' would otherwise
    be swallowed as a {ride_id} value. The same hazard applies to
    /{ride_id}/waypoints and /{ride_id}/screenshots below."""
    with connection() as conn:
        with conn.cursor() as cur:
            # _RIDE_COLS_OWNER: a client that reloaded mid-ride resumes
            # signing from the track_signing block this returns.
            cur.execute(
                f"""
                SELECT {_RIDE_COLS_OWNER} FROM tracked_rides
                WHERE account_id = %s AND user_reported_ended_at IS NULL
                  AND gbfs_reappeared_at IS NULL AND watch_expires_at > NOW()
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (user.account_id,),
            )
            row = cur.fetchone()
            if row is None:
                return {"active": None}
            submitted_ids = _survey_submitted_ids(cur, [row[0]])
            ride = _owner_ride(row, survey_submitted=str(row[0]) in submitted_ids)
            ride["plate_display_code"] = _plate_display_code_for(cur, ride["vehicle_identifier"])
    return {"active": ride}


@router.get("/api/v1/tracked-rides/{ride_id}")
def get_tracked_ride(
    ride_id: str,
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    rid = _parse_ride_id(ride_id)
    with connection() as conn:
        with conn.cursor() as cur:
            # Already owner-scoped by the account_id predicate, which is what
            # makes it safe to return _RIDE_COLS_OWNER's track_signing here.
            cur.execute(
                f"SELECT {_RIDE_COLS_OWNER} FROM tracked_rides WHERE id = %s AND account_id = %s",
                (str(rid), user.account_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "no such ride")
            submitted_ids = _survey_submitted_ids(cur, [row[0]])
            ride = _owner_ride(row, survey_submitted=str(row[0]) in submitted_ids)
            ride["plate_display_code"] = _plate_display_code_for(cur, ride["vehicle_identifier"])
    return ride


@router.patch("/api/v1/tracked-rides/{ride_id}/end")
def end_tracked_ride(
    ride_id: str,
    user: SessionUser = Depends(require_session),
    payload: EndRideIn = Body(...),
) -> dict[str, Any]:
    rid = _parse_ride_id(ride_id)
    if payload.ended_at.tzinfo is None:
        raise HTTPException(400, "ended_at must include a UTC offset")

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_reported_ended_at, vehicle_identifier, "
                "gbfs_reappeared_at, gbfs_end_lat, gbfs_end_lon, "
                "start_lat, start_lon, ride_options "
                "FROM tracked_rides WHERE id = %s AND account_id = %s FOR UPDATE",
                (str(rid), user.account_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "no such ride")
            (already_ended, vehicle_identifier, gbfs_reappeared_at,
             gbfs_end_lat, gbfs_end_lon, start_lat, start_lon, ride_options) = row
            if already_ended is not None:
                raise HTTPException(409, "this ride's end has already been reported")

            # Provisional contribution eligibility, computed off the row this
            # transaction has already locked. Written in the same UPDATE as
            # everything else the end report settles. A2's donation handler
            # and validation finisher own the authoritative status.
            validation_status, validation_reasons = _provisional_validation(
                ride_options, gbfs_reappeared_at=gbfs_reappeared_at)
            # Stamped only when the status is SETTLED, so a later reader can
            # tell "decided" from "defaulted". 'ineligible' here means the
            # rider never opted into saving tracks, which no later event can
            # undo; 'pending'/'pending_feed' are still waiting on something.
            validated_at = (
                datetime.now(timezone.utc) if validation_status == "ineligible" else None
            )

            # Measure the WHOLE path, start -> fixes -> reported end. The end
            # report is the last thing we learn about the ride, so it is the
            # only chance to close the trailing sampling gap; keeping the
            # distance the last waypoint upload happened to leave behind
            # meant the final leg was never measured at all.
            #
            # distance_source stays honest about how the number was reached:
            # 'waypoints' when the rider actually handed us a track,
            # 'straight_line' when the only two points we have are the ends
            # — which undercounts any route that isn't straight (sql/034).
            # NOTHING BELOW THIS LINE CAN REFUSE THE END REPORT. An
            # implausible final leg is dropped and an over-cap distance is
            # clamped; the ride completes either way. Refusing would strand
            # the rider — the active-ride predicate would keep answering
            # "you are still on a ride" until the watch window elapsed.
            track = _track_points(cur, rid)
            points, new_distance, new_source, clamped_from = _close_out(
                start_lat, start_lon, track, payload.end_lat, payload.end_lon)
            # Re-encode the stored path over the same points the distance was
            # measured over, so polyline and distance can't disagree. A ride
            # with no track keeps path_polyline NULL rather than gaining a
            # fabricated two-point "route" it never observed.
            path_sql = "path_polyline = %s," if track else ""
            path_params: tuple = (encode_polyline(points),) if track else ()

            cur.execute(
                f"""
                UPDATE tracked_rides SET
                    status = 'completed',
                    user_reported_ended_at = %s,
                    end_lat = %s,
                    end_lon = %s,
                    reported_battery_percent = %s,
                    total_cost_cents = %s,
                    metadata = %s::jsonb,
                    {path_sql}
                    distance_meters = %s,
                    distance_source = %s,
                    distance_clamped_from_m = %s,
                    -- §10's inert reported facts and sql/049's provisional
                    -- validation. Deliberately LAST rather than beside
                    -- reported_battery_percent where they read most
                    -- naturally: the SET order is semantically irrelevant,
                    -- but the parameter order is not — this module's tests
                    -- assert on positional indices into the tuple below, and
                    -- appending leaves every pre-existing field where it was.
                    reported_minutes = %s,
                    reported_plan = %s,
                    validation_status = %s,
                    validation_reasons = %s::jsonb,
                    validated_at = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (payload.ended_at, payload.end_lat, payload.end_lon,
                 payload.reported_battery_percent, payload.total_cost_cents,
                 json.dumps(payload.metadata or {}), *path_params,
                 new_distance, new_source, clamped_from,
                 payload.reported_minutes, payload.reported_plan,
                 validation_status, json.dumps(validation_reasons), validated_at,
                 str(rid)),
            )

            # Points (requirement #10) — SUPERSEDED as of PLAN_RIDE_MODE_API.md
            # phase A2 (RIDE_MODE_OVERHAUL_PLAN.md Decision 6 / Risk 5): PATCH
            # .../end no longer awards `waypoint` or `gbfs_trip_validated`.
            # GBFS alignment is now an ELIGIBILITY GATE (_provisional_validation
            # above / src/track_verify.py), not an award; the reshaped
            # ride-mode awards (battery_contribution, nav_route_feedback,
            # nav_qualitative_feedback, nav_distance_bonus, ride_survey) are
            # credited from POST .../track and POST .../survey instead
            # (src/points.py). credit_waypoint_points and
            # credit_gbfs_validation_points are kept, UNUSED here, for history
            # and their existing unit tests — do not delete them from
            # src/points.py.

            cur.execute(f"SELECT {_RIDE_COLS} FROM tracked_rides WHERE id = %s", (str(rid),))
            ride = _row_to_ride(cur.fetchone())
        conn.commit()
    return ride


def _vehicle_model_for(cur, vehicle_identifier: str) -> str | None:
    """device_state.current_vehicle_model_name (sql/016) for the ride's
    vehicle, at donation time — Astro/Cosmo/Apollo, capitalized
    (src/ingest.py:_KNOWN_VEHICLE_TYPES), or None for an unconfirmed
    model. Stamped once onto track_donations.vehicle_model so it survives
    the de-id sweep (the battery model needs it after account linkage is
    gone), same source A3's surveys read."""
    cur.execute(
        "SELECT current_vehicle_model_name FROM device_state WHERE vehicle_identifier = %s",
        (vehicle_identifier,),
    )
    row = cur.fetchone()
    return row[0] if row else None


@router.post("/api/v1/tracked-rides/{ride_id}/track")
async def donate_track(
    ride_id: str,
    request: Request,
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    """Bulk track donation + server-side verification (PLAN_RIDE_MODE_API.md
    phase A2, RIDE_MODE_OVERHAUL_PLAN.md Part 2) — the sole track upload
    path; ride mode never transmits mid-ride. Composes four independently
    built pieces in one transaction:

        track_verify.verify_track_chain  — signature/chain/monotonic/speed/
                                            GBFS/volume verification (pure)
        sql/051 tables                   — track_donations + donated_track_points
        points.credit_battery_contribution / credit_nav_distance_bonus
                                          — the reshaped ride-mode awards
        battery_model.ingest_donated_observation
                                          — the battery-model feedback loop

    Body cap and parsing are handled BEFORE the connection is taken (a
    malformed/oversized body is a client bug, not a reason to hold a pooled
    connection open) — same rule _serialize_ride_options follows above.
    """
    rid = _parse_ride_id(ride_id)

    def _too_large() -> HTTPException:
        return HTTPException(413, {
            "error": "donation_too_large",
            "detail": f"donation body exceeds the "
                      f"{MAX_TRACK_DONATION_BYTES // (1024 * 1024)} MB limit",
        })

    # A dishonest/missing Content-Length can't be trusted, but an HONEST
    # oversized one should reject before reading anything at all. The real
    # bound is the streamed read below, which aborts as soon as the
    # ACCUMULATED size crosses the cap — `await request.body()` used to
    # buffer the entire request into memory before this check ever ran,
    # which meant the 2 MB limit protected nothing against a very large or
    # slow request.
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) > MAX_TRACK_DONATION_BYTES:
                raise _too_large()
        except ValueError:
            pass  # non-numeric Content-Length: fall through to the bounded read

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_TRACK_DONATION_BYTES:
            raise _too_large()
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "malformed JSON body")
    batches = body.get("batches") if isinstance(body, dict) else None
    if not isinstance(batches, list) or not all(isinstance(b, str) for b in batches):
        raise HTTPException(422, {
            "error": "bad_batches",
            "detail": "`batches` must be a list of compact-JWS strings",
        })
    if len(batches) > MAX_TRACK_DONATION_BATCHES:
        raise HTTPException(413, {
            "error": "too_many_batches",
            "detail": f"at most {MAX_TRACK_DONATION_BATCHES} batches per donation",
        })

    with connection() as conn:
        with conn.cursor() as cur:
            # The transaction OPENS with this lock — PLAN_RIDE_MODE_API.md is
            # explicit: finalize_validation (src/ride_watch.py) takes the
            # SAME `ride_validation:<ride_id>` lock before touching the ride
            # row, so a ride_watch resolve landing mid-donation serializes
            # against this transaction instead of racing it (the finisher
            # would otherwise look for a donation row that hasn't committed
            # yet, or the two would deadlock if locking order ever
            # disagreed — see ride_watch.py's own ADVISORY-LOCK ORDERING
            # note).
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"ride_validation:{rid}",),
            )
            enforce(cur, bucket="track_donation_account", key=str(user.account_id),
                    limit=_LIMIT_TRACK_DONATION_PER_ACCOUNT[0],
                    window_seconds=_LIMIT_TRACK_DONATION_PER_ACCOUNT[1])

            cur.execute(
                """
                SELECT user_reported_ended_at, track_donated_at, ride_options,
                       track_key, track_nonce, track_key_issued_at,
                       start_lat, start_lon, feed_start_lat, feed_start_lon,
                       gbfs_left_feed_at, gbfs_reappeared_at,
                       gbfs_end_lat, gbfs_end_lon, vehicle_identifier,
                       feed_start_battery_percent, reported_start_battery_percent,
                       reported_battery_percent
                FROM tracked_rides WHERE id = %s AND account_id = %s FOR UPDATE
                """,
                (str(rid), user.account_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "no such ride")
            (user_reported_ended_at, track_donated_at, ride_options,
             track_key, track_nonce, track_key_issued_at,
             start_lat, start_lon, feed_start_lat, feed_start_lon,
             gbfs_left_feed_at, gbfs_reappeared_at,
             gbfs_end_lat, gbfs_end_lon, vehicle_identifier,
             feed_start_battery_percent, reported_start_battery_percent,
             reported_battery_percent) = row

            if user_reported_ended_at is None:
                raise HTTPException(409, {
                    "error": "ride_not_ended",
                    "detail": "report this ride's end (PATCH .../end) before donating its track",
                })
            if track_donated_at is not None:
                raise HTTPException(409, {
                    "error": "already_donated",
                    "detail": "this ride's track has already been donated",
                })
            opted = isinstance(ride_options, dict) and ride_options.get("save_tracks") is True
            if not opted:
                raise HTTPException(422, {
                    "error": "tracking_not_opted",
                    "detail": "this ride's ride_options.save_tracks was not on",
                })

            ride_row = RideRow(
                id=str(rid),
                track_key=track_key, track_nonce=track_nonce,
                track_key_issued_at=track_key_issued_at,
                user_reported_ended_at=user_reported_ended_at,
                start_lat=start_lat, start_lon=start_lon,
                feed_start_lat=feed_start_lat, feed_start_lon=feed_start_lon,
                gbfs_left_feed_at=gbfs_left_feed_at,
                gbfs_reappeared_at=gbfs_reappeared_at,
                gbfs_end_lat=gbfs_end_lat, gbfs_end_lon=gbfs_end_lon,
            )
            result = verify_track_chain(cur, ride_row, batches)

            # chain_invalid (checks 1/2: signature or chain integrity) is
            # one of two outcomes that REJECT the submission outright rather
            # than accepting-and-deciding: the chain isn't trustworthy
            # enough to even attribute to this ride, so nothing is written
            # and the one-donation-per-ride slot (track_donated_at) is not
            # consumed — a client that had a genuine upload bug can retry.
            # Every other real-verdict outcome (ineligible for an actual
            # reason, pending_feed, eligible) IS an accepted donation.
            if "chain_invalid" in result.reasons:
                raise HTTPException(422, {
                    "error": "chain_invalid",
                    "failing_check": "chain",
                    "batch_seq": result.failing_batch_seq,
                    "detail": "the submitted track chain failed verification "
                              "(bad signature, wrong ride binding, or a broken hash chain)",
                })

            # verdict="error" (track_verify.py's own defensive catch-all for
            # an exception inside the verifier itself -- the module claims
            # this should never actually happen against real input) is the
            # SECOND rejection case, for a reason the chain_invalid branch
            # above doesn't cover: chain_root_hash is None here too, but
            # track_donations.chain_root_hash is NOT NULL (sql/051) -- there
            # is nothing honest to persist as an "audit anchor" for a chain
            # that was never actually verified. Respond with the SAME shape
            # donate_track otherwise returns (200, not a client-facing error
            # code) so Screen 10 can still render its "there was an internal
            # error" branch -- this is OUR bug, not the client's, so (unlike
            # chain_invalid) the donation slot is deliberately left open for
            # a retry once it's fixed.
            if result.verdict == "error":
                response = result.as_response()
                response["donation_id"] = None
                response["distance_meters"] = 0.0
                response["waypoint_count"] = 0
                response["points"] = []
                return response

            # Defensive bounds check on the flattened, chain-verified track
            # before persisting it: verify_track_chain's own parsing
            # (checks 1/2) accepts any numeric lat/lon — physical plausibility
            # (check 4) and GBFS correlation (check 5) catch a wildly wrong
            # position, but neither guarantees every waypoint stays inside
            # donated_track_points' lat/lon CHECK bounds. Reject here rather
            # than let a bad row 500 out of the INSERT below.
            for _ms, wp_lat, wp_lon, _acc in result.waypoints:
                if not (-90 <= wp_lat <= 90 and -180 <= wp_lon <= 180):
                    raise HTTPException(422, {
                        "error": "chain_invalid",
                        "detail": "a waypoint in the submitted chain is outside "
                                  "valid latitude/longitude bounds",
                    })

            vehicle_model = _vehicle_model_for(cur, vehicle_identifier)

            # `verification` is per_check PLUS points_status: track_verify.py
            # computes points_status ("ok" | "pending_review") only from the
            # raw batches, which are discarded right after verification — so
            # if this donation settles late as pending_feed -> eligible,
            # finalize_validation (src/ride_watch.py) has to read the flag
            # back from here rather than recomputing it, since there is
            # nothing left to recompute it FROM. Purely additive: the HTTP
            # response's own "verification" object still comes straight off
            # result.per_check (via result.as_response(), below) and never
            # carries this extra key.
            stored_verification = dict(result.per_check)
            stored_verification["points_status"] = result.points_status

            cur.execute(
                """
                INSERT INTO track_donations (
                    tracked_ride_id, account_id, vehicle_model, chain_root_hash,
                    batch_count, waypoint_count, distance_meters, verification
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id, donated_at
                """,
                (str(rid), user.account_id, vehicle_model, result.chain_root_hash,
                 len(batches), result.waypoint_count, result.distance_meters,
                 json.dumps(stored_verification)),
            )
            donation_id, donated_at = cur.fetchone()

            if result.waypoints:
                cur.executemany(
                    "INSERT INTO donated_track_points "
                    "(donation_id, seq, recorded_ms, lat, lon, accuracy_m) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    [
                        (str(donation_id), seq, ms, lat, lon, acc)
                        for seq, (ms, lat, lon, acc) in enumerate(result.waypoints)
                    ],
                )

            # verdict != "pending_feed" -> settled now (eligible, ineligible
            # for a non-chain reason, or the internal "error" verdict — all
            # terminal); "pending_feed" -> still waiting on the live feed,
            # finished later by finalize_validation. Same validated_at rule
            # PATCH .../end and finalize_validation both already follow.
            validated_at = (
                datetime.now(timezone.utc) if result.verdict != "pending_feed" else None
            )
            cur.execute(
                """
                UPDATE tracked_rides SET
                    track_donated_at = %s,
                    validation_status = %s,
                    validation_reasons = %s::jsonb,
                    validated_at = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (donated_at, result.verdict, json.dumps(result.reasons),
                 validated_at, str(rid)),
            )

            # Points: only on an outright "eligible" verdict, and only when
            # points_status isn't "pending_review" (track_verify.py: >10%
            # of segments sustained above the fast-but-not-implausible
            # threshold — held pending manual review, not auto-credited).
            # A "pending_feed" donation's distance-dependent points are
            # HELD here and awarded later by finalize_validation on a late
            # eligible settle, per this same gating (see ride_watch.py).
            own_device = isinstance(ride_options, dict) and ride_options.get("own_device") is True
            battery_modeling_on = (
                isinstance(ride_options, dict) and ride_options.get("battery_modeling") is True
            )
            nav_improvement_on = (
                isinstance(ride_options, dict) and ride_options.get("nav_improvement") is True
            )
            soc_start = (
                feed_start_battery_percent if feed_start_battery_percent is not None
                else reported_start_battery_percent
            )
            both_batteries_known = soc_start is not None and reported_battery_percent is not None

            points_awarded: list[dict[str, Any]] = []
            may_award = result.verdict == "eligible" and result.points_status == "ok"

            if may_award and battery_modeling_on and not own_device and both_batteries_known:
                award = credit_battery_contribution(
                    cur, account_id=user.account_id, vehicle_identifier=vehicle_identifier,
                    distance_m=result.distance_meters, start_lat=start_lat, start_lng=start_lon,
                    ride_id=str(rid),
                )
                if award is not None:
                    points_awarded.append({"action": award["action"], "points": award["points"]})

            if may_award and nav_improvement_on:
                # ride_routes doesn't exist until PLAN_RIDE_MODE_API.md phase
                # A3 (sql/052) — A2 may deploy first. Guard on a safe
                # existence probe (to_regclass returns NULL rather than
                # raising against a database that hasn't applied sql/052 yet,
                # same idiom src/cli.py:deidentify_donations uses) and skip
                # the nav award gracefully until that table exists; A3's own
                # nav_route_feedback/nav_qualitative_feedback awards are
                # credited from POST .../survey, not here.
                cur.execute("SELECT to_regclass('ride_routes')")
                (ride_routes_relid,) = cur.fetchone()
                has_route_row = False
                if ride_routes_relid is not None:
                    cur.execute(
                        "SELECT 1 FROM ride_routes WHERE tracked_ride_id = %s LIMIT 1",
                        (str(rid),),
                    )
                    has_route_row = cur.fetchone() is not None
                if has_route_row:
                    award = credit_nav_distance_bonus(
                        cur, account_id=user.account_id, vehicle_identifier=vehicle_identifier,
                        distance_m=result.distance_meters, start_lat=start_lat, start_lng=start_lon,
                        ride_id=str(rid),
                    )
                    if award is not None:
                        points_awarded.append({"action": award["action"], "points": award["points"]})

            points_awarded_total = sum(p["points"] for p in points_awarded)
            # `points_settled_at` starts the de-id clock (src/cli.py's
            # deidentify_donations reads it directly) and must be stamped
            # whenever this settles NOW -- reusing `validated_at` (already
            # `now` for every non-"pending_feed" verdict, `None` for
            # "pending_feed") is the SAME immediate-vs-deferred rule
            # `finalize_validation` (src/ride_watch.py) applies when IT
            # later settles a pending_feed donation, so both settlement
            # paths agree on when the clock starts. Before this, only the
            # deferred path ever stamped it -- an immediately eligible OR
            # ineligible donation never started de-identification at all.
            cur.execute(
                "UPDATE track_donations SET points_awarded = %s, points_settled_at = %s WHERE id = %s",
                (points_awarded_total, validated_at, str(donation_id)),
            )

            # Battery ingestion: only when GBFS had ALREADY resolved at
            # donation time ("eligible" implies gbfs_end already matched) —
            # a "pending_feed" donation's battery signal is ingested later
            # by finalize_validation, the sole ingestion path for that case.
            # Unconditional on ride_options: the derived observation helps
            # the model regardless of whether this rider is credited for it.
            if result.verdict == "eligible":
                battery_ride_row = {
                    "vehicle_identifier": vehicle_identifier,
                    "track_key_issued_at": track_key_issued_at,
                    "user_reported_ended_at": user_reported_ended_at,
                    "feed_start_battery_percent": feed_start_battery_percent,
                    "reported_start_battery_percent": reported_start_battery_percent,
                    "reported_battery_percent": reported_battery_percent,
                }
                battery_donation_row = {
                    "id": donation_id, "vehicle_model": vehicle_model,
                    "distance_meters": result.distance_meters,
                }
                ingest_donated_observation(
                    cur, ride_row=battery_ride_row, donation_row=battery_donation_row)

        conn.commit()

    response = result.as_response()
    response["donation_id"] = str(donation_id)
    response["distance_meters"] = round(result.distance_meters, 1)
    response["waypoint_count"] = result.waypoint_count
    response["points"] = points_awarded
    return response


@router.post("/api/v1/tracked-rides/{ride_id}/waypoints")
def add_waypoint(
    ride_id: str,
    user: SessionUser = Depends(require_session),
    payload: WaypointIn = Body(...),
) -> dict[str, Any]:
    rid = _parse_ride_id(ride_id)
    if payload.waypoint_at.tzinfo is None:
        raise HTTPException(400, "waypoint_at must include a UTC offset")

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="tracked_ride_waypoint_account", key=str(user.account_id),
                    limit=_LIMIT_WAYPOINT_PER_ACCOUNT[0],
                    window_seconds=_LIMIT_WAYPOINT_PER_ACCOUNT[1])

            cur.execute(
                "SELECT user_reported_ended_at, gbfs_reappeared_at, watch_expires_at, "
                "start_lat, start_lon "
                "FROM tracked_rides WHERE id = %s AND account_id = %s",
                (str(rid), user.account_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "no such ride")
            ended, reappeared, expires_at, start_lat, start_lon = row
            if not (ended is None and reappeared is None and expires_at > datetime.now(timezone.utc)):
                raise HTTPException(409, {"error": "ride_not_active",
                                          "detail": "cannot add waypoints to a ride that isn't active"})

            points, idx = _prospective_path(
                cur, rid, start_lat, start_lon,
                payload.waypoint_at, payload.lat, payload.lon,
            )
            _check_appendable(points, idx)

            cur.execute(
                """
                INSERT INTO ride_waypoints (tracked_ride_id, account_id, waypoint_at, lat, lon, metadata)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id, created_at
                """,
                (str(rid), user.account_id, payload.waypoint_at, payload.lat, payload.lon,
                 json.dumps(payload.metadata or {})),
            )
            new_id, created_at = cur.fetchone()

            # Rebuild path_polyline from the full ordered set — waypoints
            # can arrive out of order (client retry/offline buffering), so
            # an incremental append would silently corrupt the path. The
            # ride's start point leads it (see _measured_path); the ride is
            # still active, so there is no reported end to close it with yet
            # — PATCH .../end recomputes over these same points plus its own
            # end coordinates. Off-feed rides do exactly the same
            # (api_rides.py:_rebuild_track) — badges sum distance across
            # both tables, so the two must measure the same way.
            #
            # `points` from _prospective_path IS that full ordered set: it
            # is the existing track with this fix inserted at the position
            # the same ORDER BY (waypoint_at, id) puts it in, which is what
            # the row we just INSERTed now occupies. Re-reading the track to
            # rebuild the identical list walked it a second time on every
            # append — doubling an already-quadratic cost over a 600-fix
            # ride, for a value that cannot differ inside one transaction.
            rebuilt = points
            # Distance is recomputed from the same full ordered set, for the
            # same reason: an incremental += would be wrong the moment a
            # waypoint arrives out of order. Measured under the operator's
            # leg cap and clamped to the ride cap — neither should bind,
            # because _check_appendable just refused anything that would
            # breach them, but a ride that predates those checks must still
            # come out of here satisfying the invariant.
            measured, excluded = measure_path(rebuilt, cap_legs=True)
            recorded, clamped_from = clamp_distance(measured)
            cur.execute(
                "UPDATE tracked_rides SET path_polyline = %s, distance_meters = %s, "
                "distance_source = %s, distance_clamped_from_m = %s, "
                "updated_at = NOW() WHERE id = %s",
                (encode_polyline(rebuilt), recorded,
                 partial_source("waypoints", partial=excluded > 0),
                 clamped_from, str(rid)),
            )
        conn.commit()
    return {
        "id": int(new_id), "ride_id": str(rid),
        "waypoint_at": payload.waypoint_at.isoformat(),
        "lat": payload.lat, "lon": payload.lon,
        "metadata": payload.metadata or {},
        "created_at": created_at.isoformat(),
    }


@router.get("/api/v1/tracked-rides/{ride_id}/waypoints")
def list_waypoints(
    ride_id: str,
    user: SessionUser = Depends(require_session),
    limit: int = Query(500, ge=1, le=5000),
    after: str | None = Query(None, description="ISO timestamp — the NEXT page: waypoints recorded after this"),
    before: str | None = Query(None, description="ISO timestamp — the PREVIOUS page: the last `limit` waypoints recorded before this"),
) -> dict[str, Any]:
    """Waypoints oldest-first.

    Pagination pairs the cursor with the sort direction, which it did not
    used to: `before` with an ascending sort re-served the OLDEST rows on
    every call, so page 2 was the start of page 1 and nothing past the first
    page was reachable at all. Page forward with `after` (the last
    waypoint_at you received); `before` walks backwards by taking the last
    `limit` rows older than the cursor and returning them oldest-first.
    Same contract as GET /api/v1/rides/{id}/waypoints.
    """
    rid = _parse_ride_id(ride_id)
    parsed_after = _parse_before(after, "after")
    parsed_before = _parse_before(before)

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM tracked_rides WHERE id = %s AND account_id = %s",
                (str(rid), user.account_id),
            )
            if cur.fetchone() is None:
                raise HTTPException(404, "no such ride")

            where = ["tracked_ride_id = %s"]
            params: list[Any] = [str(rid)]
            if parsed_after is not None:
                where.append("waypoint_at > %s")
                params.append(parsed_after)
            if parsed_before is not None:
                where.append("waypoint_at < %s")
                params.append(parsed_before)
            # Walking backwards means "the newest rows older than the
            # cursor", so the LIMIT has to bite from the far end.
            backwards = parsed_before is not None
            order = "DESC" if backwards else "ASC"
            params.append(limit)
            cur.execute(
                f"""
                SELECT id, waypoint_at, lat, lon, metadata, created_at
                FROM ride_waypoints
                WHERE {' AND '.join(where)}
                ORDER BY waypoint_at {order}, id {order}
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
    if backwards:
        rows = list(reversed(rows))
    waypoints = [
        {"id": int(r[0]), "waypoint_at": r[1].isoformat(), "lat": r[2], "lon": r[3],
         "metadata": r[4] if isinstance(r[4], dict) else {}, "created_at": r[5].isoformat()}
        for r in rows
    ]
    return {"count": len(waypoints), "waypoints": waypoints}


@router.delete("/api/v1/tracked-rides/{ride_id}")
def delete_tracked_ride(
    ride_id: str,
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    """Hard delete, cascades to user_device_watch_list + ride_waypoints.
    404 for both 'not yours' and 'doesn't exist' — no existence oracle
    across accounts."""
    rid = _parse_ride_id(ride_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM tracked_rides WHERE id = %s AND account_id = %s",
                (str(rid), user.account_id),
            )
            deleted = cur.rowcount
        conn.commit()
    if not deleted:
        raise HTTPException(404, "no such ride")
    return {"deleted": True}


@router.delete("/api/v1/tracked-rides")
def delete_all_tracked_rides(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    """Hard delete every tracked ride the account owns. Immediate and final."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tracked_rides WHERE account_id = %s", (user.account_id,))
            deleted = cur.rowcount
        conn.commit()
    return {"deleted_count": int(deleted)}
