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
src/main.py attaches RedactPlateQuery to the `uvicorn.access` logger.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from .accounts import SessionUser, require_session
from .api_public import latest_complete_cycle
from .client_ip import real_client_ip
from .pg import connection
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


_PLATE_QUERY = re.compile(r"([?&]plate=)[^&#]*")


def redact_plate_query(path: str) -> str:
    """`/api/v1/vehicles/resolve?plate=1025543` -> `…?plate=[redacted]`."""
    return _PLATE_QUERY.sub(r"\1[redacted]", path)


class RedactPlateQuery(logging.Filter):
    """Scrubs `plate=` from uvicorn access-log lines for the resolve route.

    uvicorn.access logs with args (client, method, full_path, http_version,
    status); only full_path is rewritten, and only for this route, so every
    other access line is untouched."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if (isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str)
                and args[2].startswith("/api/v1/vehicles/resolve")):
            record.args = (*args[:2], redact_plate_query(args[2]), *args[3:])
        return True


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
    plate: str = Query(..., description="Plate as printed / scanned; separators ignored"),
) -> dict[str, Any]:
    """plate -> the vehicle carrying it in the current snapshot.

    Public. Returns only `device_id` and `vehicle_identifier` — both already
    in the public devices payload — never the plate. 404 when no vehicle in
    the current snapshot carries it (or, defensively, when more than one
    does: missing beats wrong).
    """
    if len(plate) > _MAX_PLATE_LEN:
        raise HTTPException(400, detail=f"plate is at most {_MAX_PLATE_LEN} characters")
    want = normalize_plate(plate)
    if not want:
        raise HTTPException(400, detail="plate must not be empty")
    ip = real_client_ip(request) or "?"

    # The 404 is raised AFTER the connection block: an exception inside it
    # rolls the transaction back, which would un-record the rate-limit event
    # — and misses are exactly the enumeration traffic the limit is for.
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="vehicle_resolve_ip", key=ip,
                    limit=LIMIT_RESOLVE_PER_IP[0],
                    window_seconds=LIMIT_RESOLVE_PER_IP[1])
            cycle_id, _snapshot_time = latest_complete_cycle(cur)
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

    if len(rows) != 1:
        raise HTTPException(
            404,
            detail="no vehicle in the current snapshot carries that plate",
            headers={"Cache-Control": _RESOLVE_CACHE_HEADER},
        )
    device_id, vehicle_identifier = rows[0]
    response.headers["Cache-Control"] = _RESOLVE_CACHE_HEADER
    return {"device_id": device_id, "vehicle_identifier": vehicle_identifier}
