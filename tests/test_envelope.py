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


def test_known_vehicle_type_reclassifies_mislabeled_apollo():
    """id=4 is declared 'scooter' in Veo's live vehicle_types feed, but is
    the pedal-equipped, seated 'Apollo' bike — confirmed by direct visual
    inspection. Overridden to bicycle regardless of what the registry
    says, since form_factor drives the RFP compliance percentages. Also
    carries the in-app model name and the "sitting" use_type."""
    vt_map = {
        "4": ingest.VehicleType(form_factor="scooter", propulsion_type="electric",
                                 max_range_meters=67000),
    }
    p = _payload({"bike_id": "apollo1", "lat": 39.74, "lon": -104.99, "vehicle_type_id": "4"})
    d = ingest.tag_envelope(p, vt_map).devices[0]
    assert d.form_factor == "bicycle"
    assert d.vehicle_model_name == "Apollo"
    assert d.vehicle_use_type == "sitting"
    # propulsion_type / max_range still come straight from the registry —
    # only form_factor is corrected.
    assert d.propulsion_type == "electric"
    assert d.max_range_meters_for_type == 67000


def test_known_vehicle_type_applies_even_without_a_vt_map_entry():
    """The override must fire even if the live vehicle_types.json fetch
    failed and id=4 isn't in vt_map at all (falls through to 'unknown'
    otherwise, since the raw GBFS payload never carries form_factor)."""
    p = _payload({"bike_id": "apollo2", "lat": 39.74, "lon": -104.99, "vehicle_type_id": "4"})
    d = ingest.tag_envelope(p, vt_map={}).devices[0]
    assert d.form_factor == "bicycle"
    assert d.vehicle_model_name == "Apollo"
    assert d.vehicle_use_type == "sitting"


def test_known_vehicle_type_is_noop_on_form_factor_when_registry_agrees(vt_map):
    """id=3 (Cosmo) already reports 'bicycle' correctly in the registry —
    listed in the known-types table anyway (as the single documented
    source of what's been visually verified), but form_factor doesn't
    change. Model name/use_type are still populated."""
    p = _payload({"bike_id": "cosmo1", "lat": 39.74, "lon": -104.99, "vehicle_type_id": "3"})
    d = ingest.tag_envelope(p, vt_map).devices[0]
    assert d.form_factor == "bicycle"
    assert d.vehicle_model_name == "Cosmo"
    assert d.vehicle_use_type == "sitting"


def test_astro_scooter_is_standing():
    vt_map = {"1": ingest.VehicleType(form_factor="scooter", propulsion_type="electric")}
    p = _payload({"bike_id": "astro1", "lat": 39.74, "lon": -104.99, "vehicle_type_id": "1"})
    d = ingest.tag_envelope(p, vt_map).devices[0]
    assert d.form_factor == "scooter"
    assert d.vehicle_model_name == "Astro"
    assert d.vehicle_use_type == "standing"


def test_unconfirmed_id5_not_touched_but_use_type_derived_from_form_factor():
    """id=5 shares id=4's 'scooter'/67000m registry entry but hasn't been
    visually confirmed — must NOT be silently reclassified or given a
    model name. use_type still gets derived from (unmodified) form_factor
    as a fallback, since standing/sitting is inferable even without a
    confirmed model name."""
    vt_map = {"5": ingest.VehicleType(form_factor="scooter", propulsion_type="electric")}
    p = _payload({"bike_id": "unknown5", "lat": 39.74, "lon": -104.99, "vehicle_type_id": "5"})
    d = ingest.tag_envelope(p, vt_map).devices[0]
    assert d.form_factor == "scooter"
    assert d.vehicle_model_name is None
    assert d.vehicle_use_type == "standing"


def test_unknown_form_factor_has_no_use_type():
    p = _payload({"bike_id": "mystery", "lat": 39.74, "lon": -104.99, "vehicle_type_id": "99"})
    d = ingest.tag_envelope(p, vt_map={}).devices[0]
    assert d.form_factor == "unknown"
    assert d.vehicle_use_type is None
    assert d.vehicle_model_name is None


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


def test_h3_indexes_computed_for_each_device(vt_map):
    p = _payload(
        {"bike_id": "h3a", "lat": 39.74, "lon": -104.99, "vehicle_type_id": "1"},
        {"bike_id": "h3b", "lat": 39.74, "lon": -104.99, "vehicle_type_id": "1"},  # same spot
        {"bike_id": "h3c", "lat": 39.75, "lon": -104.98, "vehicle_type_id": "1"},  # different spot
    )
    out = ingest.tag_envelope(p, vt_map)
    by_id = {d.device_id: d for d in out.devices}
    # All three resolutions populated as BIGINT-safe ints
    for d in out.devices:
        assert isinstance(d.h3_8_index, int)
        assert isinstance(d.h3_9_index, int)
        assert isinstance(d.h3_10_index, int)
        assert -(1 << 63) <= d.h3_10_index < (1 << 63), "h3 index must fit signed bigint"
    # Same point → same cell at every resolution
    a, b = by_id["h3a"], by_id["h3b"]
    assert (a.h3_8_index, a.h3_9_index, a.h3_10_index) == (b.h3_8_index, b.h3_9_index, b.h3_10_index)
    # Different points → different h3_10 cells (~75m hexagons; the test deltas
    # are ~1.1 km, well above one cell's edge)
    c = by_id["h3c"]
    assert a.h3_10_index != c.h3_10_index


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
