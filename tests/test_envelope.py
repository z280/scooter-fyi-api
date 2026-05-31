"""Envelope tagging never drops data — outliers get a label, not a delete."""

import pytest

from src import ingest


@pytest.fixture
def vt_map():
    return {
        "1": ingest.VehicleType(form_factor="scooter", propulsion_type="electric"),
        "3": ingest.VehicleType(form_factor="bicycle", propulsion_type="electric"),
    }


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


def test_extra_fields_captured(vt_map):
    p = _payload(
        {
            "bike_id": "g",
            "lat": 39.74,
            "lon": -104.99,
            "vehicle_type_id": "1",
            "is_disabled": True,
            "is_reserved": False,
            "current_range_meters": 12345,
            "rental_uris": {
                "android": "https://gmjc.adj.st/?adj_t=5vyf0nr&number=1025543",
                "ios": "https://gmjc.adj.st/?adj_t=5vyf0nr&number=1025543",
            },
        },
    )
    out = ingest.tag_envelope(p, vt_map)
    d = out.devices[0]
    assert d.vehicle_plate == "1025543"
    # vehicle_identifier is sha256("1025543")[:16] — deterministic & unsalted
    from src.identity import hash_plate
    assert d.vehicle_identifier == hash_plate("1025543")
    assert d.vehicle_identifier != d.vehicle_plate  # confirm it's actually hashed
    assert len(d.vehicle_identifier) == 16
    assert d.is_disabled is True
    assert d.is_reserved is False
    assert d.current_range_meters == 12345
    assert d.propulsion_type == "electric"


def test_extra_fields_default_to_none_when_missing(vt_map):
    p = _payload({"bike_id": "h", "lat": 39.74, "lon": -104.99, "vehicle_type_id": "1"})
    d = ingest.tag_envelope(p, vt_map).devices[0]
    assert d.vehicle_plate is None
    assert d.vehicle_identifier is None
    assert d.is_disabled is None
    assert d.is_reserved is None
    assert d.current_range_meters is None
    # propulsion_type comes from vt_map, which the fixture populates
    assert d.propulsion_type == "electric"


def test_vehicle_plate_extraction_handles_query_order(vt_map):
    p = _payload(
        {
            "bike_id": "i",
            "lat": 39.74,
            "lon": -104.99,
            "vehicle_type_id": "1",
            "rental_uris": {"android": "https://x/?number=42&other=1"},
        },
        {
            "bike_id": "j",
            "lat": 39.74,
            "lon": -104.99,
            "vehicle_type_id": "1",
            "rental_uris": {"android": "https://x/?other=1&number=99"},
        },
        {
            "bike_id": "k",
            "lat": 39.74,
            "lon": -104.99,
            "vehicle_type_id": "1",
            "rental_uris": {"android": "https://x/?no-number-here"},
        },
    )
    out = ingest.tag_envelope(p, vt_map)
    by_id = {d.device_id: d.vehicle_plate for d in out.devices}
    assert by_id == {"i": "42", "j": "99", "k": None}


def test_vehicle_identifier_is_deterministic_for_a_given_salt():
    """The hash must be stable for a fixed salt + plate. Different plates
    produce different identifiers; null/empty produces null."""
    from src.identity import hash_plate
    assert hash_plate("1025543") == hash_plate("1025543")
    assert hash_plate("1025543") != hash_plate("1025544")
    assert hash_plate(None) is None
    assert hash_plate("") is None


def test_vehicle_identifier_depends_on_salt(monkeypatch):
    """Changing the salt changes the identifier — confirms the salt is
    actually being mixed in, not silently ignored."""
    from src import identity
    monkeypatch.setenv("VEHICLE_IDENTIFIER_SALT", "salt-A")
    a = identity.hash_plate("1025543")
    monkeypatch.setenv("VEHICLE_IDENTIFIER_SALT", "salt-B")
    b = identity.hash_plate("1025543")
    assert a != b
    assert len(a) == len(b) == 16


def test_hash_plate_raises_when_salt_unset(monkeypatch):
    """Missing salt is a hard error, not a silent degradation."""
    from src import identity
    monkeypatch.delenv("VEHICLE_IDENTIFIER_SALT", raising=False)
    import pytest
    with pytest.raises(RuntimeError, match="VEHICLE_IDENTIFIER_SALT"):
        identity.hash_plate("1025543")


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
