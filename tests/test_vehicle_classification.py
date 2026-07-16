"""Ground-truth vehicle classification overrides (ingest._KNOWN_VEHICLE_TYPES).

Veo's upstream vehicle_types.json mislabels some seated e-bikes as "scooter".
tag_envelope() applies our field-confirmed overrides so the standing-scooter
share — which rides against the contract fleet cap — isn't inflated by
seated bikes wearing the wrong label. This pins the id=5 (Cosmo) override,
field-confirmed 2026-07-16, plus the unknown-type fallback.

No Postgres or VEHICLE_IDENTIFIER_SALT needed: tag_envelope() uses the real
config.json envelope, and a bike with no rental_uris has no plate, so
hash_plate() short-circuits before touching the salt.
"""

from __future__ import annotations

from src.ingest import VehicleType, tag_envelope


def _tag_one(vt_id: str, vt_map: dict[str, VehicleType]):
    # Denver-core coords, no rental_uris (→ no plate → no salt needed).
    payload = {
        "data": {"bikes": [{"bike_id": "t1", "vehicle_type_id": vt_id,
                            "lat": 39.7392, "lon": -104.9903}]},
        "last_updated": 1,
    }
    devices = tag_envelope(payload, vt_map).devices
    assert len(devices) == 1
    return devices[0]


def test_type5_overrides_veos_wrong_scooter_label_to_bicycle():
    # Veo's registry calls type 5 a scooter (the mislabel we're correcting).
    vt_map = {"5": VehicleType(form_factor="scooter",
                               propulsion_type="electric", max_range_meters=67000)}
    d = _tag_one("5", vt_map)
    assert d.form_factor == "bicycle"        # override beats Veo's "scooter"
    assert d.vehicle_use_type == "sitting"   # seated → counts as sitting
    assert d.vehicle_model_name == "Cosmo"


def test_unknown_type_falls_back_to_veo_registry():
    # A type we don't override keeps Veo's label as-is (no silent guessing).
    vt_map = {"9": VehicleType(form_factor="scooter")}
    d = _tag_one("9", vt_map)
    assert d.form_factor == "scooter"
    assert d.vehicle_model_name is None
    assert d.vehicle_use_type == "standing"  # derived from form_factor
