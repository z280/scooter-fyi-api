"""python -m src.cli expire_stale_watches — closes out watch-list/ride
rows whose 3h window elapsed with no GBFS-side resolution."""

from __future__ import annotations

from contextlib import contextmanager

from src import cli


class _FakeCursor:
    def __init__(self, rowcounts):
        self._rowcounts = list(rowcounts)
        self.executed: list[str] = []
        self.rowcount = 0

    def execute(self, sql, params=()):
        self.executed.append(" ".join(sql.split()))
        self.rowcount = self._rowcounts.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rowcounts):
        self._rowcounts = rowcounts
        self.committed = False

    def cursor(self):
        return _FakeCursor(self._rowcounts)

    def commit(self):
        self.committed = True


def test_expire_stale_watches_updates_both_tables_and_returns_counts(monkeypatch):
    conn = _FakeConn([3, 2])  # watches_expired=3, rides_expired=2

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(cli, "connection", _fake_connection)
    result = cli.expire_stale_watches()
    assert result == {"watches_expired": 3, "rides_expired": 2}
    assert conn.committed


def test_expire_stale_watches_is_a_noop_when_nothing_expired(monkeypatch):
    conn = _FakeConn([0, 0])

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(cli, "connection", _fake_connection)
    result = cli.expire_stale_watches()
    assert result == {"watches_expired": 0, "rides_expired": 0}


def test_expire_stale_watches_registered_in_cli_commands():
    assert "expire_stale_watches" in cli.COMMANDS
