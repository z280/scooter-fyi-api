"""Routing a mined trip THROUGH its in-ride GBFS track (sql/070).

Because Veo keeps a rented vehicle in the feed, every rental arrives with a
position every 2 minutes for its whole duration. Routing
origin-to-destination throws that away, and measurably: over 250 real
episodes the waypoint route is 1.32x the direct route at p50 and 3.87x at
p90, and 6% of episodes are loops that finish within 400 m of where they
started while covering more than 800 m. Under a direct route those reach the
regression as a large burn over almost no distance.

These tests pin the preference order, the fallback, and the provenance
column that lets a later audit tell the two apart.
"""

from __future__ import annotations

import pytest

from src import battery_model


class _FakeCursor:
    def __init__(self, temp_row, sink):
        self.temp_row, self.sink = temp_row, sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        if "INSERT INTO battery_trip_observations" in sql:
            self.sink.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.temp_row


class _FakeConn:
    def __init__(self, temp_row=(14.0,)):
        self.temp_row = temp_row
        self.inserts: list[tuple[str, tuple]] = []

    def cursor(self):
        return _FakeCursor(self.temp_row, self.inserts)


def _cand(**over):
    cand = {
        "vehicle_identifier": "a56a83688e01cd4e",
        "vehicle_model_name": "Apollo",
        "departed_at": "2026-08-09T12:00:00+00:00",
        "arrived_at": "2026-08-09T12:12:00+00:00",
        "duration_seconds": 720,
        "latitude": 39.72555, "longitude": -104.98085,
        "lat2": 39.729218, "lon2": -105.027692,
        "soc_start": 80.0, "soc_end": 72.0, "burn": 8.0,
        "waypoints": [[39.723785, -104.983635], [39.734102, -104.993955],
                      [39.749001, -105.002239]],
    }
    cand.update(over)
    return cand


@pytest.fixture
def routes(monkeypatch):
    """Records every point-list Valhalla is asked for; `plan` maps the number
    of points to the distance to answer with, or to None to fail that call."""
    calls: list[list] = []
    plan: dict = {}

    def fake_route(points, **kw):
        calls.append(list(points))
        if plan.get(len(points), "missing") is None:
            raise battery_model.valhalla.ValhallaError("no route")
        return {"n": len(points)}

    def fake_all_trips(body):
        return [] if plan.get(body["n"]) is None else [body]

    def fake_trip_summary(trip):
        return {"distance_meters": plan.get(trip["n"], 5000.0),
                "elevation_gain_meters": 12.0}

    monkeypatch.setattr(battery_model.valhalla, "route", fake_route)
    monkeypatch.setattr(battery_model.valhalla, "all_trips", fake_all_trips)
    monkeypatch.setattr(battery_model.valhalla, "trip_summary", fake_trip_summary)
    return calls, plan


def _stats():
    return {"rejected_no_temperature": 0, "no_route": 0,
            "rejected_distance": 0, "rejected_speed": 0}


def _stored(conn):
    """The single INSERT as {column: value}, or None.

    Keyed by column name parsed out of the statement rather than by position:
    these assertions used negative indices until sql/071 appended a column and
    silently shifted every one of them.
    """
    if not conn.inserts:
        return None
    sql, params = conn.inserts[0]
    cols = sql.split("(", 1)[1].split(")", 1)[0]
    names = [c.strip() for c in cols.split(",")]
    assert len(names) == len(params), f"{len(names)} columns vs {len(params)} params"
    return dict(zip(names, params))


# ---------------------------------------------------------------------------

def test_waypoints_are_routed_through_not_around(routes):
    calls, plan = routes
    plan[5] = 4200.0            # origin + 3 waypoints + destination
    conn, stats = _FakeConn(), _stats()

    assert battery_model._route_and_store(conn, _cand(), stats) is True
    assert calls[0] == [(39.72555, -104.98085),
                        (39.723785, -104.983635),
                        (39.734102, -104.993955),
                        (39.749001, -105.002239),
                        (39.729218, -105.027692)]
    assert len(calls) == 1       # the direct route was never asked for
    assert _stored(conn)["route_distance_meters"] == 4200.0
    assert stats["routed_via_waypoints"] == 1


def test_direct_route_is_the_fallback_when_the_waypoint_route_fails(routes):
    calls, plan = routes
    plan[5] = None               # via-points will not thread
    plan[2] = 1800.0
    conn, stats = _FakeConn(), _stats()

    assert battery_model._route_and_store(conn, _cand(), stats) is True
    assert [len(c) for c in calls] == [5, 2]
    assert _stored(conn)["route_distance_meters"] == 1800.0
    assert stats["waypoint_route_failed"] == 1
    assert stats.get("routed_via_waypoints", 0) == 0


def test_waypoint_count_is_null_when_the_direct_route_was_used(routes):
    """NULL and 0 must not be conflated: a fallback row systematically
    understates ridden distance for its burn, and the fit has to be able to
    see which rows those are."""
    calls, plan = routes
    plan[5] = None
    plan[2] = 1800.0
    conn = _FakeConn()
    battery_model._route_and_store(conn, _cand(), _stats())
    assert _stored(conn)["waypoint_count"] is None


def test_waypoint_count_records_how_many_via_points_were_used(routes):
    calls, plan = routes
    plan[5] = 4200.0
    conn = _FakeConn()
    battery_model._route_and_store(conn, _cand(), _stats())
    assert _stored(conn)["waypoint_count"] == 3


def test_a_candidate_with_no_track_routes_directly(routes):
    calls, plan = routes
    plan[2] = 1800.0
    conn, stats = _FakeConn(), _stats()

    assert battery_model._route_and_store(conn, _cand(waypoints=[]), stats) is True
    assert [len(c) for c in calls] == [2]
    assert stats.get("waypoint_route_failed", 0) == 0   # nothing to fail
    assert _stored(conn)["waypoint_count"] is None


def test_both_routes_failing_is_a_no_route(routes):
    calls, plan = routes
    plan[5] = None
    plan[2] = None
    conn, stats = _FakeConn(), _stats()

    assert battery_model._route_and_store(conn, _cand(), stats) is False
    assert stats["no_route"] == 1
    assert conn.inserts == []


def test_rows_are_tagged_gbfs_rental(routes):
    """sql/070 widened the source CHECK for exactly this. 'feed_mined' means
    the old observation-gap model, and an audit of the fit has to be able to
    tell an outage-derived row from a rental-derived one."""
    calls, plan = routes
    plan[5] = 4200.0
    conn = _FakeConn()
    battery_model._route_and_store(conn, _cand(), _stats())
    assert _stored(conn)["source"] == "gbfs_rental"


def test_distance_floor_applies_to_the_waypoint_route(routes):
    """A loop: 250 m of displacement, 3 km actually ridden. The direct route
    would be rejected by the distance floor; the real one must not be."""
    calls, plan = routes
    plan[5] = 3000.0
    plan[2] = 250.0
    conn, stats = _FakeConn(), _stats()

    assert battery_model._route_and_store(conn, _cand(), stats) is True
    assert stats["rejected_distance"] == 0
    assert _stored(conn)["route_distance_meters"] == 3000.0


def test_speed_floor_uses_the_waypoint_distance(routes):
    """Same trip, judged on displacement vs on distance ridden. 250 m over 12
    minutes is 0.8 mph and would be thrown out; 3 km is 9.3 mph and stays."""
    assert battery_model._implied_mph(250.0, 720) < battery_model.MIN_IMPLIED_MPH
    assert battery_model._implied_mph(3000.0, 720) > battery_model.MIN_IMPLIED_MPH


def test_waypoints_are_capped(routes):
    calls, plan = routes
    n = battery_model.MAX_ROUTE_WAYPOINTS
    plan[n + 2] = 9000.0
    conn = _FakeConn()
    many = [[39.7 + i / 10000.0, -105.0] for i in range(n + 25)]
    battery_model._route_and_store(conn, _cand(waypoints=many), _stats())
    assert len(calls[0]) == n + 2
    assert _stored(conn)["waypoint_count"] == n
