"""sql/083: a stop also ends when its vehicle leaves the feed.

Unit coverage against a fake cursor (same thin shape as
tests/test_device_state_rentals.py): the absence rule's window arithmetic,
where the sweep sits in the cycle, and what a vehicle coming back does.
tests/test_ghost_stops_pg.py runs the same behaviour against real Postgres.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from src import device_state
from src.ingest import TaggedDevice

_T0 = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
_VID = "b1b1b1b1b1b1b1b1"
_SPOT = (39.725550, -104.980850)
_NUDGE = (39.725558, -104.980858)      # ~1 m: inside the stationary threshold
_ELSEWHERE = (39.729218, -105.027692)  # ~4 km away
_T = device_state.ABSENT_STOP_AFTER
_K = device_state.ABSENT_MIN_MISSED_CYCLES
_M = device_state.ABSENT_SWEEP_WINDOW_CYCLES


def _device(lat_lon=_SPOT, *, is_reserved=None, device_id="bike-1",
            vehicle_identifier=_VID) -> TaggedDevice:
    lat, lon = lat_lon
    return TaggedDevice(
        device_id=device_id, vehicle_type_id="1", form_factor="scooter",
        lat=lat, lon=lon, spatial_status="denver_core",
        vehicle_identifier=vehicle_identifier, vehicle_plate="1234567",
        current_range_meters=20000, is_reserved=is_reserved,
    )


def _cycles(n: int, *, every=timedelta(minutes=2), latest=_T0) -> list[datetime]:
    """n observed-cycle times, newest first, the newest at `latest`."""
    return [latest - i * every for i in range(n)]


class _FakeCursor:
    rowcount = 0

    def __init__(self, *, state=None, open_stop=True, snapshot_times=(), baseline=None):
        self.state = state
        # (rows, median) answered to record_processed_cycle's baseline read.
        self.baseline = baseline
        self.open_stop = open_stop
        self.snapshot_times = list(snapshot_times)
        self.calls: list[tuple[str, list]] = []
        self._last = ""

    def execute(self, sql, params=()):
        self._last = " ".join(sql.split())
        self.calls.append((self._last, [params]))

    def executemany(self, sql, seq):
        self._last = " ".join(sql.split())
        self.calls.append((self._last, list(seq)))

    def fetchall(self):
        if "FROM device_state_processed_cycles" in self._last:
            return [(t,) for t in self.snapshot_times]
        if self._last.startswith("SELECT DISTINCT vehicle_identifier FROM device_history"):
            return [(_VID,)] if self.open_stop else []
        if "FROM device_state" in self._last and self.state is not None:
            s = self.state
            return [(_VID, s["device_id"], s["lat"], s["lon"],
                     s["first_observed_at_location"], s["number_failed_starts"],
                     s["first_ever_observed_at"], s["rental_started_at"],
                     s["last_observed_at"], s.get("rental_max_distance_m"),
                     s.get("rental_origin_device_id"), s.get("last_fix_lat"),
                     s.get("last_fix_lon"), s.get("last_range_meters"),
                     s.get("range_low_since_full"),
                     None, None, None, None, None)]  # sql/107 low point + settled
        return []

    def fetchone(self):
        if "percentile_cont" in self._last:
            return self.baseline if self.baseline is not None else (0, None)
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    # -- assertions ---------------------------------------------------------
    def rows_for(self, needle: str) -> list:
        return [p for sql, params in self.calls if needle in sql for p in params]

    def index_of(self, needle: str) -> int:
        return next(i for i, (sql, _) in enumerate(self.calls) if needle in sql)

    def ran(self, needle: str) -> bool:
        return any(needle in sql for sql, _ in self.calls)


class _FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def commit(self):
        pass


@pytest.fixture
def cycle(monkeypatch):
    monkeypatch.setattr(device_state.device_features, "seed_catalog_features",
                        lambda cur: None)

    def run(devices, *, at=_T0, **cursor_kw):
        cur = _FakeCursor(**cursor_kw)

        @contextmanager
        def _fake_connection():
            yield _FakeConn(cur)

        monkeypatch.setattr(device_state, "connection", _fake_connection)
        stats = device_state.update_for_cycle(uuid.uuid4(), at, devices)
        return stats, cur

    return run


def _state(lat_lon=_SPOT, *, last_seen, device_id="bike-1", rental_started_at=None) -> dict:
    lat, lon = lat_lon
    return {
        "device_id": device_id, "lat": lat, "lon": lon,
        "first_observed_at_location": _T0 - timedelta(days=4),
        "number_failed_starts": 0,
        "first_ever_observed_at": _T0 - timedelta(days=60),
        "rental_started_at": rental_started_at,
        "last_observed_at": last_seen,
    }


# ---------------------------------------------------------------------------
# The rule's window
# ---------------------------------------------------------------------------

def test_cutoff_is_threshold_before_now_when_cycles_are_regular():
    cur = _FakeCursor(snapshot_times=_cycles(_K + _M))
    w = device_state.absence_window(cur, _T0, bounded=False)
    assert w.cutoff == _T0 - _T
    assert w.since is None


def test_too_few_observed_cycles_judges_nothing_absent():
    cur = _FakeCursor(snapshot_times=_cycles(_K - 1))
    assert device_state.absence_window(cur, _T0, bounded=True) is None
    assert device_state.close_absent_stops(cur, None) == 0
    assert device_state.absent_stop_candidates(cur, None) == []


def test_after_an_ingest_outage_our_downtime_is_not_the_vehicles_absence():
    # One cycle back after 5 h of no cycles: every vehicle's last sighting is
    # >= 5 h old by the wall clock, but most were only missed by ONE real
    # cycle. The cutoff drops to before the outage, so only vehicles that had
    # already missed the K-1 cycles preceding it (and this one) qualify.
    before_outage = _cycles(_K + _M - 1, latest=_T0 - timedelta(hours=5))
    cur = _FakeCursor(snapshot_times=[_T0] + before_outage)
    w = device_state.absence_window(cur, _T0, bounded=False)
    assert w.cutoff == before_outage[_K - 2]
    assert w.cutoff < _T0 - timedelta(hours=5)


def test_bounded_window_starts_where_the_sweep_m_cycles_ago_stopped():
    times = _cycles(_K + _M)
    cur = _FakeCursor(snapshot_times=times)
    w = device_state.absence_window(cur, _T0, bounded=True)
    assert w.cutoff == _T0 - _T
    assert w.since == min(times[_M] - _T, times[_M + _K - 1])
    # About an hour of cycles: a band, not the whole history.
    assert timedelta(minutes=55) <= w.cutoff - w.since <= timedelta(minutes=65)


def test_bounded_window_is_unbounded_until_enough_history_exists():
    cur = _FakeCursor(snapshot_times=_cycles(_K + _M - 1))
    w = device_state.absence_window(cur, _T0, bounded=True)
    assert w.since is None


def test_the_close_locks_its_vehicles_and_skips_ones_a_cycle_holds():
    cur = _FakeCursor()
    w = device_state.AbsenceWindow(cutoff=_T0 - _T, since=_T0 - 2 * _T)
    device_state.close_absent_stops(cur, w)
    sql, params = cur.calls[-1]
    assert sql.startswith("UPDATE device_history h SET departed_at = GREATEST(s.last_observed_at")
    assert "departure_reason = 'absent'" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "last_observed_at >= %(since)s" in sql
    assert params == [{"cutoff": w.cutoff, "since": w.since}]


def test_the_dry_run_query_takes_no_locks():
    cur = _FakeCursor()
    device_state.absent_stop_candidates(cur, device_state.AbsenceWindow(cutoff=_T0))
    sql, _ = cur.calls[-1]
    assert sql.startswith("SELECT")
    assert "FOR UPDATE" not in sql
    assert "since" not in sql


# ---------------------------------------------------------------------------
# The sweep inside the cycle
# ---------------------------------------------------------------------------

def test_every_cycle_sweeps_last_after_its_own_history_writes(cycle):
    stats, cur = cycle([_device(_ELSEWHERE)],
                       state=_state(last_seen=_T0 - timedelta(minutes=2)),
                       snapshot_times=_cycles(_K + _M))
    sweep = cur.index_of("departure_reason = 'absent'")
    assert sweep > cur.index_of("INSERT INTO device_history")
    assert sweep > cur.index_of("UPDATE device_history SET departed_at = %s, departure_reason = 'moved'")
    assert sweep > cur.index_of("UPDATE device_state SET vehicle_plate")
    # It is the bounded, per-cycle form.
    assert "last_observed_at >= %(since)s" in cur.calls[sweep][0]


def test_a_feed_with_nothing_usable_in_it_still_sweeps_but_is_not_an_observation(cycle):
    """A fresh payload carrying no usable identifier (a feed that stopped
    sending plates, or the whole fleet withdrawn) is recorded as processed
    but NOT as an observed cycle (sql/086), so it can never be one of the
    missed cycles that closes a stop. The bounded sweep still runs, for the
    vehicles that already qualified from real cycles."""
    stats, cur = cycle([_device(vehicle_identifier=None)],
                       snapshot_times=_cycles(_K + _M), baseline=(720, 7500))
    assert stats.skipped_no_identifier == 1
    assert stats.counted_as_observation is False
    [row] = cur.rows_for("INSERT INTO device_state_processed_cycles")
    assert row[2:] == (0, 7500, False)
    sweep = cur.index_of("departure_reason = 'absent'")
    assert sweep > cur.index_of("INSERT INTO device_state_processed_cycles")
    # The bounded, per-cycle form, same as any other cycle's sweep.
    assert "last_observed_at >= %(since)s" in cur.calls[sweep][0]


def test_an_empty_feed_is_not_an_observation_even_without_a_baseline(cycle):
    """Same for a payload with no devices at all; zero never counts, even
    just after sql/086 when there is no baseline yet."""
    stats, cur = cycle([], snapshot_times=_cycles(_K + _M))
    assert stats.skipped_no_identifier == 0
    assert stats.counted_as_observation is False
    [row] = cur.rows_for("INSERT INTO device_state_processed_cycles")
    assert row[2:] == (0, None, False)
    assert cur.ran("departure_reason = 'absent'")


# ---------------------------------------------------------------------------
# sql/086: the processed-cycle ledger
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("eligible, baseline, counts", [
    (0, None, False),        # zero never counts, with or without a baseline
    (0, 7500, False),
    (1, None, True),         # no baseline yet (fresh ledger): any non-zero counts
    (7500, 7500, True),
    (5868, 7500, True),      # 0.78: the lowest real cycle since 2026-05-30
    (3750, 7500, True),      # exactly at the floor
    (3749, 7500, False),     # just below it
    (40, 7500, False),       # near-empty
])
def test_what_counts_as_an_observation(eligible, baseline, counts):
    assert device_state.counts_as_observation(eligible, baseline) is counts


def test_the_absence_rule_counts_processed_cycles_not_core_snapshots():
    cur = _FakeCursor(snapshot_times=_cycles(_K + _M))
    device_state.absence_window(cur, _T0, bounded=True)
    sql, params = cur.calls[-1]
    assert "FROM device_state_processed_cycles" in sql
    assert "WHERE counts_as_observation" in sql
    assert "snapshot_metadata_core" not in sql
    assert params == [(_T0, _K + _M)]


def test_a_normal_cycle_records_itself_in_its_own_transaction_before_the_sweep(cycle):
    stats, cur = cycle([_device(_ELSEWHERE)],
                       state=_state(last_seen=_T0 - timedelta(minutes=2)),
                       snapshot_times=_cycles(_K + _M), baseline=(720, 1))
    assert stats.counted_as_observation is True
    ledger = cur.index_of("INSERT INTO device_state_processed_cycles")
    # After this cycle's own observations, before the window is read.
    assert ledger > cur.index_of("UPDATE device_state SET vehicle_plate")
    assert ledger > cur.index_of("INSERT INTO device_history")
    assert ledger < cur.index_of("FROM device_state_processed_cycles WHERE counts_as_observation")
    assert ledger < cur.index_of("departure_reason = 'absent'")
    [row] = cur.rows_for("INSERT INTO device_state_processed_cycles")
    assert row[1:] == (_T0, 1, 1, True)


def test_the_baseline_ignores_a_short_ledger():
    cur = _FakeCursor(baseline=(device_state.ABSENT_BASELINE_MIN_CYCLES - 1, 7500))
    assert device_state.record_processed_cycle(cur, uuid.uuid4(), _T0, 10) is True
    [row] = cur.rows_for("INSERT INTO device_state_processed_cycles")
    assert row[3] is None


def test_the_ledger_trim_keeps_the_newest_rows_whatever_their_age():
    cur = _FakeCursor(baseline=(720, 7500))
    device_state.record_processed_cycle(cur, uuid.uuid4(), _T0, 7500)
    sql, [params] = cur.calls[-1]
    assert sql.startswith("DELETE FROM device_state_processed_cycles")
    assert "OFFSET %(keep)s LIMIT 1" in sql
    assert params == {"before": _T0 - device_state.ABSENT_LEDGER_RETENTION,
                      "keep": device_state.ABSENT_LEDGER_KEEP_MIN - 1}
    # Enough rows are always kept for the rule's widest read.
    assert device_state.ABSENT_LEDGER_KEEP_MIN > max(
        device_state.ABSENT_BASELINE_CYCLES, _K + _M)


def test_a_move_records_moved(cycle):
    _, cur = cycle([_device(_ELSEWHERE)], state=_state(last_seen=_T0 - timedelta(minutes=2)))
    assert cur.ran("UPDATE device_history SET departed_at = %s, departure_reason = 'moved'")


def test_a_recently_seen_fleet_costs_no_open_stop_probe(cycle):
    _, cur = cycle([_device(_SPOT)], state=_state(last_seen=_T0 - timedelta(minutes=2)))
    assert not cur.ran("SELECT DISTINCT vehicle_identifier FROM device_history")


# ---------------------------------------------------------------------------
# Reappearance after the stop was closed as absent
# ---------------------------------------------------------------------------

def test_back_at_the_same_spot_opens_a_new_stop_and_keeps_the_dwell_clock(cycle):
    stats, cur = cycle([_device(_NUDGE)], state=_state(last_seen=_T0 - 3 * _T),
                       open_stop=False)
    assert cur.ran("SELECT DISTINCT vehicle_identifier FROM device_history")
    assert stats.stationary == 1 and stats.stops_reopened == 1
    [row] = cur.rows_for("INSERT INTO device_history")
    assert row[0] == _VID and row[3] == _T0          # arrives now, not back-dated
    assert (row[4], row[5]) == _NUDGE                 # where it is seen now
    assert row[9] == 0                                # no failed start
    # device_state: liveness only. first_observed_at_location (the dwell
    # clock the reliability tier reads) is NOT reset.
    assert not cur.ran("first_observed_at_location = %s")
    assert cur.rows_for("UPDATE device_state SET current_spatial_status = %s, last_observed_at")
    # No trip: nothing says it was ridden.
    assert not cur.ran("INSERT INTO trip_events")


def test_back_at_the_same_spot_with_a_new_bike_id_counts_the_failed_start_on_the_new_stop(cycle):
    stats, cur = cycle([_device(_SPOT, device_id="bike-2")],
                       state=_state(last_seen=_T0 - 3 * _T), open_stop=False)
    assert stats.failed_starts == 1 and stats.stops_reopened == 1
    [row] = cur.rows_for("INSERT INTO device_history")
    assert row[8] == "bike-2" and row[9] == 1


def test_back_elsewhere_is_one_ordinary_move_not_two_stops(cycle):
    stats, cur = cycle([_device(_ELSEWHERE)], state=_state(last_seen=_T0 - 3 * _T),
                       open_stop=False)
    assert stats.moved == 1 and stats.stops_reopened == 0
    assert len(cur.rows_for("INSERT INTO device_history")) == 1
    assert len(cur.rows_for("INSERT INTO trip_events")) == 1


def test_long_gap_but_stop_still_open_is_just_stationary(cycle):
    # Missed by the sweep (e.g. before the cleanup ran): nothing to reopen.
    stats, cur = cycle([_device(_SPOT)], state=_state(last_seen=_T0 - 3 * _T),
                       open_stop=True)
    assert stats.stationary == 1 and stats.stops_reopened == 0
    assert not cur.ran("INSERT INTO device_history")


def test_back_while_reserved_opens_nothing_until_the_rental_ends(cycle):
    stats, cur = cycle([_device(_SPOT, is_reserved=True)],
                       state=_state(last_seen=_T0 - 3 * _T), open_stop=False)
    assert stats.rentals_started == 1 and stats.stops_reopened == 0
    assert not cur.ran("INSERT INTO device_history")


def test_released_after_vanishing_mid_rental_is_a_single_moved_row(cycle):
    stats, cur = cycle([_device(_SPOT, is_reserved=False)],
                       state=_state(last_seen=_T0 - 3 * _T, rental_started_at=_T0 - 4 * _T),
                       open_stop=False)
    assert stats.rentals_ended == 1 and stats.stops_reopened == 0
    assert len(cur.rows_for("INSERT INTO device_history")) == 1
