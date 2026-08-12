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
            current_range_meters: int | None = 5000,
            is_reserved: bool | None = None,
            is_disabled: bool | None = None) -> TaggedDevice:
    """is_reserved defaults to None — "upstream said nothing", which reads
    as available, i.e. the presence-only behaviour every pre-existing test
    in this file was written against."""
    return TaggedDevice(
        device_id="bike-1", vehicle_type_id="1", form_factor="scooter",
        lat=lat, lon=lon, spatial_status="denver_core",
        vehicle_identifier=vehicle_identifier,
        current_range_meters=current_range_meters,
        is_reserved=is_reserved, is_disabled=is_disabled,
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


# ---------- the reservation signal (regression: 0/19 rides ever resolved) ----
#
# See src/ride_watch.py's "WHAT CHECKED OUT ACTUALLY LOOKS LIKE": Veo keeps
# a rented vehicle IN free_bike_status, moving, with is_reserved true, so
# presence-only detection never fired for a single real ride.

def test_watching_device_present_but_reserved_is_newly_left():
    """The regression these tests exist for. Before 2026-08-10 this was
    "unchanged" and the watch sat open until it expired."""
    dev = _device("aaaa000000000000", is_reserved=True)
    watch_rows = [(1, _RIDE_A, "aaaa000000000000", "watching")]
    newly_left, newly_reappeared = ride_watch._classify(
        watch_rows, observed={dev.vehicle_identifier: dev})
    assert newly_left == [(1, _RIDE_A)]
    assert newly_reappeared == []


def test_left_feed_device_present_but_still_reserved_is_unchanged():
    """Mid-rental: listed, moving, still reserved. Not an end signal —
    resolving here would stamp gbfs_end_* somewhere along the route."""
    dev = _device("aaaa000000000000", is_reserved=True)
    watch_rows = [(1, _RIDE_A, "aaaa000000000000", "left_feed")]
    newly_left, newly_reappeared = ride_watch._classify(
        watch_rows, observed={dev.vehicle_identifier: dev})
    assert newly_left == []
    assert newly_reappeared == []


def test_left_feed_device_unreserved_again_is_newly_reappeared():
    """The end of the arc: is_reserved back to false at the new kerb."""
    dev = _device("aaaa000000000000", lat=39.7402, lon=-104.986, is_reserved=False)
    watch_rows = [(1, _RIDE_A, "aaaa000000000000", "left_feed")]
    newly_left, newly_reappeared = ride_watch._classify(
        watch_rows, observed={dev.vehicle_identifier: dev})
    assert newly_left == []
    assert [t[:2] for t in newly_reappeared] == [(1, _RIDE_A)]
    assert newly_reappeared[0][2] is dev


def test_full_rental_arc_reserved_then_released():
    """Walk one watch through the three cycles that matter, feeding
    _classify the states a real rental produces in order."""
    parked = _device("aaaa000000000000", lat=39.7365, lon=-104.9918, is_reserved=False)
    riding = _device("aaaa000000000000", lat=39.7400, lon=-104.9806, is_reserved=True)
    dropped = _device("aaaa000000000000", lat=39.7402, lon=-104.9860, is_reserved=False)

    left, back = ride_watch._classify(
        [(1, _RIDE_A, "aaaa000000000000", "watching")], {"aaaa000000000000": parked})
    assert (left, back) == ([], [])          # still on the kerb

    left, back = ride_watch._classify(
        [(1, _RIDE_A, "aaaa000000000000", "watching")], {"aaaa000000000000": riding})
    assert left == [(1, _RIDE_A)]            # -> left_feed

    left, back = ride_watch._classify(
        [(1, _RIDE_A, "aaaa000000000000", "left_feed")], {"aaaa000000000000": riding})
    assert (left, back) == ([], [])          # mid-ride, no end signal yet

    left, back = ride_watch._classify(
        [(1, _RIDE_A, "aaaa000000000000", "left_feed")], {"aaaa000000000000": dropped})
    assert [t[:2] for t in back] == [(1, _RIDE_A)]   # -> resolved, at the drop point
    assert (back[0][2].lat, back[0][2].lon) == (39.7402, -104.9860)


def test_absence_still_counts_as_checked_out():
    """Operators that DO drop rented vehicles (and genuine feed dropouts)
    must keep working — this is the pre-existing contract, asserted here
    against the helper directly so it can't be lost in a later refactor."""
    assert ride_watch._is_checked_out(None) is True


def test_missing_reservation_flag_reads_as_available():
    """src/ingest.py normalises a non-bool is_reserved to None. None must
    NOT mean checked out, or a feed that stops publishing the flag would
    pin every watch open forever."""
    assert ride_watch._is_checked_out(_device("aaaa000000000000", is_reserved=None)) is False


def test_disabled_but_unreserved_is_not_checked_out():
    """is_disabled is out-of-service, not in-use — see _is_checked_out's
    docstring. Reading it as checked out would flip every maintenance-
    flagged vehicle mid-watch."""
    dev = _device("aaaa000000000000", is_reserved=False, is_disabled=True)
    assert ride_watch._is_checked_out(dev) is False


def test_disabled_and_reserved_is_checked_out():
    dev = _device("aaaa000000000000", is_reserved=True, is_disabled=True)
    assert ride_watch._is_checked_out(dev) is True


# ---------- update_watches_for_cycle (DB orchestration, fake cursor) ---------

class _FakeCursor:
    def __init__(self, watch_rows):
        self._watch_rows = watch_rows
        self.executed: list[tuple[str, tuple]] = []
        self.executemany_calls: list[tuple[str, list]] = []
        # A single, strictly-ordered log spanning BOTH execute() and
        # executemany() — the two pre-existing lists above are separate,
        # which makes it impossible to assert real interleaving order (e.g.
        # "the lock ran before the executemany"). This is additive; nothing
        # about the two lists above changes.
        self.call_log: list[tuple[str, str]] = []  # (kind, joined_sql)

    def execute(self, sql, params=()):
        joined = " ".join(sql.split())
        self.executed.append((joined, params))
        self.call_log.append(("execute", joined))

    def executemany(self, sql, seq_of_params):
        joined = " ".join(sql.split())
        self.executemany_calls.append((joined, list(seq_of_params)))
        self.call_log.append(("executemany", joined))

    def fetchall(self):
        return self._watch_rows

    def fetchone(self):
        # No ride exists in this fake schema, so finalize_validation's own
        # `SELECT ... FROM tracked_rides ... FOR UPDATE` (called from
        # update_watches_for_cycle's post-commit finisher loop, see below)
        # reads None and returns immediately — the same "no such ride"
        # no-op path a real, un-donated ride would take here too.
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, watch_rows):
        self.cur = _FakeCursor(watch_rows)
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


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


# ---------- the ride_watch advisory-lock fix + validation-finisher wiring --
# (PLAN_RIDE_MODE_API.md phase A2, "Validation finisher")

def test_advisory_lock_is_taken_before_the_gbfs_reappeared_update(monkeypatch):
    """THE LOAD-BEARING ORDERING. PLAN_RIDE_MODE_API.md's A2 spec: a resolve
    path that ran its gbfs_* row UPDATE first and locked second would
    deadlock against a donation mid-flight on the same ride. This fails if
    that inversion is ever reintroduced — the lock must appear in call_log
    strictly before the executemany that writes gbfs_reappeared_at.

    finalize_validation is stubbed out here (it takes its own, separately-
    tested lock in a LATER transaction — see
    test_finalize_validation_runs_after_the_reappear_transaction_commits —
    which would otherwise add a second, expected-to-be-later lock call and
    muddy this specific assertion)."""
    monkeypatch.setattr(ride_watch, "finalize_validation", lambda cur, ride_id: None)
    dev = _device("aaaa000000000000", lat=39.7, lon=-105.0, current_range_meters=8000)
    stats, conn = _run(
        monkeypatch,
        watch_rows=[(1, _RIDE_A, "aaaa000000000000", "left_feed")],
        devices=[dev],
    )
    assert stats.newly_reappeared == 1

    kinds_and_sql = conn.cur.call_log
    lock_positions = [
        i for i, (kind, sql) in enumerate(kinds_and_sql)
        if kind == "execute" and "pg_advisory_xact_lock" in sql
    ]
    executemany_positions = [
        i for i, (kind, sql) in enumerate(kinds_and_sql)
        if kind == "executemany" and "gbfs_reappeared_at" in sql
    ]
    assert lock_positions, "expected at least one advisory-lock execute() call"
    assert executemany_positions, "expected the gbfs_reappeared_at executemany"
    assert max(lock_positions) < min(executemany_positions), (
        "the ride_validation advisory lock must be acquired BEFORE the "
        "tracked_rides gbfs_reappeared_at UPDATE, not after"
    )

    # And the lock key itself is bound as a parameter (never string-
    # interpolated into the SQL text), per the hashtextextended(%s, 0) idiom
    # src/api_tracked_rides.py's start handler uses.
    lock_sql, lock_params = next(
        (sql, params) for sql, params in conn.cur.executed
        if "pg_advisory_xact_lock" in sql
    )
    assert lock_sql == "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
    assert lock_params == (f"ride_validation:{_RIDE_A}",)


def test_finalize_validation_is_called_once_per_reappeared_ride(monkeypatch):
    dev_a = _device("aaaa000000000000")
    dev_b = _device("bbbb000000000000")
    calls: list[str] = []
    monkeypatch.setattr(
        ride_watch, "finalize_validation",
        lambda cur, ride_id: calls.append(ride_id) or {"status": "eligible"},
    )
    stats, conn = _run(
        monkeypatch,
        watch_rows=[
            (1, _RIDE_A, "aaaa000000000000", "left_feed"),
            (2, _RIDE_B, "bbbb000000000000", "left_feed"),
        ],
        devices=[dev_a, dev_b],
    )
    assert stats.newly_reappeared == 2
    assert sorted(calls) == sorted([str(_RIDE_A), str(_RIDE_B)])
    assert stats.finalized_validations == 2


def test_finalize_validation_runs_after_the_reappear_transaction_commits(monkeypatch):
    """finalize_validation must see gbfs_reappeared_at already committed —
    the whole point of splitting it into its own, later transaction (see
    the module's ADVISORY-LOCK ORDERING note). A commit-order probe: the
    first time finalize_validation is invoked, conn.committed must already
    be True."""
    conn = _FakeConn(watch_rows=[(1, _RIDE_A, "aaaa000000000000", "left_feed")])
    dev = _device("aaaa000000000000")
    seen_committed_before_finalize = []

    def _fake_finalize(cur, ride_id):
        seen_committed_before_finalize.append(conn.committed)
        return None

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(ride_watch, "connection", _fake_connection)
    monkeypatch.setattr(ride_watch, "finalize_validation", _fake_finalize)
    ride_watch.update_watches_for_cycle(uuid.uuid4(), datetime.now(timezone.utc), [dev])

    assert seen_committed_before_finalize == [True]
    assert conn.committed


def test_a_failed_finalize_validation_is_rolled_back_and_does_not_stop_the_cycle(monkeypatch):
    """Isolation, not just crash-safety: one ride's finalize_validation
    blowing up must not undo the already-committed left/reappeared/
    unchanged work for this cycle, and must not stop OTHER reappeared
    rides in the same batch from being finalized."""
    dev_a = _device("aaaa000000000000")
    dev_b = _device("bbbb000000000000")
    seen: list[str] = []

    def _fake_finalize(cur, ride_id):
        seen.append(ride_id)
        if ride_id == str(_RIDE_A):
            raise RuntimeError("battery ingestion blew up")
        return {"status": "ineligible"}

    monkeypatch.setattr(ride_watch, "finalize_validation", _fake_finalize)
    stats, conn = _run(
        monkeypatch,
        watch_rows=[
            (1, _RIDE_A, "aaaa000000000000", "left_feed"),
            (2, _RIDE_B, "bbbb000000000000", "left_feed"),
        ],
        devices=[dev_a, dev_b],
    )
    assert sorted(seen) == sorted([str(_RIDE_A), str(_RIDE_B)])  # both attempted
    assert stats.finalized_validations == 1  # only _RIDE_B counted
    assert conn.rolled_back  # _RIDE_A's failed attempt was rolled back
    assert conn.committed  # the batch's own work still committed normally


def test_finalize_validation_returning_none_is_not_counted(monkeypatch):
    """A no-op settle (nothing pending, or already settled) must not
    inflate finalized_validations."""
    dev = _device("aaaa000000000000")
    monkeypatch.setattr(ride_watch, "finalize_validation", lambda cur, ride_id: None)
    stats, conn = _run(
        monkeypatch,
        watch_rows=[(1, _RIDE_A, "aaaa000000000000", "left_feed")],
        devices=[dev],
    )
    assert stats.newly_reappeared == 1
    assert stats.finalized_validations == 0


def test_left_feed_never_clobbers_a_completed_ride():
    """A ride that ENDED while a cycle was still in flight must not be dragged
    back to `left_feed` when that cycle finally commits.

    `snapshot_time` is when the cycle OBSERVED the feed, not when its
    transaction commits, and a cycle can run for minutes. Seen in production
    on ride faf14a49: departure observed 13:00:01, rider ended at 13:01:22
    (status -> completed), row last written 13:05:34 with status left_feed.
    The end had landed and the late commit undid it.

    Asserted against the SQL rather than through a database because the guard
    is the WHERE clause itself — a fake cursor would happily "apply" an
    UPDATE whose predicate it never evaluates, and prove nothing.
    """
    import inspect

    from src import ride_watch

    src = inspect.getsource(ride_watch.update_watches_for_cycle)
    # The TRACKED_RIDES update, not the watch-list one a few lines above it —
    # both set a status called 'left_feed' and only one of them is this rule.
    start = src.index("UPDATE tracked_rides SET")
    stmt = src[start : src.index('"""', start)]
    assert "AND status = 'watching'" in stmt, (
        "the left_feed transition must be legal only from 'watching'"
    )
