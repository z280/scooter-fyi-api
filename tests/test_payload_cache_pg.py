"""src/payload_cache.py's UNLOGGED table (sql/103) against real Postgres:
an entry survives a process restart byte for byte, and a rewrite replaces
the variant's one row rather than adding another.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
"""

from __future__ import annotations

import gzip
import os
from contextlib import contextmanager

import pytest

psycopg = pytest.importorskip("psycopg")

from src import payload_cache as pc  # noqa: E402
from tests.test_fleet_reports_pg import SQL_DIR, _reachable  # noqa: E402

_KEY = "devices|test-payload-cache-pg"


@pytest.fixture()
def dsn(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — payload_cache Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for path in sorted(SQL_DIR.glob("*.sql")):
                cur.execute(path.read_text())
            cur.execute("DELETE FROM payload_cache WHERE cache_key = %s", (_KEY,))

    @contextmanager
    def _conn():
        with psycopg.connect(dsn) as c:
            yield c

    monkeypatch.setattr(pc, "connection", _conn)
    monkeypatch.setattr(pc, "ENABLED", True)
    pc.clear()
    yield dsn
    pc.clear()
    with psycopg.connect(dsn) as conn:
        conn.execute("DELETE FROM payload_cache WHERE cache_key = %s", (_KEY,))


def test_entry_round_trips_through_the_table(dsn):
    prefix = b'{"type":"FeatureCollection","features":[{"p":"Lunar \xf0\x9f\x90\xb8"}]'
    built = pc.get_or_build(_KEY, "cycle-1", "1-2-3", lambda: pc.make_entry(
        _KEY, "cycle-1", "1-2-3", prefix, {"device_count": 1, "snapshot_time": "t"}))
    pc.clear()                                       # a restarted worker
    loaded = pc._load(_KEY)
    assert loaded is not None
    assert (loaded.cycle_id, loaded.stamp, loaded.meta) == ("cycle-1", "1-2-3", built.meta)
    assert loaded.body == built.body and loaded.crc == built.crc and loaded.size == built.size
    assert gzip.decompress(pc.assemble(loaded, b"}")) == prefix + b"}"
    # ...and get_or_build uses it rather than building.
    got = pc.get_or_build(_KEY, "cycle-1", "1-2-3", lambda: pytest.fail("rebuilt"))
    assert got.body == built.body


def test_new_cycle_replaces_the_row(dsn):
    for cycle in ("cycle-1", "cycle-2"):
        pc.get_or_build(_KEY, cycle, "s", lambda c=cycle: pc.make_entry(_KEY, c, "s", b"{"))
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT cycle_id FROM payload_cache WHERE cache_key = %s", (_KEY,)).fetchall()
        persistence = conn.execute(
            "SELECT relpersistence FROM pg_class WHERE relname = 'payload_cache'").fetchone()
    assert rows == [("cycle-2",)]
    assert persistence == ("u",), "payload_cache must stay UNLOGGED"
