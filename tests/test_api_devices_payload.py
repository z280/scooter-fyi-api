"""Payload-diet contract for /api/v1/devices/current + /api/v1/equity-estimate.

Locks the lean default field set, the ?include= opt-ins (ranks, h3), the
string encoding of opted-in h3 indexes, battery_percent, the dwell
evidence fields, and the cycle-keyed ETag/304 revalidation flow.
"""

from __future__ import annotations

import uuid
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime, timezone

import h3
import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from src import api_public
from src.quality import DwellPeerStats, compute_battery_percent

_CYCLE_ID = uuid.UUID("8f3a2d10-1234-4abc-8def-0123456789ab")
_SNAP = datetime(2026, 7, 6, 14, 30, tzinfo=timezone.utc)

_H3_8 = h3.str_to_int(h3.latlng_to_cell(39.7392, -104.9876, 8))
_H3_9 = h3.str_to_int(h3.latlng_to_cell(39.7392, -104.9876, 9))
_H3_10 = h3.str_to_int(h3.latlng_to_cell(39.7392, -104.9876, 10))

_ROW = (
    "dev1", "scooter", 39.7392, -104.9876, "denver_core",
    "8c4a1f0d2e9b7a35",          # 5  vehicle_identifier
    False, False,                # 6-7 is_disabled / is_reserved
    45293,                       # 8  current_range_meters
    "electric",                  # 9  propulsion_type
    _H3_8, _H3_9, _H3_10,        # 10-12
    "75", "40/52", "3100/4100", "3100/6000", "12/40", "3/8", "1/1",  # 13-19 ranks
    False,                       # 20 has_negative_report
    52800,                       # 21 max_range_meters_for_type
    0,                           # 22 number_failed_starts
    None,                        # 23 first_observed_at_location
    "standing", "Astro",         # 24-25
)

_RANK_FIELDS = (
    "range_percentile_by_type", "range_rank_unique_by_type",
    "range_rank_all_by_type", "range_rank_all_devices",
    "range_rank_h3_8_peers", "range_rank_h3_9_peers", "range_rank_h3_10_peers",
)
_H3_FIELDS = ("h3_8_index", "h3_9_index", "h3_10_index")


class _FakeCursor:
    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return (_CYCLE_ID, _SNAP)

    def fetchall(self):
        return [_ROW]

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

    monkeypatch.setattr(api_public, "connection", _conn)
    monkeypatch.setattr(
        api_public, "stats_for_cycle",
        lambda cycle_id, snapshot_time: {
            "8c4a1f0d2e9b7a35": DwellPeerStats(
                dwell_hours=31.0, percentile=0.96, peer_median_hours=6.04,
                peer_count=12, is_outlier=False,
            )
        },
    )


def _request(headers: dict[str, str] | None = None) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/v1/devices/current",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "query_string": b"",
    })


def _call(headers=None, **kwargs):
    defaults = dict(
        form_factor=None, spatial_status=None, include_outliers=False,
        bbox=None, include=None,
    )
    defaults.update(kwargs)
    return api_public.devices_current(_request(headers), Response(), **defaults)


# ---------- lean default + ?include= opt-ins ---------------------------------
def test_default_payload_omits_rank_and_h3_fields(_fake_db):
    props = _call()["features"][0]["properties"]
    for f in _RANK_FIELDS + _H3_FIELDS:
        assert f not in props, f"{f} must be opt-in"


def test_include_ranks_restores_rank_fields(_fake_db):
    props = _call(include="ranks")["features"][0]["properties"]
    for f in _RANK_FIELDS:
        assert f in props
    assert props["range_percentile_by_type"] == "75"
    assert props["range_rank_h3_10_peers"] == "1/1"
    for f in _H3_FIELDS:
        assert f not in props


def test_include_h3_is_string_encoded(_fake_db):
    props = _call(include="h3")["features"][0]["properties"]
    assert props["h3_9_index"] == h3.int_to_str(_H3_9)
    assert isinstance(props["h3_8_index"], str)
    assert isinstance(props["h3_10_index"], str)
    for f in _RANK_FIELDS:
        assert f not in props


def test_include_both_tokens(_fake_db):
    out = _call(include="ranks,h3")
    props = out["features"][0]["properties"]
    for f in _RANK_FIELDS + _H3_FIELDS:
        assert f in props
    assert out["metadata"]["include"] == ["h3", "ranks"]


def test_unknown_include_token_is_400(_fake_db):
    with pytest.raises(HTTPException) as e:
        _call(include="ranks,bogus")
    assert e.value.status_code == 400
    assert "bogus" in e.value.detail


# ---------- battery + dwell evidence -----------------------------------------
def test_battery_percent_derived_from_type_max(_fake_db):
    props = _call()["features"][0]["properties"]
    assert props["battery_percent"] == 86  # round(100 * 45293 / 52800)


def test_dwell_evidence_fields_passed_through(_fake_db):
    props = _call()["features"][0]["properties"]
    assert props["dwell_percentile_hood"] == 96
    assert props["dwell_peer_median_hours"] == 6.0  # rounded to 1 dp


def test_battery_percent_edge_cases():
    assert compute_battery_percent(None, 52800) is None
    assert compute_battery_percent(45293, None) is None
    assert compute_battery_percent(45293, 0) is None
    assert compute_battery_percent(60000, 52800) == 100  # clamp: range > rated max
    assert compute_battery_percent(0, 52800) == 0
    assert compute_battery_percent(26400, 52800) == 50


# ---------- ETag / 304 --------------------------------------------------------
def test_etag_set_and_304_on_revalidation(_fake_db):
    resp = Response()
    api_public.devices_current(
        _request(), resp,
        form_factor=None, spatial_status=None, include_outliers=False,
        bbox=None, include=None,
    )
    etag = resp.headers["etag"]
    assert str(_CYCLE_ID) in etag
    assert "max-age=30" in resp.headers["cache-control"]

    out = _call(headers={"If-None-Match": etag})
    assert isinstance(out, Response)
    assert out.status_code == 304
    assert out.headers["etag"] == etag


def test_etag_varies_by_include_tokens(_fake_db):
    r1, r2 = Response(), Response()
    api_public.devices_current(
        _request(), r1,
        form_factor=None, spatial_status=None, include_outliers=False,
        bbox=None, include=None,
    )
    api_public.devices_current(
        _request(), r2,
        form_factor=None, spatial_status=None, include_outliers=False,
        bbox=None, include="ranks",
    )
    assert r1.headers["etag"] != r2.headers["etag"]


def test_etag_varies_by_filters(_fake_db):
    """form_factor / spatial_status / include_outliers / bbox each change the
    body, so each must change the ETag — otherwise a client reusing a tag
    across filtered requests gets a 304 for a different representation."""
    variants = [
        dict(form_factor=None, spatial_status=None, include_outliers=False, bbox=None, include=None),
        dict(form_factor="scooter", spatial_status=None, include_outliers=False, bbox=None, include=None),
        dict(form_factor=None, spatial_status="china_glitch", include_outliers=False, bbox=None, include=None),
        dict(form_factor=None, spatial_status=None, include_outliers=True, bbox=None, include=None),
        dict(form_factor=None, spatial_status=None, include_outliers=False, bbox="-105,39.6,-104.9,39.8", include=None),
    ]
    tags = set()
    for v in variants:
        resp = Response()
        api_public.devices_current(_request(), resp, **v)
        tags.add(resp.headers["etag"])
    assert len(tags) == len(variants), "each filter combination needs a distinct ETag"


def test_invalid_bbox_400s_even_when_etag_matches(_fake_db):
    """bbox is validated BEFORE the 304 short-circuit."""
    # First get a valid ETag for the default query.
    resp = Response()
    api_public.devices_current(
        _request(), resp,
        form_factor=None, spatial_status=None, include_outliers=False,
        bbox=None, include=None,
    )
    etag = resp.headers["etag"]
    # Re-request WITH that ETag but a malformed bbox — must 400, not 304.
    with pytest.raises(HTTPException) as e:
        api_public.devices_current(
            _request({"If-None-Match": etag}), Response(),
            form_factor=None, spatial_status=None, include_outliers=False,
            bbox="not,a,bbox,!!", include=None,
        )
    assert e.value.status_code == 400


# ---------- /api/v1/equity-estimate -------------------------------------------
_Col = namedtuple("_Col", "name")

_SNAPSHOT_FIELDS = {
    "cycle_id": _CYCLE_ID,
    "snapshot_time": _SNAP,
    "total_devices_denver": 8000,
    "total_bike_denver": 5000,
    "total_scooter_denver": 3000,
    "total_devices_er1": 1200, "total_bike_er1": 800, "total_scooter_er1": 400,
    "total_devices_er2": 800, "total_bike_er2": 500, "total_scooter_er2": 300,
    "total_devices_er3": 400, "total_bike_er3": 250, "total_scooter_er3": 150,
}


class _SnapCursor:
    description = [_Col(k) for k in _SNAPSHOT_FIELDS]

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return tuple(_SNAPSHOT_FIELDS.values())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def _fake_snapshot_db(monkeypatch):
    class _Conn:
        def cursor(self):
            return _SnapCursor()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    @contextmanager
    def _conn():
        yield _Conn()

    monkeypatch.setattr(api_public, "connection", _conn)


def test_equity_estimate_sums_selected_ranks(_fake_snapshot_db):
    out = api_public.equity_estimate(_request(), Response(), ranks="2,1,2")
    assert out["ranks"] == [1, 2]  # deduped + sorted
    assert out["total_devices"] == 2000
    assert out["total_bikes"] == 1300
    assert out["total_scooters"] == 700
    assert out["percent_all_devices"] == 25.0
    assert out["percent_all_bikes"] == 26.0
    assert out["percent_all_scooters"] == 23.33


def test_equity_estimate_validates_ranks(_fake_snapshot_db):
    for bad in ("", "0", "7", "1,abc"):
        with pytest.raises(HTTPException) as e:
            api_public.equity_estimate(_request(), Response(), ranks=bad)
        assert e.value.status_code == 400


def test_equity_estimate_304(_fake_snapshot_db):
    resp = Response()
    api_public.equity_estimate(_request(), resp, ranks="1,2")
    etag = resp.headers["etag"]
    out = api_public.equity_estimate(
        _request({"If-None-Match": etag}), Response(), ranks="1,2"
    )
    assert isinstance(out, Response)
    assert out.status_code == 304
