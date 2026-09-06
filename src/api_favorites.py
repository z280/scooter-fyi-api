"""My Scooters — the specific vehicles a rider has kept
(sql/081_favorite_devices.sql, ALONG_THE_WAY_PLAN.md §8).

    GET    /api/v1/profile/favorite-devices          the list, with live state
    POST   /api/v1/profile/favorite-devices          keep one — needs a fresh scan
    PATCH  /api/v1/profile/favorite-devices/{vid}    nickname, notify_on_available
    DELETE /api/v1/profile/favorite-devices/{vid}

TWO RULES CARRY THIS MODULE, and they are different mechanisms solving
different problems. Confusing them is the mistake most likely to be made by
somebody changing this file later, so they are stated apart:

  THE GATE (anti-abuse). Keeping a vehicle needs a QR payload that validates
  for it AND a fix within FAVORITE_PROXIMITY_METERS of where the fleet last
  saw it. The scan alone is not enough: `src/qr.py:validate_scan` proves the
  caller HAS THE PLATE — it hashes the payload's plate and compares — and
  nothing in src/api_qr.py or credit_qr_scan_points has ever compared the
  submitted lat/lng to anything. Fine for a points bonus. Not fine for a
  feature whose premise is "you were there".

  THE WITHHOLDING (privacy). A favourite's POSITION IS NOT RETURNED WHILE THE
  VEHICLE IS IN A RENTAL. Veo keeps rented vehicles in the feed broadcasting a
  live moving position (src/ride_watch.py measured it: 320 m between
  consecutive samples for reserved vehicles against 1.2 m for the rest), and
  /api/v1/devices/current publishes that publicly today. What a favourite
  would add is a one-tap, persistent, targeted subscription to ONE vehicle
  somebody physically located — scan the sticker on the scooter outside a
  person's house, keep it, watch where it goes. So: parked position yes,
  moving position no. Enforced here rather than in the client, because a rule
  a client can route around is not a rule.

The gate does nothing about the second problem — somebody can legitimately
scan the scooter outside your house — which is exactly why the withholding is
not built on top of it.

WHAT IS NOT STORED. Not the rider's fix. The proximity check runs at write
time and the coordinates are dropped. Nothing reads them, and every stored
position is a retention rule that has to be written into three files
(src/cli.py, src/api_meta.py:_PRIVACY, the privacy policy template).

POINTS. Keeping a vehicle runs the same `credit_qr_scan_points` path the
scan endpoint does. It is already once-per-(account, vehicle) and advisory-
locked, so a first-ever scan pays 100 and every later one pays nothing —
there is no way to double-pay by favouriting something you already scanned.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from .accounts import SessionUser, require_session
from .geo import distance_meters
from .identity import hash_plate
from .pg import connection
from .points import credit_qr_scan_points
from .qr import QrValidationError, extract_plate, validate_scan
from .quality import compute_battery_percent
from .ratelimit import enforce

log = logging.getLogger(__name__)

router = APIRouter()

_VEHICLE_IDENTIFIER_RE = r"^[0-9a-f]{16}$"

#: How many vehicles one rider may keep.
#:
#: Ten, and the number is a product rule rather than a technical one: a rider
#: with fifty kept scooters is not keeping favourites, they are running a
#: tracker. Ten is more than anyone needs and few enough to stay a list rather
#: than a search. Capped in code, not in the schema, per sql/043's header.
MAX_FAVORITE_DEVICES = 10

#: How close the rider has to be to keep a vehicle.
#:
#: The radius the "Unlock in Veo" gate already uses for "physically at the
#: scooter", and generous for a reason that has nothing to do with generosity:
#: a GBFS position is up to two minutes stale and consumer GPS in a street
#: canyon is routinely 20-30 m out. The two errors do not cancel. A tighter
#: radius rejects honest riders standing with a hand on the handlebar, which
#: is the failure that makes a feature feel broken.
FAVORITE_PROXIMITY_METERS = 75.0

#: How long a vehicle can be absent from the feed before its row reads "gone"
#: rather than "we just haven't seen it this cycle". Twelve hours rather than
#: a couple of cycles: `device-watch.ts` needs a fast answer because a rider is
#: WALKING; this list is read days later, and calling a scooter gone because
#: one poll missed it would have every rider's list flickering.
GONE_AFTER_HOURS = 12

_LIMIT_FAVORITE_WRITES = (20, 3600)


def _now() -> datetime:
    """The clock, behind one indirection so a test can freeze it.

    `GONE_AFTER_HOURS` is the only rule here that compares a stored timestamp
    to the present, and a test that fixes its fixture timestamps while the
    handler reads the real clock is a test that passes on the day it was
    written and fails later — which is exactly what happened to the first
    version of tests/test_favorite_devices.py when this container's clock
    moved three days forward mid-session. One indirection makes "an hour ago"
    mean an hour ago, whatever day it is.
    """
    return datetime.now(timezone.utc)


class FavoriteIn(BaseModel):
    """The scan itself, not a claim to have done one.

    Identical in shape to `QrScanIn` deliberately: it reuses the same
    validator, and a client-asserted "I already verified this" boolean would
    be a gate living on the wrong side of the network.
    """

    #: OPTIONAL, because the scan is the identity.
    #:
    #: `src/api_device_features.py` established this: the tools-drawer flow
    #: scans a scooter the rider never tapped on the map, and the QR is the
    #: only identity it has. My Scooters has exactly the same flow — "keep
    #: one" from the panel, camera straight up — and requiring the client to
    #: already know a salted hash it cannot compute would have forced a
    #: plate-extraction copy into the browser to work around it.
    #:
    #: When it IS sent, it must agree with the sticker. Unlike a features
    #: report — where a mismatch re-targets, because the answers describe the
    #: scooter the rider was standing at and there is data worth saving — a
    #: disagreement here is refused. "Keep this one" naming one scooter while
    #: the sticker names another is a client bug, and quietly keeping the
    #: other one would be the app deciding which scooter somebody meant.
    vehicle_identifier: str | None = Field(None, min_length=16, max_length=16,
                                           pattern=_VEHICLE_IDENTIFIER_RE)
    qr_raw_value: str = Field(..., min_length=1, max_length=2000)
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    nickname: str | None = Field(None, min_length=1, max_length=40)


class FavoritePatch(BaseModel):
    """Both fields optional; absent means "leave it".

    No re-scan needed to rename or to change the notification setting: the
    gate is about establishing that this vehicle is yours to keep, and it has
    already been passed. Requiring a rider to stand at the scooter to turn
    off a notification would be a rule with no purpose.
    """

    nickname: str | None = Field(None, max_length=40)
    notify_on_available: bool | None = None


def _latest_cycle(cur) -> Any:
    cur.execute(
        """
        SELECT cycle_id
        FROM observation_cycles oc
        JOIN snapshot_metadata_core USING (cycle_id)
        WHERE oc.job_status = 'complete'
        ORDER BY snapshot_time DESC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    return row[0] if row else None


def _rows_for(cur, account_id: int) -> list[tuple]:
    """One row per favourite, with whatever the latest cycle says about it.

    LEFT JOINs throughout: a vehicle that has left the fleet still has a row
    here and still has to render, which is the whole reason the table does not
    carry a foreign key to device_state.
    """
    cycle_id = _latest_cycle(cur)
    cur.execute(
        """
        SELECT f.vehicle_identifier, f.nickname, f.verified_at,
               f.notify_on_available, f.created_at, f.last_seen_at,
               r.latitude, r.longitude, r.is_disabled, r.is_reserved,
               r.current_range_meters, r.vehicle_model_name, r.vehicle_use_type,
               ds.last_observed_at, ds.rental_started_at
          FROM favorite_devices f
          LEFT JOIN device_state ds
                 ON ds.vehicle_identifier = f.vehicle_identifier
          LEFT JOIN raw_telemetry_points r
                 ON r.vehicle_identifier = f.vehicle_identifier
                AND r.cycle_id = %s
         WHERE f.account_id = %s
         ORDER BY f.created_at DESC
        """,
        (cycle_id, account_id),
    )
    return cur.fetchall()


def _state(
    *,
    in_feed: bool,
    is_disabled: Any,
    is_reserved: Any,
    rental_started_at: Any,
    last_observed_at: Any,
    now: datetime,
) -> str:
    """available | unavailable | in_use | gone.

    ORDER MATTERS, and it is not the same order the map uses. `in_use` is
    tested BEFORE `unavailable` here: both hide the position, but only one of
    them is a person riding, and telling a rider "out of service" about a
    scooter somebody is on the back of is simply wrong. (The map's own
    `hideUnavailable` collapses the two because for its purpose — can I go and
    get this — they are the same answer.)

    `rental_started_at` is honoured alongside `is_reserved` because
    src/device_state.py freezes a vehicle's position for the duration of a
    rental: a stale feed row could report a vehicle un-reserved while
    device_state still knows it is out. Either signal withholds.
    """
    reserved = bool(is_reserved) or rental_started_at is not None
    if reserved:
        return "in_use"
    if not in_feed:
        if last_observed_at is None:
            return "gone"
        age_hours = (now - last_observed_at).total_seconds() / 3600.0
        return "gone" if age_hours >= GONE_AFTER_HOURS else "unavailable"
    if is_disabled:
        return "unavailable"
    return "available"


def _favorite_payload(row: tuple, now: datetime) -> dict[str, Any]:
    (vehicle_identifier, nickname, verified_at, notify, created_at,
     last_seen_at, lat, lon, is_disabled, is_reserved, range_meters,
     model_name, use_type, last_observed_at, rental_started_at) = row

    state = _state(
        in_feed=lat is not None and lon is not None,
        is_disabled=is_disabled,
        is_reserved=is_reserved,
        rental_started_at=rental_started_at,
        last_observed_at=last_observed_at,
        now=now,
    )

    # THE WITHHOLDING. Position and charge are both ride-progress signals: a
    # battery falling five points every ten minutes is a track, just a coarse
    # one. `gone` withholds too, for a different reason — the last position we
    # have is days old and publishing it as "where it is" would be a claim we
    # cannot make.
    withheld = state in ("in_use", "gone")

    out: dict[str, Any] = {
        "vehicle_identifier": vehicle_identifier,
        "nickname": nickname,
        "state": state,
        "position_withheld": withheld,
        "notify_on_available": bool(notify),
        "verified_at": verified_at.isoformat() if verified_at else None,
        "created_at": created_at.isoformat() if created_at else None,
        "last_seen_at": (last_observed_at or last_seen_at).isoformat()
        if (last_observed_at or last_seen_at) else None,
        "vehicle_model_name": model_name,
        "vehicle_use_type": use_type,
    }
    if not withheld:
        out["lat"] = float(lat) if lat is not None else None
        out["lon"] = float(lon) if lon is not None else None
        out["battery_percent"] = compute_battery_percent(range_meters)
        out["current_range_meters"] = range_meters
    return out


@router.get("/api/v1/profile/favorite-devices")
def list_favorites(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    """Every vehicle this rider keeps, newest first, with its live state.

    `position_withheld` is an explicit field rather than a silent omission:
    an absent `lat` that a client has to interpret is one somebody eventually
    "fixes" by falling back to a cached value, which is exactly the behaviour
    the rule exists to prevent. A named flag has to be argued with.
    """
    now = _now()
    with connection() as conn:
        with conn.cursor() as cur:
            rows = _rows_for(cur, user.account_id)
    return {
        "favorite_devices": [_favorite_payload(r, now) for r in rows],
        "max_favorites": MAX_FAVORITE_DEVICES,
    }


@router.post("/api/v1/profile/favorite-devices", status_code=201)
def add_favorite(
    payload: FavoriteIn = Body(...),
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    """Keep a vehicle. Requires a QR payload that validates for it AND a fix
    within FAVORITE_PROXIMITY_METERS of where the fleet last saw it.

    Re-keeping one you already have is a 201, not a 409: it refreshes
    `verified_at` and returns the row. A rider standing at their own scooter
    pressing the button again has not made a mistake.
    """
    # Resolve the vehicle from the sticker. `extract_plate`'s whole-payload
    # fallback means this is None only for an empty payload, which the model
    # already refuses.
    plate = extract_plate(payload.qr_raw_value)
    if not plate:
        raise HTTPException(
            400, {"error": "qr_mismatch",
                  "detail": "could not read a plate from this QR code"},
        )
    scanned = hash_plate(plate)
    if payload.vehicle_identifier is not None:
        try:
            validate_scan(payload.qr_raw_value, payload.vehicle_identifier)
        except QrValidationError as e:
            raise HTTPException(400, {"error": "qr_mismatch", "detail": str(e)})
    vehicle_identifier = payload.vehicle_identifier or scanned

    now = _now()
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="favorite_write_account",
                    key=str(user.account_id),
                    limit=_LIMIT_FAVORITE_WRITES[0],
                    window_seconds=_LIMIT_FAVORITE_WRITES[1])

            cur.execute(
                "SELECT current_lat, current_lon, last_observed_at "
                "FROM device_state WHERE vehicle_identifier = %s",
                (vehicle_identifier,),
            )
            device = cur.fetchone()
            if device is None:
                raise HTTPException(
                    400, {"error": "unknown_device",
                          "detail": "no vehicle with that identifier"},
                )
            device_lat, device_lon, last_observed_at = device

            # THE PROXIMITY HALF OF THE GATE. A vehicle we have no position
            # for cannot be checked against, and the safe answer is to refuse:
            # letting it through would make "we don't know where it is" the
            # one reliable way past the gate.
            if device_lat is None or device_lon is None:
                raise HTTPException(
                    403,
                    {"error": "too_far_from_device",
                     "detail": "we have no recent position for this vehicle, "
                               "so we can't tell you're standing at it",
                     "meters_away": None,
                     "meters_allowed": FAVORITE_PROXIMITY_METERS},
                )
            metres = distance_meters(
                payload.lat, payload.lng, float(device_lat), float(device_lon)
            )
            if metres > FAVORITE_PROXIMITY_METERS:
                raise HTTPException(
                    403,
                    {"error": "too_far_from_device",
                     "detail": "you'll need to be standing at this one",
                     "meters_away": round(metres),
                     "meters_allowed": FAVORITE_PROXIMITY_METERS},
                )

            # The cap binds the INSERT path only, and is checked against the
            # rows that are not this vehicle — so a rider at the cap can still
            # re-verify one they already keep. Same rule, and the same reason,
            # as api_preferences._enforce_named_cap.
            cur.execute("SELECT 1 FROM accounts WHERE id = %s FOR UPDATE",
                        (user.account_id,))
            if cur.fetchone() is None:
                raise HTTPException(401, "account no longer exists")
            cur.execute(
                "SELECT COUNT(*) FROM favorite_devices "
                "WHERE account_id = %s AND vehicle_identifier <> %s",
                (user.account_id, vehicle_identifier),
            )
            (others,) = cur.fetchone()
            if others >= MAX_FAVORITE_DEVICES:
                raise HTTPException(
                    409,
                    {"error": "favorite_limit_reached",
                     "detail": f"you already keep {MAX_FAVORITE_DEVICES} "
                               f"scooters — let one go before keeping another",
                     "max_favorites": MAX_FAVORITE_DEVICES},
                )

            cur.execute(
                """
                INSERT INTO favorite_devices
                    (account_id, vehicle_identifier, nickname, verified_at,
                     last_seen_at)
                VALUES (%s, %s, %s, NOW(), %s)
                ON CONFLICT (account_id, vehicle_identifier) DO UPDATE SET
                    verified_at = NOW(),
                    last_seen_at = EXCLUDED.last_seen_at,
                    -- A re-scan that carries no nickname must not wipe the
                    -- one the rider typed the first time.
                    nickname = COALESCE(EXCLUDED.nickname,
                                        favorite_devices.nickname)
                RETURNING (xmax = 0) AS inserted
                """,
                (user.account_id, vehicle_identifier,
                 payload.nickname, last_observed_at),
            )
            (inserted,) = cur.fetchone()

            # Same path the scan endpoint uses: once per (account, vehicle),
            # advisory-locked, so there is no way to double-pay by favouriting
            # something already scanned.
            awarded = credit_qr_scan_points(
                cur, account_id=user.account_id,
                vehicle_identifier=vehicle_identifier,
                lat=payload.lat, lng=payload.lng,
            )

            rows = _rows_for(cur, user.account_id)
        conn.commit()

    mine = next(
        (r for r in rows if r[0] == vehicle_identifier), None
    )
    return {
        "favorite": _favorite_payload(mine, now) if mine else None,
        "already_favorited": not inserted,
        "points_awarded": awarded["points"] if awarded else 0,
    }


@router.patch("/api/v1/profile/favorite-devices/{vehicle_identifier}")
def patch_favorite(
    vehicle_identifier: str = Path(..., pattern=_VEHICLE_IDENTIFIER_RE),
    payload: FavoritePatch = Body(...),
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    """Rename, or turn the availability alert on and off.

    An empty-string nickname clears it; an absent one leaves it. The
    distinction matters because "" and None mean different things to a rider
    who has just deleted the text in a box.
    """
    now = _now()
    sets: list[str] = []
    params: list[Any] = []
    if payload.nickname is not None:
        sets.append("nickname = %s")
        params.append(payload.nickname.strip() or None)
    if payload.notify_on_available is not None:
        sets.append("notify_on_available = %s")
        params.append(payload.notify_on_available)
    if not sets:
        raise HTTPException(400, {"error": "nothing_to_change"})

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE favorite_devices SET {', '.join(sets)} "
                "WHERE account_id = %s AND vehicle_identifier = %s "
                "RETURNING id",
                (*params, user.account_id, vehicle_identifier),
            )
            if cur.fetchone() is None:
                raise HTTPException(404, {"error": "not_favorited"})
            rows = _rows_for(cur, user.account_id)
        conn.commit()
    mine = next((r for r in rows if r[0] == vehicle_identifier), None)
    return {"favorite": _favorite_payload(mine, now) if mine else None}


@router.delete("/api/v1/profile/favorite-devices/{vehicle_identifier}")
def delete_favorite(
    vehicle_identifier: str = Path(..., pattern=_VEHICLE_IDENTIFIER_RE),
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    """Let one go. 404 when it was not yours to begin with — which is also
    the answer for somebody else's favourite, deliberately: this endpoint
    never distinguishes "you don't keep that" from "nobody does"."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM favorite_devices "
                "WHERE account_id = %s AND vehicle_identifier = %s",
                (user.account_id, vehicle_identifier),
            )
            deleted = cur.rowcount
        conn.commit()
    if not deleted:
        raise HTTPException(404, {"error": "not_favorited"})
    return {"deleted": True, "vehicle_identifier": vehicle_identifier}
