"""python -m src.cli backfill_public_usernames — one-time backfill for
accounts created before sql/025."""

from __future__ import annotations

from contextlib import contextmanager

from src import accounts, cli


class _FakeCursor:
    def __init__(self, ids):
        self._ids = ids

    def execute(self, sql, params=()):
        pass

    def fetchall(self):
        return [(i,) for i in self._ids]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, ids):
        self._ids = ids

    def cursor(self):
        return _FakeCursor(self._ids)

    def commit(self):
        pass


def test_backfill_assigns_a_username_to_every_null_row(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(accounts, "assign_public_username", lambda cur, aid: calls.append(aid))

    @contextmanager
    def _fake_connection():
        yield _FakeConn([3, 7, 9])

    monkeypatch.setattr(cli, "connection", _fake_connection)
    result = cli.backfill_public_usernames()
    assert result == {"assigned": 3}
    assert calls == [3, 7, 9]


def test_backfill_is_a_noop_when_nothing_to_backfill(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(accounts, "assign_public_username", lambda cur, aid: calls.append(aid))

    @contextmanager
    def _fake_connection():
        yield _FakeConn([])

    monkeypatch.setattr(cli, "connection", _fake_connection)
    result = cli.backfill_public_usernames()
    assert result == {"assigned": 0}
    assert calls == []


def test_backfill_registered_in_cli_commands():
    assert "backfill_public_usernames" in cli.COMMANDS
