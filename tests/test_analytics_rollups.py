"""Fleet analytics rollups (sql/094): the pure aggregation, and the endpoints'
validation. The SQL is exercised on real Postgres in test_analytics_rollups_pg.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import analytics_rollups as ar

T = datetime(2026, 10, 1, 15, 37, tzinfo=timezone.utc)   # 09:37 Denver


@pytest.fixture(autouse=True)
def _regions(monkeypatch):
    """Two fake areas split at longitude -105: west = NB_West/CD_1."""
    def regions(lat, lon):
        if lat is None or lon is None:
            return (ar.CITY,)
        if lon < -105:
            return (ar.CITY, ("neighborhood", "NB_West"), ("council_district", "CD_1"))
        return (ar.CITY, ("neighborhood", "NB_East"))
    monkeypatch.setattr(ar, "regions_for", regions)


def test_rides_are_counted_once_per_layer_by_start_point_and_hour():
    rows = [
        (1, T, "Cosmo", 39.7, -105.1),
        (2, T + timedelta(minutes=10), "Cosmo", 39.7, -105.1),
        (3, T, None, 39.7, -104.9),
        (4, T + timedelta(hours=1), "Astro", None, None),
    ]
    acc = ar.aggregate_rides(rows)
    h = T.replace(minute=0)
    assert acc[(h, "city", "Denver", "Cosmo")] == 2
    assert acc[(h, "neighborhood", "NB_West", "Cosmo")] == 2
    assert acc[(h, "council_district", "CD_1", "Cosmo")] == 2
    assert acc[(h, "neighborhood", "NB_East", "Unknown")] == 1
    # No point: counted in the city, in no region.
    assert acc[(h + timedelta(hours=1), "city", "Denver", "Astro")] == 1
    assert not any(k[1] != "city" and k[3] == "Astro" for k in acc)


def test_stops_feed_failed_starts_at_departure_and_dwell_by_denver_day():
    arrived = T - timedelta(hours=2)
    late = datetime(2026, 10, 2, 5, 30, tzinfo=timezone.utc)   # 23:30 Denver, Oct 1
    rows = [
        (arrived, T, "Cosmo", 39.7, -105.1, 2),                 # 2 failed starts, 2 h dwell
        (arrived, T, "Cosmo", 39.7, -105.1, 0),                 # dwell only
        (late - timedelta(minutes=30), late, "Astro", 39.7, -104.9, 0),
        (arrived, arrived + timedelta(days=40), "Cosmo", 39.7, -105.1, 0),  # > 30 days: no dwell
    ]
    failed, dwell = ar.aggregate_stops(rows)
    h = T.replace(minute=0)
    assert failed[(h, "city", "Denver", "Cosmo")] == [2, 1]
    assert failed[(h, "neighborhood", "NB_West", "Cosmo")] == [2, 1]
    day = T.astimezone(ar.DENVER).date()
    assert dwell[(day, "city", "Denver", "Cosmo")] == [2, 2 * 7200]
    # 05:30 UTC on the 2nd is still the 1st in Denver.
    assert dwell[(datetime(2026, 10, 1).date(), "neighborhood", "NB_East", "Astro")] == [1, 1800]
    assert sum(v[0] for k, v in dwell.items() if k[1] == "city") == 3


def test_region_lookup_includes_city_and_is_cached(monkeypatch):
    calls = []
    monkeypatch.undo()   # the real regions_for, with a fake layer lookup

    def fake(layer, lon, lat):
        calls.append(layer)
        return {"neighborhood": "NB_X"}.get(layer)

    import src.geo as geo
    monkeypatch.setattr(geo, "region_for_point", fake)
    ar._regions_at.cache_clear()
    got = ar.regions_for(39.74001, -104.98001)
    assert got == (ar.CITY, ("neighborhood", "NB_X"))
    ar.regions_for(39.74003, -104.98002)   # same 4-decimal cell
    assert len(calls) == len(ar.REGION_LAYERS)
    ar._regions_at.cache_clear()


# --- endpoint validation ---------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src import api_analytics
    monkeypatch.setattr(api_analytics.geo, "region_names", lambda layer: ["NB_FivePoints", "CD_9"])
    app = FastAPI()
    app.include_router(api_analytics.router)
    return TestClient(app)


@pytest.mark.parametrize("url", [
    "/api/v1/analytics/rides?granularity=minute",
    "/api/v1/analytics/rides?granularity=hour&days=60",
    "/api/v1/analytics/rides?days=0",
    "/api/v1/analytics/rides?region_type=zipcode",
    "/api/v1/analytics/rides?region_type=neighborhood",
    "/api/v1/analytics/failed-starts?region_type=neighborhood",
    "/api/v1/analytics/devices-by-region?region_type=city",
    "/api/v1/analytics/dwell?region_type=zipcode",
    "/api/v1/analytics/equity-compliance?granularity=year",
])
def test_bad_parameters_are_400s(client, url):
    assert client.get(url).status_code == 400


def test_an_unknown_region_is_a_404(client):
    r = client.get("/api/v1/analytics/rides?region_type=neighborhood&region_name=NB_Atlantis")
    assert r.status_code == 404
