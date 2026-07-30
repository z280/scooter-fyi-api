"""Donated-ride battery ingestion (PLAN_RIDE_MODE_API.md phase A2, "Battery
ingestion"; src/battery_model.py:ingest_donated_observation).

Fake-cursor tests, following the idiom already established in this file
family (tests/test_ride_session_fields.py, tests/test_ride_usuals.py): a
small in-memory table standing in for battery_trip_observations (so the
double-count guard's DELETE and the INSERT's ON CONFLICT can both be
exercised against real predicate logic, not a scripted fetchone sequence)
plus a scripted donated_track_points/hourly_temperature answer per case.

ingest_donated_observation's own docstring documents the exact `ride_row`/
`donation_row` mapping this module (the donation-endpoint lane) is expected
to build and pass in; these tests pin that contract from the callee side.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import battery_model
from src.valhalla import ValhallaError

_VID = "aaaa000000000000"
_STARTED_AT = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
_ENDED_AT = datetime(2026, 7, 1, 12, 30, 0, tzinfo=timezone.utc)
_DONATION_ID = "11111111-1111-1111-1111-111111111111"


def _ride_row(**overrides) -> dict:
    row = {
        "vehicle_identifier": _VID,
        "track_key_issued_at": _STARTED_AT,
        "user_reported_ended_at": _ENDED_AT,
        "feed_start_battery_percent": 80,
        "reported_start_battery_percent": 78.0,
        "reported_battery_percent": 65.0,
    }
    row.update(overrides)
    return row


def _donation_row(**overrides) -> dict:
    row = {
        "id": _DONATION_ID,
        "vehicle_model": "Cosmo",
        "distance_meters": 4312.5,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Fake cursor: an in-memory battery_trip_observations + scripted reads for
# donated_track_points / hourly_temperature.
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(
        self,
        *,
        donated_points: list[tuple[float, float]] | None = None,
        temperature_c: float | None = 22.5,
        existing_observations: list[dict] | None = None,
    ):
        self._donated_points = list(donated_points) if donated_points is not None else None
        self._temperature_c = temperature_c
        self.observations: list[dict] = list(existing_observations or [])
        self.executed: list[tuple[str, tuple]] = []
        self._pending = None
        self._next_id = max([o["id"] for o in self.observations], default=0) + 1

    def execute(self, sql, params=()):
        joined = " ".join(sql.split())
        self.executed.append((joined, params))

        if joined.startswith("SELECT lat, lon FROM donated_track_points"):
            self.donation_id_queried = params[0]
            self._pending = ("points", self._donated_points or [])
        elif joined.startswith("SELECT temperature_c"):
            self._pending = ("temp", self._temperature_c)
        elif joined.startswith("DELETE FROM battery_trip_observations"):
            vehicle_identifier, departed_at, arrived_at = params
            before = len(self.observations)
            self.observations = [
                o for o in self.observations
                if not (
                    o["vehicle_identifier"] == vehicle_identifier
                    and departed_at <= o["departed_at"] <= arrived_at
                    and o.get("source") != "donated_ride"
                )
            ]
            self._pending = ("deleted", before - len(self.observations))
        elif joined.startswith("INSERT INTO battery_trip_observations"):
            cols = (
                "vehicle_identifier", "vehicle_model_name", "departed_at", "arrived_at",
                "duration_seconds", "from_lat", "from_lon", "to_lat", "to_lon",
                "route_distance_meters", "elevation_gain_meters", "temperature_c",
                "soc_start_percent", "soc_end_percent", "burn_percent", "source",
            )
            row = dict(zip(cols, params))
            conflict = any(
                o["vehicle_identifier"] == row["vehicle_identifier"]
                and o["departed_at"] == row["departed_at"]
                for o in self.observations
            )
            if conflict:
                self._pending = ("inserted", None)
            else:
                row["id"] = self._next_id
                self._next_id += 1
                self.observations.append(row)
                self._pending = ("inserted", row["id"])
        else:
            raise AssertionError(f"unexpected SQL in ingest_donated_observation: {joined}")

    def fetchall(self):
        kind, value = self._pending
        assert kind == "points", f"fetchall() called after a {kind} query"
        return [(lat, lon) for lat, lon in value]

    def fetchone(self):
        kind, value = self._pending
        if kind == "temp":
            return (value,) if value is not None else None
        if kind == "inserted":
            return (value,) if value is not None else None
        raise AssertionError(f"fetchone() called after a {kind} query")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_TRACK = [(39.7400, -104.9800), (39.7420, -104.9790), (39.7450, -104.9770)]


def _cur(**kw) -> _FakeCursor:
    kw.setdefault("donated_points", _TRACK)
    return _FakeCursor(**kw)


def _no_elevation(monkeypatch):
    """Most column-mapping tests don't care about elevation specifics —
    stub the Valhalla-touching helper to a fixed value so they don't have
    to mock valhalla.trace_attributes/route as well."""
    monkeypatch.setattr(battery_model, "_donated_elevation_gain_meters", lambda points: 12.3)


# ---------------------------------------------------------------------------
# Column mapping — every sql/024 column gets the right value
# ---------------------------------------------------------------------------

def test_every_sql_024_column_is_mapped_correctly(monkeypatch):
    _no_elevation(monkeypatch)
    cur = _cur()
    result = battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(), donation_row=_donation_row())

    assert result is not None
    assert result["id"] == 1
    row = cur.observations[0]
    assert row["vehicle_identifier"] == _VID
    assert row["vehicle_model_name"] == "Cosmo"
    assert row["departed_at"] == _STARTED_AT
    assert row["arrived_at"] == _ENDED_AT
    assert row["duration_seconds"] == pytest.approx(1800.0)
    assert row["from_lat"] == pytest.approx(_TRACK[0][0])
    assert row["from_lon"] == pytest.approx(_TRACK[0][1])
    assert row["to_lat"] == pytest.approx(_TRACK[-1][0])
    assert row["to_lon"] == pytest.approx(_TRACK[-1][1])
    assert row["route_distance_meters"] == pytest.approx(4312.5)
    assert row["elevation_gain_meters"] == pytest.approx(12.3)
    assert row["temperature_c"] == pytest.approx(22.5)
    # feed_start_battery_percent (80) wins over reported_start (78) —
    # see _resolve_soc's preference order.
    assert row["soc_start_percent"] == pytest.approx(80.0)
    assert row["soc_end_percent"] == pytest.approx(65.0)
    assert row["burn_percent"] == pytest.approx(15.0)
    assert row["source"] == "donated_ride"


def test_soc_start_falls_back_to_reported_when_feed_is_unknown(monkeypatch):
    _no_elevation(monkeypatch)
    cur = _cur()
    battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(feed_start_battery_percent=None),
        donation_row=_donation_row())
    row = cur.observations[0]
    assert row["soc_start_percent"] == pytest.approx(78.0)
    assert row["burn_percent"] == pytest.approx(13.0)


def test_vehicle_model_is_null_for_an_unconfirmed_model(monkeypatch):
    _no_elevation(monkeypatch)
    cur = _cur()
    battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(), donation_row=_donation_row(vehicle_model=None))
    assert cur.observations[0]["vehicle_model_name"] is None


# ---------------------------------------------------------------------------
# No-op gating — nothing honest to insert
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides", [
    {"feed_start_battery_percent": None, "reported_start_battery_percent": None},
    {"reported_battery_percent": None},
])
def test_unresolvable_battery_is_a_noop(monkeypatch, overrides):
    _no_elevation(monkeypatch)
    cur = _cur()
    result = battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(**overrides), donation_row=_donation_row())
    assert result is None
    assert cur.observations == []
    # Never even reads donated_track_points -- the gate runs first.
    assert not any("donated_track_points" in sql for sql, _ in cur.executed)


def test_unknown_distance_is_a_noop(monkeypatch):
    _no_elevation(monkeypatch)
    cur = _cur()
    result = battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(), donation_row=_donation_row(distance_meters=None))
    assert result is None
    assert cur.observations == []


def test_no_stored_waypoints_is_a_noop(monkeypatch):
    _no_elevation(monkeypatch)
    cur = _FakeCursor(donated_points=[])
    result = battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(), donation_row=_donation_row())
    assert result is None
    assert cur.observations == []


def test_a_conflicting_insert_no_ops_and_returns_none(monkeypatch):
    """ON CONFLICT (vehicle_identifier, departed_at) DO NOTHING: a retried
    call for the same ride is idempotent."""
    _no_elevation(monkeypatch)
    existing = [{
        "id": 1, "vehicle_identifier": _VID, "departed_at": _STARTED_AT,
        "source": "donated_ride",
    }]
    cur = _cur(existing_observations=existing)
    result = battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(), donation_row=_donation_row())
    assert result is None
    assert len(cur.observations) == 1  # nothing new appended


# ---------------------------------------------------------------------------
# DOUBLE-COUNT GUARD
# ---------------------------------------------------------------------------

def test_a_feed_mined_row_inside_the_window_is_deleted(monkeypatch):
    _no_elevation(monkeypatch)
    existing = [{
        "id": 99, "vehicle_identifier": _VID,
        "departed_at": _STARTED_AT + timedelta(minutes=5),  # inside [started, ended]
        "source": None,  # pre-sql/051, feed-mined
    }]
    cur = _cur(existing_observations=existing)
    battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(), donation_row=_donation_row())
    ids = [o.get("id") for o in cur.observations]
    assert 99 not in ids
    assert any(o["source"] == "donated_ride" for o in cur.observations)


def test_a_feed_mined_row_explicitly_tagged_source_is_also_deleted(monkeypatch):
    """source IS DISTINCT FROM 'donated_ride' catches BOTH the NULL
    (pre-sql/051) case above and an explicit 'feed_mined' tag."""
    _no_elevation(monkeypatch)
    existing = [{
        "id": 98, "vehicle_identifier": _VID,
        "departed_at": _STARTED_AT + timedelta(minutes=5),
        "source": "feed_mined",
    }]
    cur = _cur(existing_observations=existing)
    battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(), donation_row=_donation_row())
    assert 98 not in [o.get("id") for o in cur.observations]


def test_a_feed_mined_row_outside_the_window_is_left_alone(monkeypatch):
    _no_elevation(monkeypatch)
    existing = [{
        "id": 97, "vehicle_identifier": _VID,
        "departed_at": _STARTED_AT - timedelta(hours=3),  # well before this ride
        "source": None,
    }]
    cur = _cur(existing_observations=existing)
    battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(), donation_row=_donation_row())
    assert 97 in [o.get("id") for o in cur.observations]


def test_a_row_already_source_donated_ride_in_window_is_not_deleted(monkeypatch):
    """A previous donation's own row for this exact window must survive the
    guard -- only a FEED-MINED duplicate is the thing being cleaned up."""
    _no_elevation(monkeypatch)
    existing = [{
        "id": 96, "vehicle_identifier": _VID,
        "departed_at": _STARTED_AT + timedelta(minutes=2),
        "source": "donated_ride",
    }]
    cur = _cur(existing_observations=existing)
    battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(), donation_row=_donation_row())
    assert 96 in [o.get("id") for o in cur.observations]


def test_a_feed_mined_row_for_a_different_vehicle_is_left_alone(monkeypatch):
    _no_elevation(monkeypatch)
    existing = [{
        "id": 95, "vehicle_identifier": "bbbb000000000000",
        "departed_at": _STARTED_AT + timedelta(minutes=5),
        "source": None,
    }]
    cur = _cur(existing_observations=existing)
    battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(), donation_row=_donation_row())
    assert 95 in [o.get("id") for o in cur.observations]


def test_delete_runs_before_the_insert(monkeypatch):
    """Ordering matters for a real UNIQUE(vehicle_identifier, departed_at)
    backstop: the guard must clear a stale row before the new one lands."""
    _no_elevation(monkeypatch)
    cur = _cur()
    battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(), donation_row=_donation_row())
    kinds = [
        "DELETE" if sql.startswith("DELETE") else
        "INSERT" if sql.startswith("INSERT") else None
        for sql, _ in cur.executed
    ]
    kinds = [k for k in kinds if k]
    assert kinds.index("DELETE") < kinds.index("INSERT")


# ---------------------------------------------------------------------------
# Elevation: trace failure degrades to NULL, never raises
# ---------------------------------------------------------------------------

def test_elevation_is_null_when_map_matching_fails(monkeypatch):
    def _raise(*a, **kw):
        raise ValhallaError("valhalla unreachable")

    monkeypatch.setattr(battery_model.valhalla, "trace_attributes", _raise)
    cur = _cur()
    result = battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(), donation_row=_donation_row())
    assert result is not None  # ingestion still proceeds
    assert cur.observations[0]["elevation_gain_meters"] is None


def test_elevation_is_null_when_the_routing_call_fails(monkeypatch):
    monkeypatch.setattr(battery_model.valhalla, "trace_attributes", lambda *a, **kw: [])

    def _raise(*a, **kw):
        raise ValhallaError("no route")

    monkeypatch.setattr(battery_model.valhalla, "route", _raise)
    cur = _cur()
    result = battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(), donation_row=_donation_row())
    assert result is not None
    assert cur.observations[0]["elevation_gain_meters"] is None


def test_elevation_is_null_for_a_single_point_track(monkeypatch):
    cur = _FakeCursor(donated_points=[(39.74, -104.98)])
    result = battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(), donation_row=_donation_row())
    assert result is not None
    assert cur.observations[0]["elevation_gain_meters"] is None


def test_elevation_uses_the_full_track_for_map_matching(monkeypatch):
    """The map-match call must see every recorded waypoint, not just the
    endpoints -- otherwise the elevation profile can follow a completely
    different street pattern than the one actually ridden."""
    seen = {}

    def _trace(points, costing_options, shape_match="walk_or_snap"):
        seen["points"] = list(points)
        return []

    monkeypatch.setattr(battery_model.valhalla, "trace_attributes", _trace)
    monkeypatch.setattr(battery_model.valhalla, "route", lambda *a, **kw: {"trip": {}})
    monkeypatch.setattr(battery_model.valhalla, "all_trips", lambda body: [])
    cur = _cur()
    battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(), donation_row=_donation_row())
    assert seen["points"] == _TRACK


def test_elevation_route_call_is_downsampled_but_map_match_is_not(monkeypatch):
    long_track = [(39.7 + i * 0.0001, -104.9 + i * 0.0001) for i in range(50)]
    seen = {}

    monkeypatch.setattr(battery_model.valhalla, "trace_attributes",
                        lambda points, *a, **kw: seen.setdefault("trace_len", len(points)) or [])
    monkeypatch.setattr(battery_model.valhalla, "route",
                        lambda points, **kw: seen.setdefault("route_len", len(points)) or {"trip": {}})
    monkeypatch.setattr(battery_model.valhalla, "all_trips", lambda body: [])

    cur = _FakeCursor(donated_points=long_track)
    battery_model.ingest_donated_observation(
        cur, ride_row=_ride_row(), donation_row=_donation_row())

    assert seen["trace_len"] == 50  # full track, unmodified
    assert seen["route_len"] <= battery_model._MAX_ELEVATION_ROUTE_POINTS
    assert seen["route_len"] < seen["trace_len"]


# ---------------------------------------------------------------------------
# _downsample_for_routing (pure)
# ---------------------------------------------------------------------------

def test_downsample_is_a_noop_under_the_cap():
    points = [(float(i), float(i)) for i in range(10)]
    assert battery_model._downsample_for_routing(points, max_points=20) == points


def test_downsample_always_keeps_both_endpoints():
    points = [(float(i), float(i)) for i in range(100)]
    out = battery_model._downsample_for_routing(points, max_points=10)
    assert out[0] == points[0]
    assert out[-1] == points[-1]
    assert len(out) <= 10


# ---------------------------------------------------------------------------
# _resolve_soc (pure)
# ---------------------------------------------------------------------------

def test_resolve_soc_prefers_feed_over_reported():
    assert battery_model._resolve_soc(_ride_row()) == (80.0, 65.0)


def test_resolve_soc_falls_back_when_feed_is_none():
    assert battery_model._resolve_soc(
        _ride_row(feed_start_battery_percent=None)) == (78.0, 65.0)


def test_resolve_soc_is_none_when_both_start_sources_are_unknown():
    assert battery_model._resolve_soc(_ride_row(
        feed_start_battery_percent=None, reported_start_battery_percent=None)) is None


def test_resolve_soc_is_none_when_end_is_unknown():
    assert battery_model._resolve_soc(_ride_row(reported_battery_percent=None)) is None


# ---------------------------------------------------------------------------
# extract_trips' donated-ride skip (the OTHER direction of the double-count
# guard) -- static assertion on _PAIRS_SQL, same idiom as the pre-existing
# test_disabled_vehicles_are_excluded_in_sql.
# ---------------------------------------------------------------------------

def test_pairs_sql_skips_gaps_overlapping_a_donated_observation():
    sql = battery_model._PAIRS_SQL
    assert "d.source = 'donated_ride'" in sql
    assert "d.departed_at < o.t2" in sql
    assert "d.arrived_at > o.snapshot_time" in sql
