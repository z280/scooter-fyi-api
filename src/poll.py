import json
import sys
import time
import urllib.request
from datetime import datetime, timezone

from .config import DENVER_BBOX, CONTRACT_PCT, GBFS_URL, POLL_INTERVAL, TIMEOUT
from .db import insert_snapshot
from .geometry import PolygonIndex


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def _pct(n: int, d: int) -> float:
    return round(n / d * 100, 2) if d else 0.0


def _fetch_bikes() -> list:
    req = urllib.request.Request(GBFS_URL, headers={"User-Agent": "veo-audit/2.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read())
    return data.get("data", {}).get("bikes", [])


def take_snapshot(v1: PolygonIndex, v2: PolygonIndex) -> bool:
    try:
        bikes = _fetch_bikes()
    except Exception as exc:
        _log(f"GBFS fetch failed: {exc!r}")
        return False

    fleet_total = len(bikes)

    min_lon, min_lat, max_lon, max_lat = DENVER_BBOX
    denver = [
        b for b in bikes
        if min_lat < b.get("lat", 0) < max_lat
        and min_lon < b.get("lon", 0) < max_lon
    ]
    fleet_in_denver = len(denver)

    scooters_den = scooters_v1 = scooters_v2 = 0
    ebikes_den   = ebikes_v1   = ebikes_v2   = 0
    reserved = disabled = 0

    for b in denver:
        vt    = b.get("vehicle_type_id")
        lon   = b["lon"]
        lat   = b["lat"]
        in_v1 = v1.contains(lon, lat)
        in_v2 = v2.contains(lon, lat)

        if b.get("is_reserved"): reserved += 1
        if b.get("is_disabled"): disabled += 1

        if vt == "1":
            scooters_den += 1
            if in_v1: scooters_v1 += 1
            if in_v2: scooters_v2 += 1
        elif vt == "3":
            ebikes_den += 1
            if in_v1: ebikes_v1 += 1
            if in_v2: ebikes_v2 += 1

    all_v1 = scooters_v1 + ebikes_v1
    all_v2 = scooters_v2 + ebikes_v2

    row = {
        "ts_utc":                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # (1-3) totals
        "fleet_total":               fleet_total,
        "fleet_in_denver":           fleet_in_denver,
        "fleet_outside_denver":      fleet_total - fleet_in_denver,
        # (2-3) all devices in designated areas
        "all_in_v1":                 all_v1,
        "all_in_v2":                 all_v2,
        # (4-6) scooters
        "scooters_in_denver":        scooters_den,
        "scooters_in_v1":            scooters_v1,
        "scooters_in_v2":            scooters_v2,
        # (7-9) eBikes
        "ebikes_in_denver":          ebikes_den,
        "ebikes_in_v1":              ebikes_v1,
        "ebikes_in_v2":              ebikes_v2,
        # (15-17) % of fleet in v1, by type
        "all_pct_in_v1":             _pct(all_v1,      fleet_in_denver),
        "scooters_pct_in_v1":        _pct(scooters_v1, scooters_den),
        "ebikes_pct_in_v1":          _pct(ebikes_v1,   ebikes_den),
        # (18-20) % of fleet in v2, by type
        "all_pct_in_v2":             _pct(all_v2,      fleet_in_denver),
        "scooters_pct_in_v2":        _pct(scooters_v2, scooters_den),
        "ebikes_pct_in_v2":          _pct(ebikes_v2,   ebikes_den),
        # (21-22) eBike share of motorized vehicles within each area
        "type3_pct_of_motorized_v1": _pct(ebikes_v1, all_v1),
        "type3_pct_of_motorized_v2": _pct(ebikes_v2, all_v2),
        # (23-24) compliance booleans
        "v1_over_30pct":             1 if _pct(all_v1, fleet_in_denver) > CONTRACT_PCT else 0,
        "v2_over_30pct":             1 if _pct(all_v2, fleet_in_denver) > CONTRACT_PCT else 0,
        "reserved_count":            reserved,
        "disabled_count":            disabled,
    }

    insert_snapshot(row)
    _log(
        f"OK fleet={fleet_in_denver} "
        f"v1={all_v1}({row['all_pct_in_v1']}%) "
        f"v2={all_v2}({row['all_pct_in_v2']}%) "
        f"v1_ok={bool(row['v1_over_30pct'])} v2_ok={bool(row['v2_over_30pct'])}"
    )
    return True


def run_loop(v1: PolygonIndex, v2: PolygonIndex) -> None:
    _log(f"Poll loop started (interval={POLL_INTERVAL}s)")
    while True:
        take_snapshot(v1, v2)
        time.sleep(POLL_INTERVAL)
