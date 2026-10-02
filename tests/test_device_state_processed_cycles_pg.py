"""sql/086: the absence rule counts the cycles device_state processed.

Against real Postgres, the two failure cases PR #98's review left open:

  1. ingest healthy, device_state failing: src/cycle.py commits the core
     snapshot and swallows the updater's failure, so snapshot_metadata_core
     keeps filling while last_observed_at goes stale. Those cycles must
     never count as misses, and a vehicle that was in them must never close.
  2. an empty, plate-less or near-empty payload: processed, but never one of
     the missed cycles, so it cannot close a stop on its own. It still
     sweeps vehicles that had already qualified from real cycles.

Plus: ordinary absence still closes after > ABSENT_STOP_AFTER and
ABSENT_MIN_MISSED_CYCLES processed misses, the ledger's retention trim, and
replaying sql/086.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
Shares tests/test_ghost_stops_pg.py's fixture, which wipes device_history /
device_state / trip_events / snapshot_metadata_core / the ledger at setup.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import timedelta

import pytest

pytest.importorskip("psycopg")

from src import device_state, ghost_stops  # noqa: E402
from tests.test_ghost_stops_pg import (  # noqa: E402,F401  (pg is a fixture)
    SQL_DIR, _EVERY, _K, _SPOT, _T, _T0, _dev, _observe, _run, _stops, _vid, pg,
)

_FLEET = 10   # vehicles 1.._FLEET; small, but the floor is a ratio


def _fleet(*, without=()):
    return [_dev(n) for n in range(1, _FLEET + 1) if n not in without]


def _updater_fails(conn, t) -> None:
    """A cycle whose ingest succeeded and whose device_state update failed:
    exactly what src/cycle.py leaves behind (core snapshot committed, the
    updater's exception logged and swallowed, nothing of its own committed)."""
    cid = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO observation_cycles (cycle_id, start_ts, job_status) "
            "VALUES (%s, %s, 'complete')", (str(cid), t))
        cur.execute(
            "INSERT INTO snapshot_metadata_core (cycle_id, snapshot_time, total_devices_denver) "
            "VALUES (%s, %s, %s)", (str(cid), t, _FLEET))
    conn.commit()


def _ledger(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT snapshot_time, eligible_count, counts_as_observation "
            "FROM device_state_processed_cycles ORDER BY snapshot_time")
        return cur.fetchall()


@pytest.fixture()
def short_baseline(monkeypatch):
    # Production needs 30 processed cycles before a baseline exists; at the
    # tests' 10-minute cadence that is 5 h of cycles per test. The rule is the
    # same with 5.
    monkeypatch.setattr(device_state, "ABSENT_BASELINE_MIN_CYCLES", 5)


# ---------------------------------------------------------------------------
# 1. Updater-only failures
# ---------------------------------------------------------------------------

def test_updater_failures_are_not_processed_cycles(pg):
    last = _run(pg, _T0, _T0 + (_K + 2) * _EVERY, lambda t: _fleet())
    before = _ledger(pg)
    t = last
    for _ in range(12):           # two hours of updater-only failure
        t += _EVERY
        _updater_fails(pg, t)
    assert _ledger(pg) == before  # snapshot rows, but no processed cycles
    with pg.cursor() as cur:
        w = device_state.absence_window(cur, t, bounded=True)
    # The cutoff stays at the processed cycles: the k-th newest of them, not
    # `t - ABSENT_STOP_AFTER`, which the failed cycles would have allowed.
    assert w.cutoff == before[-_K][0]
    assert w.cutoff < last


def test_a_vehicle_present_through_an_updater_outage_is_never_closed(pg):
    # Every vehicle is in every feed. device_state fails for 2 h, long enough
    # that before sql/086 the first cycle back closed (and backdated) every
    # stop to the outage start. Vehicle 3 alone is absent on the way back in.
    last = _run(pg, _T0, _T0 + (_K + 2) * _EVERY, lambda t: _fleet())
    t = last
    for _ in range(12):
        t += _EVERY
        _updater_fails(pg, t)
    stats = _observe(pg, t + _EVERY, _fleet(without={3}))
    assert stats.stops_closed_absent == 0
    for n in range(1, _FLEET + 1):
        assert [s[1] for s in _stops(pg, n)] == [None], n
    # Nor does the CLI backstop, run now.
    assert ghost_stops.run(now=t + _EVERY)["closed"] == 0
    # The snapshot-table count of missed cycles would have said otherwise:
    # vehicle 3 is > 1 h and > k core snapshots stale.
    assert last < t + _EVERY - _T


def test_absence_after_an_updater_outage_needs_k_processed_misses(pg):
    # Vehicle 3 left at the outage start. It closes only after it has missed
    # k PROCESSED cycles, and then at its real last-observed time.
    last = _run(pg, _T0, _T0 + (_K + 2) * _EVERY, lambda t: _fleet())
    t = last
    for _ in range(12):
        t += _EVERY
        _updater_fails(pg, t)
    for i in range(1, _K):        # k-1 processed misses: still open
        _observe(pg, t + i * _EVERY, _fleet(without={3}))
        assert _stops(pg, 3)[0][1] is None, i
    _observe(pg, t + _K * _EVERY, _fleet(without={3}))
    [(arrived, departed, reason, *_)] = _stops(pg, 3)
    assert (arrived, departed, reason) == (_T0, last, "absent")
    assert all(_stops(pg, n)[0][1] is None for n in range(1, _FLEET + 1) if n != 3)


# ---------------------------------------------------------------------------
# 2. Empty / plate-less / near-empty payloads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    pytest.param(lambda: [], id="empty"),
    pytest.param(lambda: [dataclasses.replace(d, vehicle_identifier=None)
                          for d in _fleet()], id="plate-less"),
    pytest.param(lambda: [_dev(1)], id="near-empty"),
])
def test_a_glitched_payload_cannot_close_stops_on_its_own(pg, short_baseline, payload):
    # More real cycles before the glitch than glitch cycles, so the glitch is
    # not yet the baseline's median (in production: the glitch has lasted
    # under ~12 h of the 24 h baseline).
    last = _run(pg, _T0, _T0 + 25 * _EVERY, lambda t: _fleet())
    t = last
    closed = 0
    for _ in range(12):           # two hours of the glitch: > 1 h and > k cycles
        t += _EVERY
        closed += _observe(pg, t, payload()).stops_closed_absent
    assert closed == 0
    for n in range(1, _FLEET + 1):
        assert [s[1] for s in _stops(pg, n)] == [None], n
    assert [c for _, _, c in _ledger(pg)[-12:]] == [False] * 12
    # Recovery: the full fleet is back, every stop continues unbroken.
    _observe(pg, t + _EVERY, _fleet())
    for n in range(1, _FLEET + 1):
        assert [s[1] for s in _stops(pg, n)] == [None], n


def test_a_glitch_still_sweeps_vehicles_that_qualified_from_real_cycles(pg, short_baseline):
    # Vehicle 3 left well before the glitch and had already missed k real
    # cycles; it is closed during the glitch once an hour has passed, at its
    # last observed time. Vehicle 4 left only two real cycles before the
    # glitch: the glitch cannot supply its missing misses, so it stays open
    # until k real cycles have passed after recovery.
    start_glitch = _T0 + (_K + 6) * _EVERY
    # Missing from the k real cycles before the glitch; its hour is up during it.
    left3 = start_glitch - (_K + 1) * _EVERY
    # Missing from only the 2 real cycles before the glitch.
    left4 = start_glitch - 3 * _EVERY

    def fleet(t):
        out = {3} if t > left3 else set()
        if t > left4:
            out.add(4)
        return _fleet(without=out)

    last = _run(pg, _T0, start_glitch - _EVERY, fleet)
    assert _stops(pg, 3)[0][1] is None   # k misses, but not yet an hour
    t = last
    closed_during_glitch = 0
    for _ in range(9):            # 90 minutes of empty payloads
        t += _EVERY
        closed_during_glitch += _observe(pg, t, []).stops_closed_absent
    assert closed_during_glitch == 1
    assert _stops(pg, 3)[0][1:3] == (left3, "absent")
    assert _stops(pg, 4)[0][1] is None
    # Back to real cycles: k-2 more misses are needed (it missed 2 before).
    for i in range(1, _K - 2):
        _observe(pg, t + i * _EVERY, fleet(t + i * _EVERY))
        assert _stops(pg, 4)[0][1] is None, i
    _observe(pg, t + (_K - 2) * _EVERY, fleet(t + (_K - 2) * _EVERY))
    assert _stops(pg, 4)[0][1:3] == (left4, "absent")


def test_ordinary_absence_still_closes_after_the_threshold_and_k_misses(pg, short_baseline):
    # One vehicle of ten leaving is 90% of the baseline: a real observation.
    left = _T0 + (_K + 2) * _EVERY

    def fleet(t):
        return _fleet(without={7} if t > left else ())

    _run(pg, _T0, left + _T - _EVERY, fleet)
    assert _stops(pg, 7)[0][1] is None
    assert all(c for _, _, c in _ledger(pg))
    _run(pg, left + _T, left + _T + _EVERY, fleet)
    assert _stops(pg, 7)[0][1:3] == (left, "absent")


def test_a_real_change_in_fleet_size_is_followed_by_the_baseline(pg, short_baseline, monkeypatch):
    # The baseline includes cycles that did not count, so a fleet that really
    # shrinks (and stays shrunk) counts again once that is the median of the
    # baseline window. Production: ABSENT_BASELINE_CYCLES = 720, ~12 h in.
    #
    # BELOW half, not halved. 4 of 10 is 0.40, and it has to be under the floor
    # for the baseline to have anything to adapt to: ABSENT_FLOOR_RATIO is
    # compared with `>=`, so an exact halving counts on the very first cycle and
    # never reaches this path at all. `test_what_counts_as_an_observation` pins
    # both sides of that boundary (3750/7500 counts, 3749 does not).
    monkeypatch.setattr(device_state, "ABSENT_BASELINE_CYCLES", 10)
    last = _run(pg, _T0, _T0 + 10 * _EVERY, lambda t: _fleet())
    counted = [_observe(pg, last + i * _EVERY, _fleet(without={1, 2, 3, 4, 5, 6})
                        ).counted_as_observation for i in range(1, 12)]
    assert counted[0] is False
    assert counted[-1] is True
    assert counted.index(True) <= 6


# ---------------------------------------------------------------------------
# Ledger retention and migration
# ---------------------------------------------------------------------------

def test_the_ledger_trims_old_rows_but_keeps_the_newest(pg, monkeypatch):
    monkeypatch.setattr(device_state, "ABSENT_LEDGER_KEEP_MIN", 4)
    ret = device_state.ABSENT_LEDGER_RETENTION
    with pg.cursor() as cur:
        for i in range(6):        # six rows, all older than the retention
            cur.execute(
                "INSERT INTO device_state_processed_cycles "
                "(cycle_id, snapshot_time, eligible_count, counts_as_observation) "
                "VALUES (%s, %s, 10, true)",
                (str(uuid.uuid4()), _T0 - ret - timedelta(days=1) + i * _EVERY))
    pg.commit()
    _observe(pg, _T0, _fleet())
    rows = _ledger(pg)
    # The new row plus the 3 newest old ones: KEEP_MIN rows, whatever their age.
    assert len(rows) == 4
    assert rows[-1][0] == _T0
    assert rows[0][0] == _T0 - ret - timedelta(days=1) + 3 * _EVERY
    # Once enough recent rows exist, every row past the retention goes.
    _run(pg, _T0 + _EVERY, _T0 + 4 * _EVERY, lambda t: _fleet())
    assert all(r[0] >= _T0 - ret for r in _ledger(pg))


def test_the_ledger_is_not_trimmed_below_keep_min_after_a_long_outage(pg):
    # Fewer rows than KEEP_MIN: nothing goes, however old, so the first cycle
    # back from a week-long updater outage still sees the cycles before it.
    old = _T0 - device_state.ABSENT_LEDGER_RETENTION - timedelta(days=3)
    _run(pg, old, old + (_K + 1) * _EVERY, lambda t: _fleet())
    n_before = len(_ledger(pg))
    _observe(pg, _T0, _fleet(without={3}))
    assert len(_ledger(pg)) == n_before + 1
    assert _stops(pg, 3)[0][1] is None


def test_sql_086_replays_and_takes_no_foreign_key(pg):
    path = SQL_DIR / "086_device_state_processed_cycles.sql"
    with pg.cursor() as cur:
        cur.execute(
            "INSERT INTO device_state_processed_cycles "
            "(cycle_id, snapshot_time, eligible_count, counts_as_observation) "
            "VALUES (%s, %s, 1, true)", (str(uuid.uuid4()), _T0))
        cur.execute(path.read_text())
        cur.execute(path.read_text())
        cur.execute("SELECT count(*) FROM device_state_processed_cycles")
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'device_state_processed_cycles' ORDER BY 1")
        assert {r[0] for r in cur.fetchall()} >= {
            "idx_dspc_snapshot_time", "idx_dspc_observation_time"}
        cur.execute(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'device_state_processed_cycles'::regclass AND contype = 'f'")
        assert cur.fetchone()[0] == 0
        cur.execute("SHOW lock_timeout")
        assert cur.fetchone()[0] == "0"   # RESET for the files after it
    pg.commit()
