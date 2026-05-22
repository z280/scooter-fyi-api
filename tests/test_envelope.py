"""Envelope tagging never drops data — outliers get a label, not a delete."""

import pytest

from src import ingest


@pytest.fixture
def vt_map():
    return {"1": "scooter", "3": "bicycle"}


def _payload(*records):
    return {"last_updated": 1700000000, "data": {"bikes": list(records)}}


def test_denver_core_tagged(vt_map):
    p = _payload(
        {"bike_id": "a", "lat": 39.74, "lon": -104.99, "vehicle_type_id": "1"},
    )
    out = ingest.tag_envelope(p, vt_map)
    assert len(out.devices) == 1
    assert out.devices[0].spatial_status == "denver_core"
    assert out.devices[0].form_factor == "scooter"


def test_china_glitch_tagged_not_dropped(vt_map):
    p = _payload(
        {"bike_id": "b", "lat": 22.5, "lon": 114.0, "vehicle_type_id": "3"},
    )
    out = ingest.tag_envelope(p, vt_map)
    assert len(out.devices) == 1
    assert out.devices[0].spatial_status == "china_glitch"
    assert out.devices[0].form_factor == "bicycle"


def test_other_outlier(vt_map):
    p = _payload(
        {"bike_id": "c", "lat": 0.0, "lon": 0.0, "vehicle_type_id": "1"},
    )
    out = ingest.tag_envelope(p, vt_map)
    assert out.devices[0].spatial_status == "other_outlier"


def test_missing_coordinates_skipped(vt_map):
    p = _payload(
        {"bike_id": "d", "vehicle_type_id": "1"},
        {"bike_id": "e", "lat": "nope", "lon": -104.9},
        {"bike_id": "f", "lat": 39.7, "lon": -104.9, "vehicle_type_id": "1"},
    )
    out = ingest.tag_envelope(p, vt_map)
    assert {d.device_id for d in out.devices} == {"f"}


def test_payload_hash_is_stable(vt_map):
    p = _payload(
        {"bike_id": "a", "lat": 39.74, "lon": -104.99, "vehicle_type_id": "1"},
        {"bike_id": "b", "lat": 39.75, "lon": -104.98, "vehicle_type_id": "3"},
    )
    h1 = ingest.tag_envelope(p, vt_map).payload_sha256
    # Reordering bikes shouldn't change the hash (we sort before hashing)
    p2 = _payload(
        {"bike_id": "b", "lat": 39.75, "lon": -104.98, "vehicle_type_id": "3"},
        {"bike_id": "a", "lat": 39.74, "lon": -104.99, "vehicle_type_id": "1"},
    )
    h2 = ingest.tag_envelope(p2, vt_map).payload_sha256
    assert h1 == h2
