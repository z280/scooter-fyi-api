"""sql/083 ghost-stop closing, against real Postgres.

Drives src/device_state.update_for_cycle cycle by cycle (writing
observation_cycles / snapshot_metadata_core first, as the real cycle does;
the absence rule itself counts the cycles device_state records in sql/086's
ledger), and src/ghost_stops' cleanup and dry
run, and checks what device_history ends up holding:

  * a vehicle absent past the threshold has its stop closed at its LAST
    OBSERVED time, as 'absent'; a vehicle in the feed never does;
  * reserved (rented) and disabled vehicles in the feed never close as absent;
  * a vehicle that comes back gets a new stop (same spot) or an ordinary
    MOVED (elsewhere), the old stop keeping its absent close, and its dwell
    clock in device_state is not reset;
  * the cleanup is idempotent;
  * the dry run writes nothing, proven on a READ ONLY session that refuses
    the real run.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
Like tests/test_area_leaders_pg.py, it wipes device_history / device_state /
trip_events, snapshot_metadata_core and device_state_processed_cycles (whose
newest rows the absence rule reads) at setup.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from src import device_state, equity_backfill, ghost_stops  # noqa: E402
from src.ingest import TaggedDevice  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_T = device_state.ABSENT_STOP_AFTER
_K = device_state.ABSENT_MIN_MISSED_CYCLES
_M = device_state.ABSENT_SWEEP_WINDOW_CYCLES
_EVERY = timedelta(minutes=10)   # coarser than production's 2 min, same rule
# 2031-09-15 is in MDT: the 6-9 AM Denver SLA window is 12:00-15:00 UTC.
_DAY = date(2031, 9, 15)
_T0 = datetime(2031, 9, 15, 12, 0, tzinfo=timezone.utc)

_SPOT = (39.725550, -104.980850)
_NUDGE = (39.725558, -104.980858)      # ~1 m, inside the stationary threshold
_ELSEWHERE = (39.729218, -105.027692)  # ~4 km


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


def _vid(n: int) -> str:
    return f"9e0{n:013x}"


def _dev(n: int, lat_lon=_SPOT, *, is_reserved=None, is_disabled=None,
         device_id=None) -> TaggedDevice:
    lat, lon = lat_lon
    return TaggedDevice(
        device_id=device_id or f"bike-{n}", vehicle_type_id="1",
        form_factor="scooter", lat=lat, lon=lon, spatial_status="denver_core",
        vehicle_plate=f"P{n:06d}", vehicle_identifier=_vid(n),
        is_reserved=is_reserved, is_disabled=is_disabled,
        current_range_meters=20000,
    )


@pytest.fixture()
def pg(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — ghost stop Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM trip_events")
        cur.execute("DELETE FROM device_history")
        cur.execute("DELETE FROM device_state")
        cur.execute("DELETE FROM snapshot_metadata_core")
        cur.execute("DELETE FROM device_state_processed_cycles")
    conn.commit()

    @contextmanager
    def _conn():
        yield conn

    for mod in (device_state, ghost_stops, equity_backfill):
        monkeypatch.setattr(mod, "connection", _conn)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _observe(conn, t: datetime, devices: list[TaggedDevice]) -> device_state.StateUpdateStats:
    """One cycle as src/cycle.py runs it: the snapshot row first, then state."""
    cid = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO observation_cycles (cycle_id, start_ts, job_status) "
            "VALUES (%s, %s, 'complete')", (str(cid), t))
        cur.execute(
            "INSERT INTO snapshot_metadata_core (cycle_id, snapshot_time, total_devices_denver) "
            "VALUES (%s, %s, %s)", (str(cid), t, len(devices)))
    conn.commit()
    return device_state.update_for_cycle(cid, t, devices)


def _stops(conn, n: int) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT snapshot_time, departed_at, departure_reason, lat, lon, dwell_failed_starts "
            "FROM device_history WHERE vehicle_identifier = %s ORDER BY id", (_vid(n),))
        return cur.fetchall()


def _state(conn, n: int) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT first_observed_at_location, last_observed_at, number_failed_starts "
            "FROM device_state WHERE vehicle_identifier = %s", (_vid(n),))
        return cur.fetchone()


def _trips(conn, n: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM trip_events WHERE vehicle_identifier = %s", (_vid(n),))
        return cur.fetchone()[0]


def _run(conn, start: datetime, until: datetime, fleet) -> datetime:
    """Observe every _EVERY from `start` up to `until`; `fleet(t)` gives the
    devices in the feed at t. Returns the last cycle time."""
    t = start
    last = start
    while t <= until:
        _observe(conn, t, fleet(t))
        last = t
        t += _EVERY
    return last


# ---------------------------------------------------------------------------

def test_absent_past_threshold_closes_at_last_observed(pg):
    left_at = _T0 + 3 * _EVERY
    horizon = left_at + _T + (_K + 2) * _EVERY

    def fleet(t):
        return [_dev(1)] + ([_dev(2)] if t <= left_at else [])

    # Just under the threshold: still open.
    _run(pg, _T0, left_at + _T - _EVERY, fleet)
    assert _stops(pg, 2) == [(_T0, None, None, *_SPOT, 0)]

    _run(pg, left_at + _T, horizon, fleet)
    [(arrived, departed, reason, *_)] = _stops(pg, 2)
    assert (arrived, departed, reason) == (_T0, left_at, "absent")
    # The one that stayed is untouched.
    assert _stops(pg, 1) == [(_T0, None, None, *_SPOT, 0)]
    # No trip was invented for the departure.
    assert _trips(pg, 2) == 0


def test_with_full_history_the_bounded_per_cycle_sweep_still_catches_it(pg):
    # Enough observed cycles before the departure that absence_window's lower
    # bound is active (production always has them), so this exercises the
    # bounded form the cycle actually runs.
    start = _T0 - (_K + _M + 2) * _EVERY
    left_at = _T0

    def fleet(t):
        return [_dev(1)] + ([_dev(2)] if t <= left_at else [])

    _run(pg, start, left_at + _T + (_K + 2) * _EVERY, fleet)
    [(arrived, departed, reason, *_)] = _stops(pg, 2)
    assert (arrived, departed, reason) == (start, left_at, "absent")


def test_the_bounded_sweep_leaves_old_ghosts_to_the_cleanup(pg):
    # A ghost from long before the sweep's window (the pre-sql/083 backlog)
    # is not the per-cycle sweep's job; close_ghost_stops closes it.
    _run(pg, _T0, _T0 + (_K + _M + 2) * _EVERY, lambda t: [_dev(1)])
    _insert_ghost(pg, 6, gone_since=_T0 - timedelta(days=9))
    last = _run(pg, _T0 + (_K + _M + 3) * _EVERY, _T0 + (_K + _M + 5) * _EVERY,
                lambda t: [_dev(1)])
    assert _stops(pg, 6)[0][1] is None
    assert ghost_stops.run(now=last)["closed"] == 1
    assert _stops(pg, 6)[0][1:3] == (_T0 - timedelta(days=9), "absent")


def test_the_first_cycle_after_an_outage_does_not_close_what_it_missed(pg):
    # Vehicle 2 is missing from the ONE cycle that runs after a long ingest
    # outage (a partial feed). It was seen in the last cycle before the
    # outage, so it has missed only one real cycle: not absent.
    last = _run(pg, _T0, _T0 + (_K + 1) * _EVERY, lambda t: [_dev(1), _dev(2)])
    back = last + _T + timedelta(hours=3)
    stats = _observe(pg, back, [_dev(1)])
    assert stats.stops_closed_absent == 0
    assert _stops(pg, 2)[0][1] is None
    # It shows up again next cycle: one continuous stop, never split.
    _observe(pg, back + _EVERY, [_dev(1), _dev(2)])
    assert [s[1] for s in _stops(pg, 2)] == [None]


def test_reserved_and_disabled_vehicles_in_the_feed_never_close_as_absent(pg):
    ride_start = _T0 + 2 * _EVERY
    horizon = _T0 + _T + (_K + 4) * _EVERY

    def fleet(t):
        return [
            _dev(1),
            # first seen mid-rental, reserved the whole time
            _dev(3, is_reserved=True),
            # disabled, sitting still, the whole time
            _dev(4, is_disabled=True),
            # parked, then one long rental that outlasts the threshold
            _dev(5, is_reserved=(t >= ride_start)),
        ]

    _run(pg, _T0, horizon, fleet)
    with pg.cursor() as cur:
        cur.execute("SELECT count(*) FROM device_history WHERE departure_reason = 'absent'")
        assert cur.fetchone()[0] == 0
    assert _stops(pg, 3) == [(_T0, None, None, *_SPOT, 0)]
    assert _stops(pg, 4) == [(_T0, None, None, *_SPOT, 0)]
    # The rental closed vehicle 5's stop when it started, as a move.
    assert _stops(pg, 5) == [(_T0, ride_start, "moved", *_SPOT, 0)]


def test_back_at_the_same_spot_is_a_new_stop_and_keeps_its_dwell_clock(pg):
    left_at = _T0 + 2 * _EVERY
    back_at = left_at + _T + (_K + 3) * _EVERY

    def fleet(t):
        return [_dev(1)] + (
            [_dev(2)] if t <= left_at else [_dev(2, _NUDGE)] if t >= back_at else [])

    _run(pg, _T0, back_at + _EVERY, fleet)
    assert _stops(pg, 2) == [
        (_T0, left_at, "absent", *_SPOT, 0),       # never reopened
        (back_at, None, None, *_NUDGE, 0),         # new stop from the return
    ]
    first_obs, last_obs, _ = _state(pg, 2)
    assert first_obs == _T0                        # dwell clock NOT reset
    assert last_obs == back_at + _EVERY
    assert _trips(pg, 2) == 0


def test_back_at_the_same_spot_with_a_rotated_bike_id_counts_it_on_the_new_stop(pg):
    left_at = _T0 + 2 * _EVERY
    back_at = left_at + _T + (_K + 3) * _EVERY

    def fleet(t):
        if t <= left_at:
            return [_dev(1), _dev(2)]
        if t >= back_at:
            return [_dev(1), _dev(2, device_id="bike-2-rotated")]
        return [_dev(1)]

    _run(pg, _T0, back_at, fleet)
    assert [s[5] for s in _stops(pg, 2)] == [0, 1]
    assert _state(pg, 2)[2] == 1


def test_back_elsewhere_is_one_move_and_the_absent_close_stands(pg):
    left_at = _T0 + 2 * _EVERY
    back_at = left_at + _T + (_K + 3) * _EVERY

    def fleet(t):
        return [_dev(1)] + (
            [_dev(2)] if t <= left_at else [_dev(2, _ELSEWHERE)] if t >= back_at else [])

    _run(pg, _T0, back_at, fleet)
    assert _stops(pg, 2) == [
        (_T0, left_at, "absent", *_SPOT, 0),       # not re-stamped to back_at
        (back_at, None, None, *_ELSEWHERE, 0),
    ]
    assert _state(pg, 2)[0] == back_at             # a real move resets dwell
    assert _trips(pg, 2) == 1                      # exactly as before sql/083


def test_a_short_gap_is_not_a_departure(pg):
    gone = (_T0 + 2 * _EVERY, _T0 + 2 * _EVERY + _T / 2)   # half the threshold

    def fleet(t):
        return [_dev(1)] + ([] if gone[0] < t < gone[1] else [_dev(2)])

    _run(pg, _T0, _T0 + _T + (_K + 6) * _EVERY, fleet)
    assert _stops(pg, 2) == [(_T0, None, None, *_SPOT, 0)]


def test_the_check_constraint_admits_only_the_two_reasons(pg):
    _observe(pg, _T0, [_dev(1)])
    with pg.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute("UPDATE device_history SET departure_reason = 'teleported'")
    pg.rollback()


# ---------------------------------------------------------------------------
# One-off cleanup and its dry run
# ---------------------------------------------------------------------------

def _insert_ghost(conn, n: int, *, gone_since: datetime) -> None:
    """A pre-sql/083 ghost: an open stop for a vehicle last seen at
    `gone_since`, written directly (the cycle that would have seen it ran
    before this change existed)."""
    arrived = gone_since - timedelta(days=1)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO device_state (vehicle_identifier, current_device_id, current_lat,
                   current_lon, current_spatial_status, first_observed_at_location,
                   first_ever_observed_at, last_observed_at)
               VALUES (%s, %s, %s, %s, 'denver_core', %s, %s, %s)""",
            (_vid(n), f"bike-{n}", *_SPOT, arrived, arrived, gone_since))
        cur.execute(
            """INSERT INTO device_history (vehicle_identifier, snapshot_time, lat, lon,
                   spatial_status, form_factor, device_id_observed)
               VALUES (%s, %s, %s, %s, 'denver_core', 'scooter', %s)""",
            (_vid(n), arrived, *_SPOT, f"bike-{n}"))
    conn.commit()


def _seed_ghosts(conn) -> datetime:
    """Vehicles 1 and 2 in the feed through the day's 6-9 AM window, then two
    ghosts, 6 and 7, that left the feed nine days earlier. Returns `now` for
    the cleanup."""
    end = _T0 + timedelta(hours=3)
    last = _run(conn, _T0 - _EVERY, end, lambda t: [_dev(1), _dev(2)])
    for n in (6, 7):
        _insert_ghost(conn, n, gone_since=_T0 - timedelta(days=9))
    return last + timedelta(minutes=1)


def _history(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, departed_at, departure_reason FROM device_history ORDER BY id")
        return cur.fetchall()


def test_cleanup_closes_ghosts_at_last_observed_and_is_idempotent(pg):
    now = _seed_ghosts(pg)
    first = ghost_stops.run(now=now)
    assert first["closed"] == 2
    assert first["matched"]["stops_denver_core"] == 2
    gone_since = _T0 - timedelta(days=9)
    for n in (6, 7):
        assert _stops(pg, n)[0][1:3] == (gone_since, "absent")
    for n in (1, 2):
        assert _stops(pg, n)[0][1] is None
    after_first = _history(pg)

    second = ghost_stops.run(now=now + timedelta(hours=1))
    assert second["closed"] == 0
    assert _history(pg) == after_first


def _in_equity_by_latitude(stops):
    return [equity_backfill.Stop(**{**s.__dict__, "in_equity": s.lat > 39.7}) for s in stops]


def test_dry_run_writes_nothing_on_a_read_only_session(pg, monkeypatch):
    now = _seed_ghosts(pg)
    before = _history(pg)

    ro = psycopg.connect(os.environ["VEO_TEST_PG_DSN"],
                         options="-c default_transaction_read_only=on")
    try:
        with ro.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            assert cur.fetchone()[0] == "on"
        ro.rollback()

        @contextmanager
        def _ro():
            yield ro

        for mod in (ghost_stops, equity_backfill):
            monkeypatch.setattr(mod, "connection", _ro)
        # The map lookup is DuckDB spatial against the real boundary file;
        # the fidelity arithmetic is what is under test here.
        monkeypatch.setattr(equity_backfill, "tag_equity_membership", _in_equity_by_latitude)

        result = ghost_stops.dry_run([_DAY], now=now)
        assert result["dry_run"] is True
        assert result["would_close"]["stops"] == 2
        [day] = result["days"]
        assert day["snapshots"] == 18                      # 12:00-15:00 UTC every 10 min
        assert day["before"]["fidelity_mean"] == 2.0       # 4 reconstructed / 2 recorded
        assert day["before"]["snapshots_passing_gate"] == 0
        assert day["after"]["fidelity_mean"] == 1.0
        assert day["after"]["snapshots_passing_gate"] == 18
        assert day["after"]["gated_percent_all_devices_equity"] == 100.0

        # The same session refuses the real run, so the dry run above really
        # did run somewhere a write would have failed.
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            ghost_stops.run(now=now)
        ro.rollback()
    finally:
        ro.close()

    assert _history(pg) == before
