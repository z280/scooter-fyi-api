"""update_for_cycle's rental handling (sql/069) against a fake cursor.

tests/test_device_state.py covers only the pure distance helper, on the
grounds that "the DB-touching path needs a real Postgres and is exercised
in the live container". That left the four-way branch — the thing that
runs every two minutes against seven thousand devices and writes
trip_events — with no coverage at all, which is how a rental came to be
counted as ~10 trips for a month without anything failing.

The fake here is deliberately thin: it records SQL and parameters, and
answers the one SELECT update_for_cycle makes. That is enough to assert
the transitions, which is what the rental fix changes. Same fake-cursor
shape as tests/test_ride_watch.py.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from src import device_state
from src.ingest import TaggedDevice

_T0 = datetime(2026, 8, 8, 19, 22, 2, tzinfo=timezone.utc)
_VID = "a56a83688e01cd4e"

# Denver, ~1.6 km apart — well over the stationary threshold.
_ORIGIN = (39.725550, -104.980850)
_DROP = (39.729218, -105.027692)
_NUDGE = (39.725558, -104.980858)   # ~1 m — inside the threshold


def _device(lat_lon=_ORIGIN, *, is_reserved=None, device_id="bike-1") -> TaggedDevice:
    lat, lon = lat_lon
    return TaggedDevice(
        device_id=device_id, vehicle_type_id="1", form_factor="scooter",
        lat=lat, lon=lon, spatial_status="denver_core",
        vehicle_identifier=_VID, vehicle_plate="1025899",
        current_range_meters=27948, is_reserved=is_reserved,
    )


class _FakeCursor:
    def __init__(self, state: dict | None):
        self.state = state
        self.calls: list[tuple[str, str, list]] = []  # (kind, sql, params)

    # -- recording ----------------------------------------------------------
    def execute(self, sql, params=()):
        self.calls.append(("execute", " ".join(sql.split()), [params]))

    def executemany(self, sql, seq):
        self.calls.append(("executemany", " ".join(sql.split()), list(seq)))

    def fetchall(self):
        if self.state is None:
            return []
        return [(
            _VID,
            self.state["device_id"],
            self.state["lat"],
            self.state["lon"],
            self.state["first_observed_at_location"],
            self.state["number_failed_starts"],
            self.state["first_ever_observed_at"],
            self.state["rental_started_at"],
        )]

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    # -- assertions ---------------------------------------------------------
    def rows_for(self, needle: str) -> list:
        """Every parameter tuple written by a statement containing `needle`."""
        return [p for _, sql, params in self.calls if needle in sql for p in params]

    def ran(self, needle: str) -> bool:
        return any(needle in sql for _, sql, _ in self.calls)


class _FakeConn:
    def __init__(self, state):
        self.cur = _FakeCursor(state)
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


@pytest.fixture
def cycle(monkeypatch):
    """Returns run(devices, state=..., at=...) -> (stats, cursor)."""
    monkeypatch.setattr(device_state.device_features, "seed_catalog_features",
                        lambda cur: None)

    def run(devices, *, state, at=_T0):
        conn = _FakeConn(state)

        @contextmanager
        def _fake_connection():
            yield conn

        monkeypatch.setattr(device_state, "connection", _fake_connection)
        stats = device_state.update_for_cycle(uuid.uuid4(), at, devices)
        return stats, conn.cur

    return run


def _known(lat_lon=_ORIGIN, *, rental_started_at=None, device_id="bike-1") -> dict:
    lat, lon = lat_lon
    return {
        "device_id": device_id, "lat": lat, "lon": lon,
        "first_observed_at_location": _T0 - timedelta(hours=2),
        "number_failed_starts": 0,
        "first_ever_observed_at": _T0 - timedelta(days=30),
        "rental_started_at": rental_started_at,
    }


# ---------------------------------------------------------------------------
# The regression: a rental sampled every 2 minutes must produce ONE trip
# ---------------------------------------------------------------------------

def test_first_reserved_cycle_starts_a_rental_and_writes_no_trip(cycle):
    stats, cur = cycle([_device(_ORIGIN, is_reserved=True)], state=_known())
    assert (stats.rentals_started, stats.moved) == (1, 0)
    assert not cur.ran("INSERT INTO trip_events")
    assert cur.ran("rental_started_at = %s")
    # The origin stop is closed — the rider has it — but no new stop opens.
    assert cur.ran("SET departed_at")
    assert not cur.ran("INSERT INTO device_history")


def test_moving_while_reserved_does_not_move_the_stored_position(cycle):
    """THE BUG. Pre-sql/069 this wrote a MOVED, a trip_events row and a new
    device_history stop — once every two minutes, for the whole rental."""
    mid_ride = _device((39.734102, -104.993955), is_reserved=True)
    stats, cur = cycle([mid_ride], state=_known(rental_started_at=_T0))
    assert (stats.rentals_held, stats.moved) == (1, 0)
    assert not cur.ran("INSERT INTO trip_events")
    assert not cur.ran("INSERT INTO device_history")
    assert not cur.ran("current_lat = %s")


def test_release_produces_exactly_one_trip_origin_to_drop_point(cycle):
    stats, cur = cycle([_device(_DROP, is_reserved=False, device_id="bike-2")],
                       state=_known(rental_started_at=_T0))
    assert (stats.rentals_ended, stats.moved) == (1, 1)
    trips = cur.rows_for("INSERT INTO trip_events")
    assert len(trips) == 1
    # (vid, plate, cycle, at, form, use, model, from_lat, from_lon, to_lat, to_lon, m)
    assert (trips[0][7], trips[0][8]) == _ORIGIN     # frozen origin, not mid-route
    assert (trips[0][9], trips[0][10]) == _DROP
    assert trips[0][11] == pytest.approx(4030, rel=0.05)
    assert cur.ran("rental_started_at = NULL")


def test_ten_cycle_rental_yields_one_trip_not_ten(cycle):
    """End to end over the real sample sequence: the shape of the whole
    fix in one assertion."""
    route = [_ORIGIN, (39.723785, -104.983635), (39.734102, -104.993955),
             (39.749001, -105.002239), (39.736751, -105.024462)]
    state = _known()
    trips = 0
    for i, pos in enumerate(route):                       # reserved, moving
        stats, cur = cycle([_device(pos, is_reserved=True)], state=state)
        trips += len(cur.rows_for("INSERT INTO trip_events"))
        if i == 0:
            state["rental_started_at"] = _T0              # what the UPDATE did
    stats, cur = cycle([_device(_DROP, is_reserved=False)], state=state)
    trips += len(cur.rows_for("INSERT INTO trip_events"))
    assert trips == 1


# ---------------------------------------------------------------------------
# Releases that aren't relocations
# ---------------------------------------------------------------------------

def test_release_within_threshold_reopens_the_stop_but_records_no_trip(cycle):
    """A cancelled reservation, or a round trip. The vehicle demonstrably
    left and came back, so dwell restarts — but nothing relocated."""
    stats, cur = cycle([_device(_NUDGE, is_reserved=False)],
                       state=_known(rental_started_at=_T0))
    assert (stats.rentals_ended, stats.moved) == (1, 0)
    assert not cur.ran("INSERT INTO trip_events")
    assert len(cur.rows_for("INSERT INTO device_history")) == 1   # stop reopened
    assert cur.ran("rental_started_at = NULL")


def test_release_never_leaves_a_device_without_an_open_stop(cycle):
    """The first reserved cycle closes the open stop. Whatever the release
    distance, something must reopen one, or the vehicle has no open stop
    and idx_device_history_open_stops / dwell go blind."""
    for pos in (_DROP, _NUDGE):
        _, cur = cycle([_device(pos, is_reserved=False)],
                       state=_known(rental_started_at=_T0))
        assert len(cur.rows_for("INSERT INTO device_history")) == 1


# ---------------------------------------------------------------------------
# Degradation: the other operator convention, and a feed that goes quiet
# ---------------------------------------------------------------------------

def test_absent_while_rented_still_yields_a_single_moved(cycle):
    """An operator that DOES drop rented vehicles never sets the flag at
    all — the vehicle simply isn't in the payload, nothing updates, and
    the reappearance elsewhere is one ordinary MOVED. Unchanged by
    sql/069, asserted so it stays that way."""
    stats, cur = cycle([_device(_DROP, is_reserved=None)], state=_known())
    assert (stats.moved, stats.rentals_started, stats.rentals_ended) == (1, 0, 0)
    assert len(cur.rows_for("INSERT INTO trip_events")) == 1


def test_is_reserved_none_is_not_a_rental(cycle):
    """src/ingest.py normalises a missing or non-bool is_reserved to None.
    None must read as available, or a feed that stops publishing the flag
    would freeze every device's position permanently."""
    stats, _ = cycle([_device(_ORIGIN, is_reserved=None)], state=_known())
    assert stats.rentals_started == 0
    assert stats.stationary == 1


def test_is_reserved_false_is_not_a_rental(cycle):
    stats, _ = cycle([_device(_ORIGIN, is_reserved=False)], state=_known())
    assert stats.rentals_started == 0
    assert stats.stationary == 1


# ---------------------------------------------------------------------------
# Interactions with the other branches
# ---------------------------------------------------------------------------

def test_rotating_bike_id_during_a_rental_is_not_a_failed_start(cycle):
    """GBFS rotates bike_id per trip. The rental's first cycle records the
    new device_id, so the release compares like with like instead of
    reading the rotation as a failed unlock at the drop point."""
    _, cur = cycle([_device(_ORIGIN, is_reserved=True, device_id="bike-2")],
                   state=_known(device_id="bike-1"))
    assert cur.ran("rental_started_at = %s")
    assert not cur.ran("number_failed_starts = number_failed_starts + 1")


def test_new_device_first_seen_mid_rental_is_flagged(cycle):
    """Its origin is unknowable, but the one-trip-per-rental invariant
    still has to hold on release."""
    stats, cur = cycle([_device(_ORIGIN, is_reserved=True)], state=None)
    assert (stats.new_devices, stats.rentals_started) == (1, 1)
    inserted = cur.rows_for("INSERT INTO device_state")
    assert inserted[0][-1] == _T0        # rental_started_at, last column


def test_new_device_not_in_a_rental_has_no_flag(cycle):
    stats, cur = cycle([_device(_ORIGIN, is_reserved=False)], state=None)
    assert (stats.new_devices, stats.rentals_started) == (1, 0)
    assert cur.rows_for("INSERT INTO device_state")[0][-1] is None


def test_mixed_fleet_partitions_in_one_pass(cycle):
    """Nothing above tests more than one device at a time; the real cycle
    passes ~7000. Asserts the per-device branch state doesn't leak."""
    others = [
        TaggedDevice(device_id="b2", vehicle_type_id="1", form_factor="scooter",
                     lat=39.70, lon=-104.90, spatial_status="denver_core",
                     vehicle_identifier="ffff000000000000", is_reserved=True),
        TaggedDevice(device_id="b3", vehicle_type_id="1", form_factor="scooter",
                     lat=39.71, lon=-104.91, spatial_status="denver_core",
                     vehicle_identifier=None),   # no identifier -> skipped
    ]
    stats, _ = cycle([_device(_ORIGIN, is_reserved=True)] + others, state=_known())
    assert stats.skipped_no_identifier == 1
    # The known device starts a rental; the unknown reserved one is NEW and
    # is flagged by the NEW branch.
    assert stats.rentals_started == 2
    assert stats.new_devices == 1
