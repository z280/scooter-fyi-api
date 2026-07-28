"""python -m src.cli expire_stale_off_feed_rides — closes out off-feed
rides left 'active' for 24 hours with no end report (sql/040).

Unlike expire_stale_watches this job is load-bearing, not cosmetic:
idx_rides_one_active_per_account is a partial UNIQUE index on
status = 'active', so an abandoned ride 409s its owner out of
POST /api/v1/rides/start until this runs. The end-to-end proof that the
sweep frees the slot lives in tests/test_off_feed_rides_lifecycle_pg.py,
against a real index.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from src import cli


class _FakeCursor:
    def __init__(self, rowcounts):
        self._rowcounts = list(rowcounts)
        self.executed: list[tuple[str, tuple]] = []
        self.rowcount = 0

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))
        self.rowcount = self._rowcounts.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rowcounts):
        self.cur = _FakeCursor(rowcounts)
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True


def _run(monkeypatch, rowcounts):
    conn = _FakeConn(rowcounts)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(cli, "connection", _fake_connection)
    return cli.expire_stale_off_feed_rides(), conn


def test_expires_active_rides_and_returns_the_count(monkeypatch):
    result, conn = _run(monkeypatch, [4])
    assert result == {"rides_expired": 4}
    assert conn.committed


def test_is_a_noop_when_nothing_is_stale(monkeypatch):
    result, _ = _run(monkeypatch, [0])
    assert result == {"rides_expired": 0}


def test_only_touches_active_rides(monkeypatch):
    """A completed ride is finished history and an already-expired one is
    already terminal — re-stamping either would be a lie about when it
    ended. The WHERE clause is also what makes the job idempotent."""
    _, conn = _run(monkeypatch, [1])
    sql, _params = conn.cur.executed[0]
    assert "SET status = 'expired'" in sql
    assert "WHERE status = 'active'" in sql


def test_never_invents_an_end(monkeypatch):
    """sql/040's semantics: we never observed an end, so ended_at,
    duration_s and end_lat/end_lon stay NULL and the waypoint-measured
    distance is left exactly as it stood."""
    _, conn = _run(monkeypatch, [1])
    sql, _params = conn.cur.executed[0]
    for column in ("ended_at", "duration_s", "end_lat", "end_lon",
                   "distance_m", "distance_source"):
        assert f"{column} =" not in sql


def test_the_clock_runs_from_created_at_not_started_at(monkeypatch):
    """started_at is client-supplied (RideStartIn lets a rider backdate a
    start they noticed late), so it is spoofable in both directions:
    backdating 25h would expire a ride the instant it began, and
    post-dating it would exempt the ride forever. created_at is
    DEFAULT NOW() and nothing outside Postgres writes it."""
    _, conn = _run(monkeypatch, [1])
    sql, params = conn.cur.executed[0]
    assert "created_at < %s" in sql
    assert "started_at" not in sql

    cutoff = params[0]
    age = datetime.now(timezone.utc) - cutoff
    assert abs(age - timedelta(hours=24)) < timedelta(minutes=1)


def test_registered_in_cli_commands():
    assert "expire_stale_off_feed_rides" in cli.COMMANDS
