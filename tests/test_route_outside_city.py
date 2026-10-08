"""The outside-the-city warning on route options (owner, 2026-10-08): the
graph covers the basemap's extent, but the route styles are shaped by City of
Denver data, so a trip touching land outside the city says so."""

from __future__ import annotations

import pytest

from src import api_route


@pytest.fixture
def city(monkeypatch):
    """Inside the city = latitude below 39.80 (a stand-in boundary)."""
    import src.geo as geo
    monkeypatch.setattr(geo, "region_for_point",
                        lambda layer, lon, lat: ("CD_1" if lat < 39.80 else None)
                        if layer == "council_district" else None)


def test_an_in_city_trip_has_no_warning(city):
    got = api_route.city_coverage((39.74, -104.99), (39.75, -105.00))
    assert got == {"outside_city": {"from": False, "to": False}, "outside_city_warning": None}


@pytest.mark.parametrize("origin,dest,flags", [
    ((39.86, -104.67), (39.75, -105.00), {"from": True, "to": False}),   # starts outside
    ((39.74, -104.99), (39.93, -104.98), {"from": False, "to": True}),   # ends outside
])
def test_either_end_outside_gets_the_owners_wording(city, origin, dest, flags):
    got = api_route.city_coverage(origin, dest)
    assert got["outside_city"] == flags
    assert got["outside_city_warning"] == (
        "Scooter.fyi uses City of Denver data to optimize routing. Your routing starts or "
        "ends outside of the city and thus may not be as optimized as in-city routes would be.")


def test_an_unreadable_boundary_shows_no_warning_rather_than_a_wrong_one(monkeypatch):
    import src.geo as geo

    def boom(*a):
        raise FileNotFoundError("/app/data/CD.geojson")

    monkeypatch.setattr(geo, "region_for_point", boom)
    got = api_route.city_coverage((39.86, -104.67), (39.75, -105.00))
    assert got == {"outside_city": {"from": None, "to": None}, "outside_city_warning": None}
