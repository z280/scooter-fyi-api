"""De-id sweep — `python -m src.cli deidentify_donations`
(PLAN_RIDE_MODE_API.md phase A2 / RIDE_MODE_OVERHAUL_PLAN.md's "De-id"
glossary entry, `src/cli.py:deidentify_donations`).

Fake-cursor unit tests, same idiom as tests/test_ride_usuals.py: a small
in-memory store standing in for `track_donations` / `donated_track_points`
(and, when a test opts in, `ride_routes`), driven by a fake cursor that
recognizes the exact SQL text the function issues and applies the same
predicate a real Postgres would, rather than a canned list of fetch
results — the property worth testing here is the BOUNDARY MATH (4h grace,
28h force floor) and the coarsening arithmetic, and a canned-result fake
could not catch either wrong.

The `ride_routes` guard gets its own belt-and-braces device: the fake
raises AssertionError the instant any SQL mentioning `ride_routes` (other
than the `to_regclass` probe itself) is executed while the fake's
`has_ride_routes` flag is False — i.e. it behaves like a real Postgres
would against a database that has not applied sql/052 (UndefinedTable),
except louder and synchronous. That is strictly stronger than only
asserting the call didn't happen: it fails even if some future edit
issues a *different* ride_routes statement than the one this file knows
about today.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from src import cli

_NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fake store + cursor
# ---------------------------------------------------------------------------

class _Donation:
    __slots__ = (
        "id", "account_id", "tracked_ride_id", "points_settled_at",
        "donated_at", "deidentified_at",
    )

    def __init__(self, id, account_id, tracked_ride_id, points_settled_at,
                 donated_at, deidentified_at=None):
        self.id = id
        self.account_id = account_id
        self.tracked_ride_id = tracked_ride_id
        self.points_settled_at = points_settled_at
        self.donated_at = donated_at
        self.deidentified_at = deidentified_at


class _RideRoute:
    __slots__ = ("id", "account_id", "tracked_ride_id", "created_at", "deidentified_at")

    def __init__(self, id, account_id, tracked_ride_id, created_at, deidentified_at=None):
        self.id = id
        self.account_id = account_id
        self.tracked_ride_id = tracked_ride_id
        self.created_at = created_at
        self.deidentified_at = deidentified_at


class _FakeStore:
    def __init__(self) -> None:
        self.donations: dict[str, _Donation] = {}
        self.points: list[dict] = []  # {"donation_id", "seq", "recorded_ms"}
        self.ride_routes: dict[str, _RideRoute] = {}
        self.has_ride_routes = False
        self.executed: list[tuple[str, tuple]] = []

    def add_donation(self, id, *, account_id=1, tracked_ride_id="ride-1",
                      points_settled_at=None, donated_at, deidentified_at=None):
        self.donations[id] = _Donation(
            id, account_id, tracked_ride_id, points_settled_at, donated_at,
            deidentified_at,
        )

    def add_point(self, donation_id, seq, recorded_ms):
        self.points.append(
            {"donation_id": donation_id, "seq": seq, "recorded_ms": recorded_ms}
        )

    def add_ride_route(self, id, *, account_id=1, tracked_ride_id="ride-1",
                        created_at, deidentified_at=None):
        self.ride_routes[id] = _RideRoute(
            id, account_id, tracked_ride_id, created_at, deidentified_at,
        )


class _FakeCursor:
    def __init__(self, store: _FakeStore) -> None:
        self._store = store
        self._result: list[tuple] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        params = tuple(params)
        self._store.executed.append((s, params))
        self._result = []
        self.rowcount = 0

        # -- track_donations ---------------------------------------------
        if s.startswith("SELECT COUNT(*) FROM track_donations"):
            settled_cutoff, donated_cutoff = params
            n = sum(
                1 for d in self._store.donations.values()
                if d.deidentified_at is None
                and self._eligible(d, settled_cutoff, donated_cutoff)
            )
            self._result = [(n,)]
            return

        if s.startswith("UPDATE track_donations"):
            assert "SET account_id = NULL, tracked_ride_id = NULL, deidentified_at = NOW()" in s
            assert "WHERE deidentified_at IS NULL" in s
            assert "RETURNING id" in s
            settled_cutoff, donated_cutoff = params
            hit_ids = []
            for d in self._store.donations.values():
                if d.deidentified_at is not None:
                    continue
                if self._eligible(d, settled_cutoff, donated_cutoff):
                    d.account_id = None
                    d.tracked_ride_id = None
                    d.deidentified_at = _NOW
                    hit_ids.append(d.id)
            self._result = [(i,) for i in hit_ids]
            self.rowcount = len(hit_ids)
            return

        # -- donated_track_points ------------------------------------------
        if s.startswith("UPDATE donated_track_points"):
            assert "recorded_ms = (recorded_ms / %s) * %s" in s
            assert "WHERE donation_id = ANY(%s)" in s
            step_a, step_b, donation_ids = params
            assert step_a == step_b == 60_000, "coarsening must floor to a whole minute"
            n = 0
            for p in self._store.points:
                if p["donation_id"] in donation_ids:
                    p["recorded_ms"] = (p["recorded_ms"] // step_a) * step_a
                    n += 1
            self.rowcount = n
            return

        # -- ride_routes existence probe ------------------------------------
        if s == "SELECT to_regclass('ride_routes')":
            self._result = [(12345 if self._store.has_ride_routes else None,)]
            return

        # -- ride_routes arm (only ever reached if the probe above said yes) -
        if "ride_routes" in s:
            if not self._store.has_ride_routes:
                raise AssertionError(
                    "ride_routes SQL executed against a schema without the "
                    f"table (to_regclass would have returned NULL): {s}"
                )
            if s.startswith("SELECT COUNT(*) FROM ride_routes"):
                (cutoff,) = params
                n = sum(
                    1 for r in self._store.ride_routes.values()
                    if r.deidentified_at is None and r.created_at < cutoff
                )
                self._result = [(n,)]
                return
            if s.startswith("UPDATE ride_routes"):
                assert "SET account_id = NULL, tracked_ride_id = NULL, deidentified_at = NOW()" in s
                assert "WHERE deidentified_at IS NULL AND created_at < %s" in s
                (cutoff,) = params
                n = 0
                for r in self._store.ride_routes.values():
                    if r.deidentified_at is None and r.created_at < cutoff:
                        r.account_id = None
                        r.tracked_ride_id = None
                        r.deidentified_at = _NOW
                        n += 1
                self.rowcount = n
                return

        raise AssertionError(f"unexpected SQL reached the fake cursor: {s}")

    @staticmethod
    def _eligible(d: _Donation, settled_cutoff, donated_cutoff) -> bool:
        settled_hit = d.points_settled_at is not None and d.points_settled_at < settled_cutoff
        donated_hit = d.donated_at < donated_cutoff
        return settled_hit or donated_hit


class _FakeConn:
    def __init__(self, store: _FakeStore) -> None:
        self._store = store
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self._store)

    def commit(self):
        self.commits += 1


@pytest.fixture()
def store(monkeypatch) -> _FakeStore:
    st = _FakeStore()

    @contextmanager
    def _fake_connection():
        yield _FakeConn(st)

    monkeypatch.setattr(cli, "connection", _fake_connection)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return _NOW if tz is not None else _NOW.replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", _FrozenDatetime)
    return st


# ---------------------------------------------------------------------------
# 4h settled-grace boundary — exact
# ---------------------------------------------------------------------------

def test_settled_3h59m_ago_is_untouched(store):
    store.add_donation(
        "d1", points_settled_at=_NOW - timedelta(hours=3, minutes=59),
        donated_at=_NOW - timedelta(hours=5),
    )
    result = cli.deidentify_donations()
    assert result["donations_deidentified"] == 0
    d = store.donations["d1"]
    assert d.deidentified_at is None
    assert d.account_id == 1
    assert d.tracked_ride_id == "ride-1"


def test_settled_4h01m_ago_is_deidentified(store):
    store.add_donation(
        "d1", points_settled_at=_NOW - timedelta(hours=4, minutes=1),
        donated_at=_NOW - timedelta(hours=5),
    )
    result = cli.deidentify_donations()
    assert result["donations_deidentified"] == 1
    d = store.donations["d1"]
    assert d.deidentified_at == _NOW
    assert d.account_id is None
    assert d.tracked_ride_id is None


def test_settled_exactly_4h_ago_is_untouched(store):
    """The predicate is a strict `<`, so the boundary instant itself does
    not yet qualify — it does on the very next tick."""
    store.add_donation(
        "d1", points_settled_at=_NOW - timedelta(hours=4),
        donated_at=_NOW - timedelta(hours=5),
    )
    result = cli.deidentify_donations()
    assert result["donations_deidentified"] == 0


# ---------------------------------------------------------------------------
# 28h force-floor boundary — exact, independent of settlement
# ---------------------------------------------------------------------------

def test_donated_27h59m_ago_with_points_never_settled_is_untouched(store):
    store.add_donation(
        "d1", points_settled_at=None, donated_at=_NOW - timedelta(hours=27, minutes=59),
    )
    result = cli.deidentify_donations()
    assert result["donations_deidentified"] == 0
    assert store.donations["d1"].deidentified_at is None


def test_donated_28h01m_ago_is_deidentified_regardless_of_settlement(store):
    store.add_donation(
        "d1", points_settled_at=None, donated_at=_NOW - timedelta(hours=28, minutes=1),
    )
    result = cli.deidentify_donations()
    assert result["donations_deidentified"] == 1
    assert store.donations["d1"].deidentified_at == _NOW


def test_the_force_floor_overrides_a_recently_settled_donation(store):
    """Points settled 10 minutes ago (comfortably inside the 4h grace) but
    donated 29h ago: the force floor still fires. 'or a hard floor of 28h
    after donation even if points never settle' — this is the harder half
    of that sentence, where points DID settle, just too late to matter."""
    store.add_donation(
        "d1", points_settled_at=_NOW - timedelta(minutes=10),
        donated_at=_NOW - timedelta(hours=29),
    )
    result = cli.deidentify_donations()
    assert result["donations_deidentified"] == 1


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------

def test_already_deidentified_rows_are_left_alone(store):
    already = _NOW - timedelta(hours=10)
    store.add_donation(
        "d1", account_id=None, tracked_ride_id=None,
        points_settled_at=_NOW - timedelta(hours=40),
        donated_at=_NOW - timedelta(hours=40),
        deidentified_at=already,
    )
    result = cli.deidentify_donations()
    assert result["donations_deidentified"] == 0
    assert store.donations["d1"].deidentified_at == already, \
        "an already-swept row's deidentified_at must not be re-stamped"


def test_running_twice_in_a_row_does_nothing_extra_the_second_time(store):
    store.add_donation(
        "d1", points_settled_at=_NOW - timedelta(hours=10),
        donated_at=_NOW - timedelta(hours=11),
    )
    first = cli.deidentify_donations()
    assert first["donations_deidentified"] == 1

    second = cli.deidentify_donations()
    assert second["donations_deidentified"] == 0
    assert second["points_coarsened"] == 0


def test_a_mix_of_eligible_and_ineligible_rows_only_sweeps_the_eligible_ones(store):
    store.add_donation(
        "old", points_settled_at=_NOW - timedelta(hours=5), donated_at=_NOW - timedelta(hours=6),
    )
    store.add_donation(
        "fresh", points_settled_at=_NOW - timedelta(minutes=1), donated_at=_NOW - timedelta(hours=1),
    )
    result = cli.deidentify_donations()
    assert result["donations_deidentified"] == 1
    assert store.donations["old"].deidentified_at == _NOW
    assert store.donations["fresh"].deidentified_at is None


# ---------------------------------------------------------------------------
# recorded_ms coarsening — real millisecond values
# ---------------------------------------------------------------------------

def test_recorded_ms_is_coarsened_to_minute_precision(store):
    store.add_donation(
        "d1", points_settled_at=_NOW - timedelta(hours=5), donated_at=_NOW - timedelta(hours=6),
    )
    # A real epoch-ms timestamp: 2026-07-30T11:47:23.456Z, well inside its
    # own minute (11:47:00.000 - 11:47:59.999).
    store.add_point("d1", 0, 1785498443456)
    store.add_point("d1", 1, 1785498443456 + 1_000)   # +1s, same minute
    store.add_point("d1", 2, 1785498443456 + 59_999)  # +60s roughly, crosses into next minute
    store.add_point("d1", 3, 0)                       # epoch zero: still a whole minute

    result = cli.deidentify_donations()
    assert result["donations_deidentified"] == 1
    assert result["points_coarsened"] == 4

    coarsened = {p["seq"]: p["recorded_ms"] for p in store.points}
    assert coarsened[0] == (1785498443456 // 60_000) * 60_000 == 1785498420000
    assert coarsened[0] % 60_000 == 0
    assert coarsened[1] == coarsened[0], "same minute as point 0 -> same coarsened value"
    assert coarsened[2] != coarsened[0], "60s later crossed into the next minute"
    assert coarsened[2] % 60_000 == 0
    assert coarsened[3] == 0


def test_points_belonging_to_an_untouched_donation_are_not_coarsened(store):
    store.add_donation(
        "fresh", points_settled_at=_NOW - timedelta(minutes=1), donated_at=_NOW - timedelta(hours=1),
    )
    store.add_point("fresh", 0, 1785498443456)
    result = cli.deidentify_donations()
    assert result["points_coarsened"] == 0
    assert store.points[0]["recorded_ms"] == 1785498443456


def test_only_points_of_swept_donations_are_touched_not_the_whole_table(store):
    store.add_donation(
        "old", points_settled_at=_NOW - timedelta(hours=5), donated_at=_NOW - timedelta(hours=6),
    )
    store.add_donation(
        "fresh", points_settled_at=_NOW - timedelta(minutes=1), donated_at=_NOW - timedelta(hours=1),
    )
    store.add_point("old", 0, 1785498443456)
    store.add_point("fresh", 0, 1785498443456)

    result = cli.deidentify_donations()
    assert result["points_coarsened"] == 1
    by_donation = {p["donation_id"]: p["recorded_ms"] for p in store.points}
    assert by_donation["old"] == 1785498420000
    assert by_donation["fresh"] == 1785498443456


# ---------------------------------------------------------------------------
# ride_routes guard: table absent today -> never touched, never errors
# ---------------------------------------------------------------------------

def test_ride_routes_is_never_touched_when_the_table_does_not_exist(store):
    """store.has_ride_routes defaults to False (sql/052 has not landed in
    this build order). Any SQL statement mentioning ride_routes other than
    the to_regclass probe raises inside the fake — so this test fails loud
    if the guard is ever removed or miswired, not just quietly passes."""
    store.add_donation(
        "d1", points_settled_at=_NOW - timedelta(hours=5), donated_at=_NOW - timedelta(hours=6),
    )
    result = cli.deidentify_donations()
    assert result["ride_routes_deidentified"] == 0
    probe = [e for e in store.executed if e[0] == "SELECT to_regclass('ride_routes')"]
    assert len(probe) == 1, "the existence probe itself must still run"
    assert not any(
        "ride_routes" in sql and sql != "SELECT to_regclass('ride_routes')"
        for sql, _ in store.executed
    )


def test_the_probe_runs_even_when_no_donation_is_eligible(store):
    """The ride_routes arm's guard check is independent of whether the
    donations arm found anything to sweep this run."""
    result = cli.deidentify_donations()
    assert result["donations_deidentified"] == 0
    assert result["ride_routes_deidentified"] == 0
    assert any(sql == "SELECT to_regclass('ride_routes')" for sql, _ in store.executed)


def test_a_dry_run_touches_nothing_but_still_reports_what_it_would_do(store):
    store.add_donation(
        "d1", points_settled_at=_NOW - timedelta(hours=5), donated_at=_NOW - timedelta(hours=6),
    )
    store.add_point("d1", 0, 1785498443456)

    result = cli.deidentify_donations(dry_run=True)
    assert result["donations_deidentified"] == 1
    assert result["dry_run"] is True

    d = store.donations["d1"]
    assert d.deidentified_at is None, "a dry run must not write anything"
    assert d.account_id == 1
    assert store.points[0]["recorded_ms"] == 1785498443456, \
        "a dry run must not coarsen any recorded_ms either"


# ---------------------------------------------------------------------------
# ride_routes guard: table present (A3 landed) -> the same predicate
# "activates" with no code change, per the module docstring's claim
# ---------------------------------------------------------------------------

def test_ride_routes_sweeps_on_its_own_28h_clock_once_the_table_exists(store):
    store.has_ride_routes = True
    store.add_ride_route("r1", created_at=_NOW - timedelta(hours=29))
    store.add_ride_route("r2", created_at=_NOW - timedelta(hours=27))

    result = cli.deidentify_donations()
    assert result["ride_routes_deidentified"] == 1
    assert store.ride_routes["r1"].deidentified_at == _NOW
    assert store.ride_routes["r1"].account_id is None
    assert store.ride_routes["r1"].tracked_ride_id is None
    assert store.ride_routes["r2"].deidentified_at is None


def test_ride_routes_sweep_is_independent_of_any_donation(store):
    """A nav-improvement ride whose track was never donated still has no
    track_donations row at all -- the ride_routes arm must not require one."""
    store.has_ride_routes = True
    store.add_ride_route("r1", created_at=_NOW - timedelta(hours=30))
    assert store.donations == {}

    result = cli.deidentify_donations()
    assert result["ride_routes_deidentified"] == 1
    assert result["donations_deidentified"] == 0


def test_ride_routes_idempotence(store):
    store.has_ride_routes = True
    store.add_ride_route("r1", created_at=_NOW - timedelta(hours=30))

    first = cli.deidentify_donations()
    assert first["ride_routes_deidentified"] == 1
    second = cli.deidentify_donations()
    assert second["ride_routes_deidentified"] == 0


# ---------------------------------------------------------------------------
# Commit discipline
# ---------------------------------------------------------------------------

def test_a_real_run_commits(monkeypatch, store):
    conns: list[_FakeConn] = []

    @contextmanager
    def _tracking_connection():
        conn = _FakeConn(store)
        conns.append(conn)
        yield conn

    monkeypatch.setattr(cli, "connection", _tracking_connection)
    cli.deidentify_donations()
    assert conns[0].commits == 1


def test_a_dry_run_does_not_commit(monkeypatch, store):
    conns: list[_FakeConn] = []

    @contextmanager
    def _tracking_connection():
        conn = _FakeConn(store)
        conns.append(conn)
        yield conn

    monkeypatch.setattr(cli, "connection", _tracking_connection)
    cli.deidentify_donations(dry_run=True)
    assert conns[0].commits == 0
