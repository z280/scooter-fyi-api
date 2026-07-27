"""python -m src.cli cleanup_ride_screenshots — deletes ride transaction
screenshots (+ their row) past the 18-month retention, mirroring
cleanup_receipts' fake-cursor idiom."""

from __future__ import annotations

from contextlib import contextmanager

from src import cli


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.deleted_ids: list[int] = []

    def execute(self, sql, params=()):
        if sql.strip().startswith("DELETE FROM ride_transaction_screenshots"):
            self.deleted_ids.append(params[0])

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows):
        self.cur = _FakeCursor(rows)
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True


def test_cleanup_deletes_old_screenshots_and_their_rows(monkeypatch):
    conn = _FakeConn([(1, "ride-screenshots/1/a.jpg"), (2, "ride-screenshots/1/b.jpg")])
    deleted_keys = []
    monkeypatch.setattr(cli, "connection", lambda: _ctx(conn))
    import src.ride_screenshots as rs
    monkeypatch.setattr(rs, "delete_screenshot", lambda key: deleted_keys.append(key))
    # cleanup_ride_screenshots does `from .ride_screenshots import ... delete_screenshot`
    # locally, so patch the source module (re-imported fresh on each call).

    result = cli.cleanup_ride_screenshots()
    assert result == {"deleted": 2, "failed": 0}
    assert deleted_keys == ["ride-screenshots/1/a.jpg", "ride-screenshots/1/b.jpg"]
    assert conn.cur.deleted_ids == [1, 2]
    assert conn.committed


def test_cleanup_continues_after_a_delete_failure(monkeypatch):
    conn = _FakeConn([(1, "a.jpg"), (2, "b.jpg")])
    monkeypatch.setattr(cli, "connection", lambda: _ctx(conn))
    import src.ride_screenshots as rs

    def _flaky(key):
        if key == "a.jpg":
            raise RuntimeError("R2 hiccup")

    monkeypatch.setattr(rs, "delete_screenshot", _flaky)
    result = cli.cleanup_ride_screenshots()
    assert result == {"deleted": 1, "failed": 1}
    assert conn.cur.deleted_ids == [2]  # only the successful one's row is removed


def test_cleanup_is_a_noop_when_nothing_is_due(monkeypatch):
    conn = _FakeConn([])
    monkeypatch.setattr(cli, "connection", lambda: _ctx(conn))
    result = cli.cleanup_ride_screenshots()
    assert result == {"deleted": 0, "failed": 0}


def test_cleanup_ride_screenshots_registered_in_cli_commands():
    assert "cleanup_ride_screenshots" in cli.COMMANDS


@contextmanager
def _ctx(conn):
    yield conn
