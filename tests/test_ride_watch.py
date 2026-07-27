"""Tests for src/ride_watch.py: the pure watching/left_feed/resolved
classification (mirrors tests/test_device_state.py's style for testing
pure per-cycle logic) plus the DB orchestration around it against a fake
cursor."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from src import ride_watch
from src.ingest import TaggedDevice

_RIDE_A = uuid.uuid4()
_RIDE_B = uuid.uuid4()


def _device(vehicle_identifier: str, lat: float = 39.74, lon: float = -104.98,
            current_range_meters: int | None = 5000) -> TaggedDevice:
    return TaggedDevice(
        device_id="bike-1", vehicle_type_id="1", form_factor="scooter",
        lat=lat, lon=lon, spatial_status="denver_core",
        vehicle_identifier=vehicle_identifier,
        current_range_meters=current_range_meters,
    )


# ---------- _classify (pure) --------------------------------------------------

def test_watching_device_absent_from_feed_is_newly_left():
    watch_rows = [(1, _RIDE_A, "aaaa000000000000", "watching")]
    newly_left, newly_reappeared = ride_watch._classify(watch_rows, observed={})
    assert newly_left == [(1, _RIDE_A)]
    assert newly_reappeared == []


def test_watching_device_still_present_is_unchanged():
    dev = _device("aaaa000000000000")
    watch_rows = [(1, _RIDE_A, "aaaa000000000000", "watching")]
    newly_left, newly_reappeared = ride_watch._classify(watch_rows, observed={dev.vehicle_identifier: dev})
    assert newly_left == []
    assert newly_reappeared == []


def test_left_feed_device_reappearing_is_newly_reappeared():
    dev = _device("aaaa000000000000")
    watch_rows = [(1, _RIDE_A, "aaaa000000000000", "left_feed")]
    newly_left, newly_reappeared = ride_watch._classify(watch_rows, observed={dev.vehicle_identifier: dev})
    assert newly_left == []
    assert len(newly_reappeared) == 1
    assert newly_reappeared[0][:2] == (1, _RIDE_A)
    assert newly_reappeared[0][2] is dev


def test_left_feed_device_still_absent_is_unchanged():
    watch_rows = [(1, _RIDE_A, "aaaa000000000000", "left_feed")]
    newly_left, newly_reappeared = ride_watch._classify(watch_rows, observed={})
    assert newly_left == []
    assert newly_reappeared == []


def test_two_watchers_of_the_same_vehicle_flip_together():
    """The feed can't arbitrate physical possession — matching is purely
    per-vehicle_identifier, so two riders watching the same physical
    scooter both transition together."""
    watch_rows = [
        (1, _RIDE_A, "aaaa000000000000", "watching"),
        (2, _RIDE_B, "aaaa000000000000", "watching"),
    ]
    newly_left, newly_reappeared = ride_watch._classify(watch_rows, observed={})
    assert {w for w, _ in newly_left} == {1, 2}


def test_mixed_batch_partitions_correctly():
    dev_b = _device("bbbb000000000000")
    watch_rows = [
        (1, _RIDE_A, "aaaa000000000000", "watching"),   # absent -> left
        (2, _RIDE_B, "bbbb000000000000", "left_feed"),  # present -> reappeared
    ]
    newly_left, newly_reappeared = ride_watch._classify(
        watch_rows, observed={dev_b.vehicle_identifier: dev_b}
    )
    assert newly_left == [(1, _RIDE_A)]
    assert [t[:2] for t in newly_reappeared] == [(2, _RIDE_B)]


# ---------- update_watches_for_cycle (DB orchestration, fake cursor) ---------

class _FakeCursor:
    def __init__(self, watch_rows):
        self._watch_rows = watch_rows
        self.executed: list[tuple[str, tuple]] = []
        self.executemany_calls: list[tuple[str, list]] = []

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))

    def executemany(self, sql, seq_of_params):
        self.executemany_calls.append((" ".join(sql.split()), list(seq_of_params)))

    def fetchall(self):
        return self._watch_rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, watch_rows):
        self.cur = _FakeCursor(watch_rows)
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True


def _run(monkeypatch, watch_rows, devices):
    conn = _FakeConn(watch_rows)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(ride_watch, "connection", _fake_connection)
    stats = ride_watch.update_watches_for_cycle(
        uuid.uuid4(), datetime.now(timezone.utc), devices
    )
    return stats, conn


def test_no_open_watches_short_circuits(monkeypatch):
    stats, conn = _run(monkeypatch, watch_rows=[], devices=[])
    assert stats.open_watches == 0
    assert len(conn.cur.executed) == 1  # only the initial SELECT ran
    assert not conn.committed  # returned before reaching conn.commit()


def test_newly_left_updates_both_tables(monkeypatch):
    stats, conn = _run(
        monkeypatch,
        watch_rows=[(1, _RIDE_A, "aaaa000000000000", "watching")],
        devices=[],
    )
    assert stats.newly_left_feed == 1
    sqls = [sql for sql, _ in conn.cur.executed]
    assert any("UPDATE user_device_watch_list SET status = 'left_feed'" in s for s in sqls)
    assert any("UPDATE tracked_rides SET" in s and "status = 'left_feed'" in s for s in sqls)
    assert conn.committed


def test_newly_reappeared_writes_gbfs_fields_via_executemany(monkeypatch):
    dev = _device("aaaa000000000000", lat=39.7, lon=-105.0, current_range_meters=8000)
    stats, conn = _run(
        monkeypatch,
        watch_rows=[(1, _RIDE_A, "aaaa000000000000", "left_feed")],
        devices=[dev],
    )
    assert stats.newly_reappeared == 1
    assert len(conn.cur.executemany_calls) == 1
    sql, rows = conn.cur.executemany_calls[0]
    assert "gbfs_reappeared_at" in sql
    assert len(rows) == 1
    # (snapshot_time, cycle_id, lat, lon, battery_pct, tracked_ride_id)
    assert rows[0][2] == 39.7
    assert rows[0][3] == -105.0
    assert rows[0][5] == str(_RIDE_A)


def test_unresolved_watches_get_last_checked_cycle_bumped(monkeypatch):
    """A 'watching' watch whose vehicle is still present each cycle isn't
    a transition at all, but last_checked_cycle_id should still advance
    so an operator inspecting the row can tell it's being actively polled
    rather than stuck."""
    dev = _device("cccc000000000000")
    stats, conn = _run(
        monkeypatch,
        watch_rows=[(2, _RIDE_B, "cccc000000000000", "watching")],
        devices=[dev],
    )
    assert stats.newly_left_feed == 0
    assert stats.newly_reappeared == 0
    sqls = [sql for sql, _ in conn.cur.executed]
    assert any("last_checked_cycle_id = %s WHERE id = ANY" in s for s in sqls)
    assert conn.committed
