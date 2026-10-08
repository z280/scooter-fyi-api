"""python -m src.cli scrub_award_locations — against a real database.

A fake cursor could prove the arithmetic; only a real Postgres can prove the
thing this command exists to promise, which is that moving every row leaves
every ANSWER unchanged. So the leaderboard and territory reads are taken
before and after the sweep and compared, rather than reasoned about.

SKIPS unless VEO_TEST_PG_DSN names a reachable, migratable database. NEVER
point that at production: the fixture replays every migration and wipes
user_points and its own accounts.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import h3
import pytest

psycopg = pytest.importorskip("psycopg")

from src import cli  # noqa: E402
from src.accounts import upsert_account  # noqa: E402
from src.points import h3_8_index_for  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_TEST_EMAIL_LIKE = "pgtest-scrub-%@example.com"

# A real Denver doorstep, to five decimals — about a metre, which is the
# precision this command exists to destroy.
_HOME = (39.72851, -105.03452)
_RIDE_START = (39.74102, -104.98887)
# A scooter in a public street. NOT swept: the exact point is the only record
# of where a reported vehicle actually was.
_VEHICLE = (39.75011, -104.99623)


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture()
def pg_conn(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — scrub Postgres integration test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        try:
            cur.execute("DELETE FROM user_points")
        except psycopg.errors.UndefinedTable:
            conn.rollback()
    conn.commit()
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_points")
        cur.execute("DELETE FROM accounts WHERE email LIKE %s", (_TEST_EMAIL_LIKE,))
    conn.commit()

    @contextmanager
    def _conn():
        yield conn

    monkeypatch.setattr(cli, "connection", _conn)
    yield conn

    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_points")
        cur.execute("DELETE FROM accounts WHERE email LIKE %s", (_TEST_EMAIL_LIKE,))
    conn.commit()
    conn.close()


def _account(conn, tag: str) -> int:
    with conn.cursor() as cur:
        return upsert_account(cur, email=f"pgtest-scrub-{tag}@example.com")


def _award(conn, account_id: int, action: str, at: tuple[float, float]) -> int:
    lat, lng = at
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_points (account_id, action, points, lat, lng, h3_8_index)
            VALUES (%s, %s, 10, %s, %s, %s) RETURNING id
            """,
            (account_id, action, lat, lng, h3_8_index_for(lat, lng)),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    return row_id


def _row(conn, row_id: int):
    with conn.cursor() as cur:
        cur.execute("SELECT lat, lng, h3_8_index FROM user_points WHERE id = %s", (row_id,))
        return cur.fetchone()


def test_the_doorstep_stops_being_in_the_table(pg_conn):
    """The whole point, asserted rather than assumed."""
    acct = _account(pg_conn, "home")
    row_id = _award(pg_conn, acct, "profile_completion", _HOME)

    result = cli.scrub_award_locations()
    assert result["moved"] == 1

    lat, lng, _ = _row(pg_conn, row_id)
    assert (lat, lng) != _HOME
    # Not merely different — far enough that it is no longer an address. The
    # hex is ~0.46 km², so the centre is hundreds of metres from an edge point.
    assert abs(lat - _HOME[0]) + abs(lng - _HOME[1]) > 0.0005


def test_the_h3_index_is_untouched_so_territory_cannot_move(pg_conn):
    # The index is what every reader of this table actually selects. If the
    # sweep could change it, it would silently redraw somebody's territory.
    acct = _account(pg_conn, "territory")
    row_id = _award(pg_conn, acct, "profile_completion", _HOME)
    before = _row(pg_conn, row_id)[2]

    cli.scrub_award_locations()

    lat, lng, after = _row(pg_conn, row_id)
    assert after == before
    # And the new point genuinely lies in that same cell, so the row is not
    # merely unchanged-by-luck — it is internally consistent.
    assert h3_8_index_for(lat, lng) == before


def test_it_sweeps_ride_located_awards_too(pg_conn):
    # Nobody stored a ride start as a home address, but a handful of them for
    # one account describes one anyway.
    acct = _account(pg_conn, "ride")
    row_id = _award(pg_conn, acct, "nav_distance_bonus", _RIDE_START)
    cli.scrub_award_locations()
    lat, lng, _ = _row(pg_conn, row_id)
    assert (lat, lng) != _RIDE_START


def test_a_vehicle_position_is_left_exact(pg_conn):
    # A shared scooter in a public street is not a private place, and the exact
    # point is the only record of where a reported vehicle actually was.
    acct = _account(pg_conn, "vehicle")
    row_id = _award(pg_conn, acct, "qr_scan", _VEHICLE)
    cli.scrub_award_locations()
    lat, lng, _ = _row(pg_conn, row_id)
    assert (lat, lng) == _VEHICLE


def test_running_it_twice_moves_nothing_the_second_time(pg_conn):
    # Idempotent with no marker column: a scrubbed row IS one already at its
    # own cell centre. This is what makes the command safe to schedule.
    acct = _account(pg_conn, "twice")
    _award(pg_conn, acct, "profile_completion", _HOME)

    first = cli.scrub_award_locations()
    second = cli.scrub_award_locations()

    assert first["moved"] == 1
    assert second["moved"] == 0
    assert second["already_centred"] == 1


def test_dry_run_writes_nothing(pg_conn):
    acct = _account(pg_conn, "dry")
    row_id = _award(pg_conn, acct, "profile_completion", _HOME)

    result = cli.scrub_award_locations(dry_run=True)

    assert result["moved"] == 1 and result["dry_run"] is True
    assert _row(pg_conn, row_id)[:2] == _HOME


def test_a_dry_run_issues_no_update_at_all(pg_conn, monkeypatch):
    """Not merely "writes nothing" — the rollback would deliver that on its
    own. A dry run must not take a write lock on every row it examined, which
    on a large ledger is the difference between a look and an outage.
    """
    acct = _account(pg_conn, "nolock")
    _award(pg_conn, acct, "profile_completion", _HOME)

    seen: list[str] = []

    class _SpyCursor:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            return self._inner.execute(sql, params)

        def executemany(self, sql, params):
            seen.append(sql)
            return self._inner.executemany(sql, params)

        def fetchall(self):
            return self._inner.fetchall()

        def fetchone(self):
            return self._inner.fetchone()

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *a):
            return self._inner.__exit__(*a)

    class _SpyConn:
        def __init__(self, inner):
            self._inner = inner

        def cursor(self):
            return _SpyCursor(self._inner.cursor())

        def commit(self):
            return self._inner.commit()

        def rollback(self):
            return self._inner.rollback()

    @contextmanager
    def _conn():
        yield _SpyConn(pg_conn)

    monkeypatch.setattr(cli, "connection", _conn)

    assert cli.scrub_award_locations(dry_run=True)["moved"] == 1
    assert seen == []

    assert cli.scrub_award_locations()["moved"] == 1
    assert len(seen) == 1


def test_the_centre_comes_from_the_index_not_the_coordinate(pg_conn):
    """A row whose two disagree must not be moved to a different hex.

    The index is the authority for where a row IS — it is what every reader
    selects — so re-centring from the coordinate would quietly hand that row's
    territory to whichever cell the stale coordinate happened to fall in.
    """
    acct = _account(pg_conn, "disagree")
    row_id = _award(pg_conn, acct, "profile_completion", _HOME)
    # Force a disagreement: keep the index, move the coordinate miles away.
    far = (39.9, -105.3)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE user_points SET lat = %s, lng = %s WHERE id = %s",
                    (far[0], far[1], row_id))
    pg_conn.commit()
    kept_index = _row(pg_conn, row_id)[2]

    cli.scrub_award_locations()

    lat, lng, after = _row(pg_conn, row_id)
    assert after == kept_index
    assert h3_8_index_for(lat, lng) == kept_index
    # i.e. it was pulled back to the hex it claims to be in, not re-indexed to
    # the hex the stale coordinate was sitting in.
    assert h3_8_index_for(*far) != kept_index


def test_the_leaderboard_gives_the_same_answer_afterwards(pg_conn):
    """The promise, measured rather than argued.

    Every reader of this table selects `h3_8_index`, so a sweep that moves
    points within their own cells must change no answer. Taken before and
    after over the real SQL.
    """
    acct = _account(pg_conn, "answers")
    for action, at in (
        ("profile_completion", _HOME),
        ("nav_distance_bonus", _RIDE_START),
        ("qr_scan", _VEHICLE),
    ):
        _award(pg_conn, acct, action, at)

    def snapshot():
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT h3_8_index, account_id, SUM(points)
                FROM user_points GROUP BY h3_8_index, account_id
                ORDER BY h3_8_index, account_id
                """
            )
            return cur.fetchall()

    before = snapshot()
    cli.scrub_award_locations()
    assert snapshot() == before


def test_an_unreadable_index_is_reported_and_left_alone(pg_conn):
    # Guessing would make the row unreadable AND wrong, where leaving it is
    # merely wrong — and counted, so it cannot pass unnoticed.
    acct = _account(pg_conn, "corrupt")
    row_id = _award(pg_conn, acct, "profile_completion", _HOME)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE user_points SET h3_8_index = 1 WHERE id = %s", (row_id,))
    pg_conn.commit()

    result = cli.scrub_award_locations()

    assert result["unreadable_index"] == 1
    assert result["moved"] == 0
    assert _row(pg_conn, row_id)[:2] == _HOME
