"""Plate lookups served from our own snapshot, so browsers never call Veo.

WHY THIS EXISTS (owner decision 2026-10-08). The web app used to recover each
vehicle's plate by fetching Veo's public GBFS `free_bike_status` feed straight
from the rider's browser and reading `&number=<plate>` out of `rental_uris`
(the client's gbfs.ts). That join was privacy-neutral with respect to the
plate — Veo publishes it — but not with respect to the RIDER: every map view
handed Veo the scooter.fyi user's IP. The ingest already reads that same feed
server-side and keeps the raw plate (raw_telemetry_points.vehicle_plate), so
the two directions the client needs are answered here instead:

  FORWARD  GET /api/v1/vehicles/plates?device_ids=a,b,c   (signed-in rider)
           device_id -> plate, for the ids on screen. Same data Veo hands any
           anonymous caller; gated on a session anyway so the full plate list
           is not one unauthenticated scrape away through US (src/identity.py:
           the raw plate never crosses an unauthenticated wire).

  REVERSE  GET /api/v1/vehicles/resolve?plate=1025543      (public)
           plate -> {device_id, vehicle_identifier}. Serves the `?ride=plate:`
           deep link and QR flows, which run before sign-in. Returns ONLY
           identifiers the public devices payload already carries; never the
           plate. Rate-limited per IP so it cannot be walked to build a
           plate -> HMAC-identifier table.

  IDENTIFY GET /api/v1/vehicles/resolve?qr=<raw payload>&explain=true (public)
           The same lookup, extended for docs/FLEET_REPORTS_PLAN.md §2.7: a
           rider standing in front of a scooter the map does not show can ask
           WHY. `qr=` takes the sticker's raw payload (src/qr.py's
           extract_plate reads the plate out of it), and `explain=true` adds
           the reason — on_map / missing / gone — plus any uncleared
           negative report and the risk it sets (no report hides a scooter:
           owner, 2026-10-09), reading
           device_state for vehicles that have left the feed. Same rule as
           the plain lookup, by the owner's decision of 2026-10-08: public,
           30/min per IP, never echoes the plate, 404 for none or ambiguous.
           One plate oracle with one policy, not two.

Both resolve against the CURRENT snapshot — the newest complete cycle, via
api_public.latest_complete_cycle, the same one /api/v1/devices/current serves —
because `device_id` is Veo's bike_id and may rotate per trip: an id from an
older cycle can belong to a different vehicle now. Ids/plates not in that
snapshot are simply absent (forward) or 404 (reverse).

No new storage: the plate is read from raw_telemetry_points, which already
holds it under its existing 48-hour retention. The only write is the
rate-limit event (bucket, key, time), as for every other rate-limited route.

NEVER LOG PLATES. Nothing in this module logs request parameters or results;
the rate limiter logs only the bucket and the account id / IP. The one place a
plate WOULD otherwise be written down is uvicorn's access log, which records
the full request line — `GET /api/v1/vehicles/resolve?plate=1025543` — so
src/main.py attaches src/log_redaction.py's filter to the `uvicorn.access`
logger (RedactPlateQuery below is that filter's historical name).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from . import fleet_reports, vehicle_identity
from .accounts import SessionUser, require_session
from .api_public import latest_complete_cycle
from .client_ip import real_client_ip
from .identity import hash_plate
from .qr import extract_plate
from .pg import connection
from .log_redaction import RedactSensitiveQuery
from .ratelimit import enforce

router = APIRouter()

#: Most device ids one forward request may ask about. The client asks for the
#: handful of vehicles a rider is looking at (a cluster, a device card), not
#: the fleet; 50 covers a dense cluster with room to spare.
MAX_DEVICE_IDS = 50
#: raw_telemetry_points.device_id is VARCHAR(64).
_MAX_DEVICE_ID_LEN = 64
#: Generous for a plate (Veo's are 7 digits) plus hand-typed separators.
_MAX_PLATE_LEN = 32
#: A sticker's raw payload is a deep-link URL; sql/032 stores them, and none
#: is anywhere near this. Bounded so the endpoint never parses a novel.
_MAX_QR_LEN = 1024

# (limit, window_seconds).
# Forward: the client batches on-screen ids, so a rider panning the map makes a
# few requests a minute; 60/min per account is ample. The per-IP cap is higher
# because several riders can share one address (household, campus, carrier
# NAT) — it exists to stop one IP cycling through throwaway accounts.
LIMIT_PLATES_PER_ACCOUNT = (60, 60)
LIMIT_PLATES_PER_IP = (120, 60)
# Reverse: a person resolves one plate per deep link / QR scan. 30/min per IP
# is far beyond any human use and caps enumeration of the ~8,000-plate space
# at 43,200 guesses/day per address.
LIMIT_RESOLVE_PER_IP = (30, 60)

# Forward: per-user, plate-bearing. Never stored by any cache.
_PLATES_CACHE_HEADER = "private, no-store"
# Reverse: no-store too, for two reasons. (1) The answer is only true for one
# cycle — a cached device_id can point at a vehicle that has since been
# re-keyed. (2) The per-IP rate limit counts ORIGIN hits; an edge cache would
# answer repeat lookups without them being counted.
_RESOLVE_CACHE_HEADER = "no-store"

_PLATE_SEPARATORS = re.compile(r"[\s-]+")

# The same normalisation in SQL, applied to the stored raw plate so both sides
# are compared in one form. `[[:space:]-]` is POSIX for the class above; the
# whitespace strip makes the btrim/trim step implicit.
_SQL_NORMALIZED_PLATE = (
    "upper(regexp_replace(r.vehicle_plate, '[[:space:]-]+', '', 'g'))"
)
#: The same, over device_state's copy of the plate (sql/004).
_SQL_NORMALIZED_STATE_PLATE = (
    "upper(regexp_replace(ds.vehicle_plate, '[[:space:]-]+', '', 'g'))"
)

#: `last_seen` on a vehicle that has left the feed is rounded to 3 decimals
#: (~100 m), the public CSV's rule. "Where we last saw it" needs a
#: neighbourhood, not a doorstep — and the vehicles most likely to have left
#: the feed include the ones reported inaccessible, whose exact point is
#: somebody's yard (plan §6).
_LAST_SEEN_DECIMALS = 3


_PLATE_QUERY = re.compile(r"([?&]plate=)[^&#]*")


def redact_plate_query(path: str) -> str:
    """`/api/v1/vehicles/resolve?plate=1025543` -> `…?plate=[redacted]`."""
    return _PLATE_QUERY.sub(r"\1[redacted]", path)


#: The access-log filter src/main.py installs. Generalised into
#: src/log_redaction.py (it now also covers /api/v1/geocode/reverse's
#: coordinates and the httpx logger); the name is kept for existing callers.
RedactPlateQuery = RedactSensitiveQuery


def normalize_plate(raw: str | None) -> str:
    """Plate comparison form — identical to the frontend's `normalizePlate`
    (denver-scooter-fyi src/ride-deeplink.ts): trimmed, uppercased, inner
    whitespace and hyphens dropped. `" 10-25 543 "` -> `"1025543"`."""
    return _PLATE_SEPARATORS.sub("", (raw or "").strip().upper())


def parse_device_ids(raw: str) -> list[str]:
    """Split a comma list into unique, non-empty ids (first-seen order).

    400 when nothing is left, when there are more than MAX_DEVICE_IDS, or when
    an id is longer than any id the feed can contain."""
    ids: list[str] = []
    seen: set[str] = set()
    for part in (raw or "").split(","):
        d = part.strip()
        if d and d not in seen:
            seen.add(d)
            ids.append(d)
    if not ids:
        raise HTTPException(400, detail="device_ids must list at least one device id")
    if len(ids) > MAX_DEVICE_IDS:
        raise HTTPException(
            400, detail=f"device_ids accepts at most {MAX_DEVICE_IDS} ids per request",
        )
    if any(len(d) > _MAX_DEVICE_ID_LEN for d in ids):
        raise HTTPException(
            400, detail=f"device ids are at most {_MAX_DEVICE_ID_LEN} characters",
        )
    return ids


@router.get("/api/v1/vehicles/plates")
def vehicle_plates(
    request: Request,
    response: Response,
    device_ids: str = Query(..., description="Comma-separated device ids (max 50)"),
    user: SessionUser = Depends(require_session),
) -> dict[str, Any]:
    """device_id -> plate for vehicles in the current snapshot.

    Requires a rider session (`Authorization: Bearer <token>`; 401 otherwise).
    Ids not in the current snapshot, or with no plate, are omitted.
    """
    ids = parse_device_ids(device_ids)
    ip = real_client_ip(request) or "?"

    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="vehicle_plates_account", key=str(user.account_id),
                    limit=LIMIT_PLATES_PER_ACCOUNT[0],
                    window_seconds=LIMIT_PLATES_PER_ACCOUNT[1])
            enforce(cur, bucket="vehicle_plates_ip", key=ip,
                    limit=LIMIT_PLATES_PER_IP[0],
                    window_seconds=LIMIT_PLATES_PER_IP[1])
            cycle_id, snapshot_time = latest_complete_cycle(cur)
            cur.execute(
                """
                SELECT r.device_id, r.vehicle_plate
                FROM raw_telemetry_points r
                WHERE r.cycle_id = %s
                  AND r.device_id = ANY(%s)
                  AND r.vehicle_plate IS NOT NULL
                  AND r.vehicle_plate <> ''
                ORDER BY r.device_id, r.id
                """,
                (cycle_id, ids),
            )
            rows = cur.fetchall()

    plates: dict[str, str] = {}
    for device_id, plate in rows:
        # A device_id appears once per cycle; if the feed ever repeated one,
        # the first row wins (deterministic via ORDER BY r.id).
        plates.setdefault(device_id, plate)

    response.headers["Cache-Control"] = _PLATES_CACHE_HEADER
    response.headers["Vary"] = "Authorization"
    return {
        "plates": plates,
        "as_of": snapshot_time.isoformat() if snapshot_time else None,
    }


@router.get("/api/v1/vehicles/resolve")
def resolve_plate(
    request: Request,
    response: Response,
    plate: str | None = Query(
        None, description="Plate as printed / scanned; separators ignored"),
    qr: str | None = Query(
        None, description="A sticker's raw QR payload, as the camera read it. "
                          "Alternative to `plate`; exactly one is required."),
    explain: bool = Query(
        False, description="Also say WHY the vehicle is or is not on the map "
                           "(docs/FLEET_REPORTS_PLAN.md §2.7), and answer for "
                           "vehicles that have left the feed."),
) -> dict[str, Any]:
    """plate (or QR payload) -> the vehicle carrying it.

    Public. Returns only identifiers the public devices payload already
    carries — never the plate. 404 when no vehicle carries it, or,
    defensively, when more than one does: missing beats wrong.

    Without `explain` this is exactly the original lookup: the current
    snapshot only, `{device_id, vehicle_identifier}`. With `explain=true`
    the answer adds `status` and its evidence, and a vehicle that has LEFT
    the feed (missing, or acknowledged gone) answers 200 instead of 404 —
    from device_state, with `device_id: null` because a stale bike_id may
    belong to another vehicle now.
    """
    if (plate is None) == (qr is None):
        # FastAPI's own answer for a missing required query parameter, kept so
        # a client that sent nothing sees the same status it always did.
        raise HTTPException(422, detail="exactly one of plate or qr is required")
    if qr is not None:
        if len(qr) > _MAX_QR_LEN:
            raise HTTPException(400, detail=f"qr is at most {_MAX_QR_LEN} characters")
        raw_plate = extract_plate(qr)
        if not raw_plate or len(raw_plate) > _MAX_PLATE_LEN:
            # Nothing a plate could be (a wifi QR, an unrelated URL). Refused
            # before the rate limit: it reveals nothing, so it costs nothing.
            raise HTTPException(400, detail={"error": "unreadable",
                                             "message": "no plate in this QR code"})
    else:
        raw_plate = plate
    if len(raw_plate) > _MAX_PLATE_LEN:
        raise HTTPException(400, detail=f"plate is at most {_MAX_PLATE_LEN} characters")
    want = normalize_plate(raw_plate)
    if not want:
        raise HTTPException(400, detail="plate must not be empty")
    ip = real_client_ip(request) or "?"

    # The 404 is raised AFTER the connection block: an exception inside it
    # rolls the transaction back, which would un-record the rate-limit event
    # — and misses are exactly the enumeration traffic the limit is for.
    explained: dict[str, Any] | None = None
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="vehicle_resolve_ip", key=ip,
                    limit=LIMIT_RESOLVE_PER_IP[0],
                    window_seconds=LIMIT_RESOLVE_PER_IP[1])
            cycle_id, snapshot_time = latest_complete_cycle(cur)
            cur.execute(
                f"""
                SELECT DISTINCT r.device_id, r.vehicle_identifier
                FROM raw_telemetry_points r
                WHERE r.cycle_id = %s
                  AND r.vehicle_plate IS NOT NULL
                  AND {_SQL_NORMALIZED_PLATE} = %s
                LIMIT 2
                """,
                (cycle_id, want),
            )
            rows = cur.fetchall()
            if explain and len(rows) == 1:
                explained = _explain_on_map(cur, cycle_id, snapshot_time, rows[0][1])
            elif explain and not rows:
                explained = _explain_off_feed(cur, cycle_id, snapshot_time,
                                              raw_plate, want)

    response.headers["Cache-Control"] = _RESOLVE_CACHE_HEADER
    if explain and explained is not None:
        if rows:
            explained["device_id"] = rows[0][0]
        return explained
    if len(rows) != 1:
        raise HTTPException(
            404,
            detail="no vehicle in the current snapshot carries that plate",
            headers={"Cache-Control": _RESOLVE_CACHE_HEADER},
        )
    device_id, vehicle_identifier = rows[0]
    return {"device_id": device_id, "vehicle_identifier": vehicle_identifier}


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _negative_fields(state: dict[str, Any] | None) -> dict[str, Any]:
    """Same meaning as /devices/current's negative_report_* fields."""
    if state is None:
        return {"negative_report_risk": None, "negative_report_reason": None,
                "negative_report_reason_detail": None, "negative_report_since": None}
    return {"negative_report_risk": state["risk"],
            "negative_report_reason": state["reason"],
            "negative_report_reason_detail": state["reason_detail"],
            "negative_report_since": _iso(state["since"])}


def _card(vid: str, *, model: str | None, form_factor: str | None) -> dict[str, Any]:
    """What a modal can show for a vehicle with no marker: the public label
    and what kind of vehicle it is. Nothing here is not already public."""
    return {
        "public_name": vehicle_identity.public_name(vid),
        "vehicle_model_name": model,
        "form_factor": form_factor,
    }


def _explain_on_map(cur, cycle_id: Any, snapshot_time: datetime,
                    vid: str) -> dict[str, Any]:
    """A vehicle in the current snapshot: on the map — always, whatever its
    reports say (owner, 2026-10-09: a report labels, it never hides)."""
    reports = fleet_reports.open_reports_for(cur, cycle_id, vid)
    state = fleet_reports.negative_state_for(cur, cycle_id, vid)
    cur.execute(
        """
        SELECT current_vehicle_model_name, current_form_factor
          FROM device_state WHERE vehicle_identifier = %s
        """,
        (vid,),
    )
    row = cur.fetchone() or (None, None)
    return {
        "device_id": None,  # filled by the caller from the snapshot row
        "vehicle_identifier": vid,
        "status": "on_map",
        **_negative_fields(state),
        "open_reports": reports,
        "last_observed_at": _iso(snapshot_time),
        "hours_missing": None,
        "last_seen": None,
        "gone_acknowledged_at": None,
        **_card(vid, model=row[0], form_factor=row[1]),
        "as_of": _iso(snapshot_time),
    }


def _explain_off_feed(cur, cycle_id: Any, snapshot_time: datetime,
                      raw_plate: str, want: str) -> dict[str, Any] | None:
    """A vehicle the current snapshot does not carry: missing, or gone.

    Matched in device_state by the HMAC identifier (src/identity.py's
    hash_plate, over the plate as read and as normalised — the ingest hashes
    the plate exactly as the feed spells it) or by the normalised stored
    plate. None — a 404 — when nothing matches, or more than one vehicle
    does."""
    candidates = sorted({v for v in (hash_plate(raw_plate), hash_plate(want)) if v})
    cur.execute(
        f"""
        SELECT ds.vehicle_identifier, ds.last_observed_at,
               ds.current_lat, ds.current_lon,
               ds.current_vehicle_model_name, ds.current_form_factor,
               a.status, a.acknowledged_at
          FROM device_state ds
          LEFT JOIN device_census_ack a USING (vehicle_identifier)
         WHERE ds.vehicle_identifier = ANY(%s)
            OR (ds.vehicle_plate IS NOT NULL AND {_SQL_NORMALIZED_STATE_PLATE} = %s)
         LIMIT 2
        """,
        (candidates, want),
    )
    rows = cur.fetchall()
    if len(rows) != 1:
        return None
    vid, last_seen_at, lat, lon, model, form_factor, ack_status, ack_at = rows[0]
    gone = ack_status == fleet_reports.CENSUS_STATUS_GONE
    reports = fleet_reports.open_reports_for(cur, cycle_id, vid)
    state = fleet_reports.negative_state_for(cur, cycle_id, vid)
    hours = (round((snapshot_time - last_seen_at).total_seconds() / 3600.0, 1)
             if last_seen_at and snapshot_time else None)
    return {
        "device_id": None,
        "vehicle_identifier": vid,
        "status": "gone" if gone else "missing",
        **_negative_fields(state),
        "open_reports": reports,
        "last_observed_at": _iso(last_seen_at),
        "hours_missing": hours,
        "last_seen": (
            {"lat": round(float(lat), _LAST_SEEN_DECIMALS),
             "lon": round(float(lon), _LAST_SEEN_DECIMALS)}
            if lat is not None and lon is not None else None),
        "gone_acknowledged_at": _iso(ack_at) if gone else None,
        **_card(vid, model=model, form_factor=form_factor),
        "as_of": _iso(snapshot_time),
    }
