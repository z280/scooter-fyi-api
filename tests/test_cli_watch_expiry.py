"""python -m src.cli expire_stale_watches — closes out watch-list/ride
rows whose 3h window elapsed with no GBFS-side resolution."""

from __future__ import annotations

from contextlib import contextmanager

from src import cli


class _FakeCursor:
    """rowcounts feeds the first N execute() calls (the two UPDATEs); any
    execute() beyond that (the validation-finisher SELECT, added below)
    just gets rowcount 0 rather than raising, since a SELECT's caller reads
    fetchall(), not rowcount. stale_ride_ids feeds that SELECT's fetchall()
    — empty by default, i.e. "nothing for finalize_validation to do", which
    is what every pre-existing test here wants: none of them touch
    validation_status at all, so under a real schema nothing would ever
    match the finisher's WHERE clause either."""

    def __init__(self, rowcounts, stale_ride_ids=()):
        self._rowcounts = list(rowcounts)
        self._stale_ride_ids = list(stale_ride_ids)
        self.executed: list[str] = []
        self.rowcount = 0

    def execute(self, sql, params=()):
        self.executed.append(" ".join(sql.split()))
        self.rowcount = self._rowcounts.pop(0) if self._rowcounts else 0

    def fetchall(self):
        return [(r,) for r in self._stale_ride_ids]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rowcounts, stale_ride_ids=()):
        self._rowcounts = rowcounts
        self._stale_ride_ids = stale_ride_ids
        self.committed = False
        self.rolled_back = False
        self.cursors: list[_FakeCursor] = []

    def cursor(self):
        c = _FakeCursor(self._rowcounts, self._stale_ride_ids)
        self.cursors.append(c)
        return c

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def test_expire_stale_watches_updates_both_tables_and_returns_counts(monkeypatch):
    conn = _FakeConn([3, 2])  # watches_expired=3, rides_expired=2

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(cli, "connection", _fake_connection)
    result = cli.expire_stale_watches()
    assert result == {"watches_expired": 3, "rides_expired": 2, "finalized_validations": 0}
    assert conn.committed


def test_expire_stale_watches_is_a_noop_when_nothing_expired(monkeypatch):
    conn = _FakeConn([0, 0])

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(cli, "connection", _fake_connection)
    result = cli.expire_stale_watches()
    assert result == {"watches_expired": 0, "rides_expired": 0, "finalized_validations": 0}


def test_expire_stale_watches_registered_in_cli_commands():
    assert "expire_stale_watches" in cli.COMMANDS


# ---------- validation-finisher wiring (PLAN_RIDE_MODE_API.md phase A2) ----

def test_finisher_select_keys_on_watch_window_not_ride_status(monkeypatch):
    """A donated ride already has user_reported_ended_at set and isn't
    'watching'/'left_feed' any more, so the finisher SELECT must key off
    watch_expires_at/gbfs_reappeared_at instead — pinned as a static
    assertion on the query text so a future edit that folds it back into
    the status-based UPDATE above fails loudly."""
    conn = _FakeConn([0, 0])

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(cli, "connection", _fake_connection)
    cli.expire_stale_watches()

    all_sql = [s for c in conn.cursors for s in c.executed]
    finisher_sql = next(s for s in all_sql if "validation_status = 'pending_feed'" in s)
    assert "watch_expires_at <" in finisher_sql
    assert "gbfs_reappeared_at IS NULL" in finisher_sql


def test_finisher_calls_finalize_validation_once_per_stale_ride(monkeypatch):
    conn = _FakeConn([0, 0], stale_ride_ids=[11, 22])
    calls: list[str] = []

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(cli, "connection", _fake_connection)
    monkeypatch.setattr(cli, "finalize_validation", lambda cur, rid: calls.append(rid) or {"status": "eligible"})
    result = cli.expire_stale_watches()

    assert calls == ["11", "22"]
    assert result["finalized_validations"] == 2


def test_finisher_returns_none_are_not_counted(monkeypatch):
    """finalize_validation returns None when there was nothing to settle
    (idempotent no-op) — that must not inflate finalized_validations."""
    conn = _FakeConn([0, 0], stale_ride_ids=[11])

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(cli, "connection", _fake_connection)
    monkeypatch.setattr(cli, "finalize_validation", lambda cur, rid: None)
    result = cli.expire_stale_watches()

    assert result["finalized_validations"] == 0


def test_one_rides_finalize_failure_does_not_stop_the_others(monkeypatch):
    conn = _FakeConn([0, 0], stale_ride_ids=[11, 22])
    seen: list[str] = []

    def _finalize(cur, rid):
        seen.append(rid)
        if rid == "11":
            raise RuntimeError("boom")
        return {"status": "ineligible"}

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(cli, "connection", _fake_connection)
    monkeypatch.setattr(cli, "finalize_validation", _finalize)
    result = cli.expire_stale_watches()

    assert seen == ["11", "22"]  # the second ride still ran
    assert result["finalized_validations"] == 1  # only ride 22 counted
    assert conn.rolled_back  # ride 11's failed attempt was rolled back
