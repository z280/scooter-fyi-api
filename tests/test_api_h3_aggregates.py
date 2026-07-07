"""Contract tests for GET /api/v1/h3/aggregates.

Drives the real handler with an SQL-dispatching fake connection: one
downtown cell with parked devices (battery / risk / dwell math), one
cell with trips only (zero-device semantics), hourly-peak bucketing,
string cell keys, and the cycle-keyed ETag/304 flow.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import h3
import pytest
from fastapi import Response
from starlette.requests import Request

from src import api_h3

_CYCLE_ID = uuid.UUID("8f3a2d10-1234-4abc-8def-0123456789ab")
# Dwell/reliability are measured as of snapshot_time (cycle-deterministic),
# so fixture first_observed_at_location offsets anchor to _SNAP, not wall clock.
_SNAP = datetime(2026, 7, 6, 14, 30, tzinfo=timezone.utc)
_NOW = _SNAP

_CELL_A = h3.latlng_to_cell(39.7392, -104.9876, 9)   # downtown, devices + 1 trip
_CELL_B = h3.latlng_to_cell(39.7500, -105.0000, 9)   # trips only
assert _CELL_A != _CELL_B

# (h3_idx, vid, is_disabled, is_reserved, range_m, max_range_m,
#  failed_starts, first_observed_at_location, has_negative_report)
_DEVICE_ROWS = [
    # ok tier, battery 100 (LUT top), dwell 2h
    (h3.str_to_int(_CELL_A), "v-ok", False, False, 45293, 52800,
     0, _NOW - timedelta(hours=2), False),
    # high_risk (live negative report), battery 58 (off-LUT fallback), dwell 30h
    (h3.str_to_int(_CELL_A), "v-risk", False, False, 26400, 52800,
     0, _NOW - timedelta(hours=30), True),
    # untracked + rangeless: unknown tier, no battery, no dwell sample
    (h3.str_to_int(_CELL_A), "v-untracked", False, False, None, 52800,
     None, None, False),
]

# (detected_at, from_lat, from_lon) — B gets 3 starts in the 13:00 UTC hour
# and 1 at 10:10 (peak 3); A gets a single start.
_TRIP_ROWS = [
    (_SNAP.replace(hour=13, minute=5), 39.7500, -105.0000),
    (_SNAP.replace(hour=13, minute=20), 39.7500, -105.0000),
    (_SNAP.replace(hour=13, minute=45), 39.7500, -105.0000),
    (_SNAP.replace(hour=10, minute=10), 39.7500, -105.0000),
    (_SNAP.replace(hour=12, minute=15), 39.7392, -104.9876),
]


def _reindex(h3_9_int: int, res: int) -> int:
    """What the DB's h3_{res}_index column would hold for this device."""
    cell = h3.int_to_str(h3_9_int)
    if res == 9:
        return h3_9_int
    if res == 8:
        return h3.str_to_int(h3.cell_to_parent(cell, 8))
    return h3.str_to_int(h3.cell_to_center_child(cell, res))


class _FakeCursor:
    def __init__(self):
        self._last_sql = ""

    def execute(self, sql, *a, **k):
        self._last_sql = sql

    def fetchone(self):
        assert "observation_cycles" in self._last_sql
        return (_CYCLE_ID, _SNAP)

    def fetchall(self):
        if "raw_telemetry_points" in self._last_sql:
            import re

            res = int(re.search(r"h3_(\d+)_index,", self._last_sql).group(1))
            return [(_reindex(r[0], res),) + r[1:] for r in _DEVICE_ROWS]
        if "trip_events" in self._last_sql:
            return _TRIP_ROWS
        raise AssertionError(f"unexpected fetchall for: {self._last_sql[:80]}")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def _fake_db(monkeypatch):
    @contextmanager
    def _conn():
        yield _FakeConn()

    monkeypatch.setattr(api_h3, "connection", _conn)
    monkeypatch.setattr(api_h3, "stats_for_cycle", lambda cycle_id, snapshot_time: {})


def _request(headers: dict[str, str] | None = None) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/v1/h3/aggregates",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "query_string": b"",
    })


def _call(res: int = 9, headers=None):
    return api_h3.h3_aggregates(_request(headers), Response(), res=res)


def test_cell_keys_are_h3_strings(_fake_db):
    out = _call()
    assert set(out["cells"]) == {_CELL_A, _CELL_B}
    for key in out["cells"]:
        assert isinstance(key, str)
        assert h3.is_valid_cell(key)
    assert out["res"] == 9
    assert out["cycle_id"] == str(_CYCLE_ID)


def test_device_cell_aggregates(_fake_db):
    a = _call()["cells"][_CELL_A]
    assert a["device_count"] == 3
    assert a["avg_battery_percent"] == 79     # mean(100, 58); rangeless excluded
    assert a["risk_share"] == 0.33            # 1 high_risk of 3
    assert a["avg_dwell_hours"] == 16.0       # mean(2h, 30h); untracked excluded
    assert a["trips_started_24h"] == 1
    assert a["starts_per_hour_peak"] == 1


def test_trip_only_cell_semantics(_fake_db):
    b = _call()["cells"][_CELL_B]
    assert b["device_count"] == 0
    assert b["trips_started_24h"] == 4
    assert b["starts_per_hour_peak"] == 3     # three starts in the 13:00 UTC hour
    assert b["avg_battery_percent"] is None
    assert b["risk_share"] is None            # no parked devices to take a share of
    assert b["avg_dwell_hours"] is None


def test_resolution_changes_bucketing(_fake_db):
    """At res 8 the same trips land in res-8 cells (still string-keyed)."""
    out = _call(res=8)
    assert all(h3.get_resolution(k) == 8 for k in out["cells"])


def test_payload_is_cycle_deterministic(_fake_db):
    """Same cycle → byte-identical body (nothing reads the wall clock), so the
    cycle-keyed ETag never fronts two different representations. dwell is
    anchored to snapshot_time: v-ok=2h, v-risk=30h → avg 16.0h."""
    a = _call()
    b = _call()
    assert a == b
    assert a["cells"][_CELL_A]["avg_dwell_hours"] == 16.0


def test_etag_and_304(_fake_db):
    resp = Response()
    api_h3.h3_aggregates(_request(), resp, res=9)
    etag = resp.headers["etag"]
    assert str(_CYCLE_ID) in etag and ":9:" in etag
    assert resp.headers["cache-control"] == "public, max-age=600"

    out = _call(headers={"If-None-Match": etag})
    assert isinstance(out, Response)
    assert out.status_code == 304

    # A different resolution must NOT revalidate against the res-9 tag.
    out8 = _call(res=8, headers={"If-None-Match": etag})
    assert not isinstance(out8, Response)


def test_dwell_outlier_feeds_risk_share(_fake_db, monkeypatch):
    """A 50h-dwell outlier flips to high_risk and moves the cell's share."""
    from src.quality import DwellPeerStats

    rows = [
        (h3.str_to_int(_CELL_A), "v-ok", False, False, 45293, 52800,
         0, _NOW - timedelta(hours=2), False),
        # 50h clean dwell: under the 72h ghost rule on its own, over the
        # 48h gate once the peer-outlier flag is set.
        (h3.str_to_int(_CELL_A), "v-ghostish", False, False, 40000, 52800,
         0, _NOW - timedelta(hours=50), False),
    ]
    monkeypatch.setattr("tests.test_api_h3_aggregates._DEVICE_ROWS", rows)

    baseline = _call()["cells"][_CELL_A]
    assert baseline["risk_share"] == 0.0  # 50h alone is not high_risk

    monkeypatch.setattr(
        api_h3, "stats_for_cycle",
        lambda cycle_id, snapshot_time: {
            "v-ghostish": DwellPeerStats(
                dwell_hours=50.0, percentile=0.95, peer_median_hours=5.0,
                peer_count=9, is_outlier=True,
            ),
        },
    )
    flagged = _call()["cells"][_CELL_A]
    assert flagged["risk_share"] == 0.5
