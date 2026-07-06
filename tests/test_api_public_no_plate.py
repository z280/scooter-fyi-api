"""Public /api/v1/devices/current must NOT leak vehicle_plate.

The §1.1 plate promotion was reverted: the raw plate is private-only. This
drives the real devices_current handler with a fake connection and asserts
(a) no feature carries vehicle_plate, and (b) the two fields that trail the
removed plate column in the SELECT — vehicle_use_type, vehicle_model_name —
still map to the right positional index (guards the index shift the revert
required).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from src import api_public

_CYCLE_ID = uuid.UUID("8f3a2d10-1234-4abc-8def-0123456789ab")
_SNAP = datetime(2026, 7, 5, 14, 30, tzinfo=timezone.utc)

# One synthetic raw_telemetry_points row in the EXACT column order the
# handler's SELECT produces after the plate removal (26 columns, r[0]..r[25]).
# r[24]=vehicle_use_type, r[25]=vehicle_model_name — the two that shifted.
_ROW = (
    "dev1",                 # 0  device_id
    "scooter",              # 1  form_factor
    39.7392,                # 2  latitude
    -104.9876,              # 3  longitude
    "denver_core",          # 4  spatial_status
    "8c4a1f0d2e9b7a35",     # 5  vehicle_identifier
    False,                  # 6  is_disabled
    False,                  # 7  is_reserved
    45293,                  # 8  current_range_meters
    "electric",             # 9  propulsion_type
    111, 222, 333,          # 10-12 h3_8/9/10
    "75",                   # 13 range_percentile_by_type
    "40/52",                # 14 range_rank_unique_by_type
    "3100/4100",            # 15 range_rank_all_by_type
    "3100/6000",            # 16 range_rank_all_devices
    "12/40", "3/8", "1/1",  # 17-19 h3 peer ranks
    False,                  # 20 has_negative_report
    52800,                  # 21 max_range_meters_for_type
    0,                      # 22 number_failed_starts
    None,                   # 23 first_observed_at_location
    "standing",             # 24 vehicle_use_type
    "Astro",                # 25 vehicle_model_name
)


class _FakeCursor:
    def execute(self, *a, **k):
        pass

    def fetchone(self):
        # The handler's first query resolves the latest completed cycle.
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
    # Peer-relative dwell stats run their own (real) query + cache — stub
    # them out; the no-stats path must leave the dwell fields null.
    monkeypatch.setattr(api_public, "stats_for_cycle", lambda cycle_id: {})


def test_public_devices_current_omits_vehicle_plate(_fake_db):
    out = api_public.devices_current(form_factor=None, spatial_status=None, include_outliers=False, bbox=None)
    props = out["features"][0]["properties"]
    assert "vehicle_plate" not in props, "raw plate must not appear on the public endpoint"


def test_trailing_fields_still_map_after_plate_removal(_fake_db):
    """Proves the r[25]->r[24], r[26]->r[25] shift is correct."""
    props = api_public.devices_current(form_factor=None, spatial_status=None, include_outliers=False, bbox=None)["features"][0]["properties"]
    assert props["vehicle_use_type"] == "standing"
    assert props["vehicle_model_name"] == "Astro"
    # And a spot-check that nothing else shifted:
    assert props["vehicle_identifier"] == "8c4a1f0d2e9b7a35"
    assert props["current_range_meters"] == 45293
    assert props["reliability_tier"] in ("ok", "unknown", "high_risk")
