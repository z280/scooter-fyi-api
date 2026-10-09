"""POST /api/v1/dibs/{id}/release against a real database.

tests/test_dibs.py proves this through a fake cursor, and for most of the
endpoint that is enough. It cannot prove the one thing that matters most
here, because the fake implements the liveness check in Python: the guard is
the statement's own `AND expires_at > NOW()`, and removing it from the SQL
leaves every fake-cursor test passing. Found exactly that way, by mutation.

What the guard buys, now that `released_at` exists: a release call arriving
AFTER the claim ran out must not stamp it. If it did, the certificate would
say the rider handed the scooter back when in fact their time expired — the
page crediting somebody for something they did not do, which is worse than
the omission sql/098 set out to fix.

SKIPS unless VEO_TEST_PG_DSN names a reachable, migratable database. NEVER
point it at production: the fixture replays every migration and deletes its
own dibs rows.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

psycopg = pytest.importorskip("psycopg")

from src import api_dibs, ratelimit  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_VID = "eeee000000000000"


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture()
def client(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — dibs release Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()

    def _clean() -> None:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dibs WHERE vehicle_identifier = %s", (_VID,))
        conn.commit()

    _clean()

    @contextmanager
    def _conn():
        yield conn

    monkeypatch.setattr(api_dibs, "connection", _conn)
    # The rate limiter writes to its own table and is not what this is about.
    monkeypatch.setattr(api_dibs, "enforce", lambda cur, **kw: None)

    app = FastAPI()
    app.include_router(api_dibs.router)
    c = TestClient(app)
    c.conn = conn  # type: ignore[attr-defined]
    yield c
    _clean()
    conn.close()


def _claim(conn, dibs_id: str, *, minutes_left: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dibs (id, vehicle_identifier, vehicle_name, claimed_by,
                              claimed_at, expires_at)
            VALUES (%s, %s, 'Perseus 619', 'Zach',
                    NOW() - interval '5 minutes',
                    NOW() + make_interval(secs => %s))
            """,
            (dibs_id, _VID, minutes_left * 60),
        )
    conn.commit()


def _row(conn, dibs_id: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT expires_at, released_at FROM dibs WHERE id = %s", (dibs_id,)
        )
        return cur.fetchone()


def test_releasing_a_live_claim_stamps_both(client):
    _claim(client.conn, "rel-live", minutes_left=20)

    assert client.post("/api/v1/dibs/rel-live/release").json() == {"released": True}

    expires_at, released_at = _row(client.conn, "rel-live")
    assert released_at is not None
    # Expired by the same statement, so it drops out of every live-claim query
    # and the SMS watch at the same instant it is recorded as given back.
    assert expires_at <= datetime.now(timezone.utc) + timedelta(seconds=1)


def test_releasing_an_expired_claim_stamps_nothing(client):
    """THE MUTATION THIS FILE EXISTS FOR. The fake-cursor suite cannot see it.

    Without `AND expires_at > NOW()` the certificate would credit a rider with
    handing back a scooter whose claim had already run out.
    """
    _claim(client.conn, "rel-dead", minutes_left=-5)

    assert client.post("/api/v1/dibs/rel-dead/release").json() == {"released": False}

    _, released_at = _row(client.conn, "rel-dead")
    assert released_at is None
    assert "expired at" in client.get("/dibs/rel-dead").text
    assert "gave them up" not in client.get("/dibs/rel-dead").text


def test_a_second_release_does_not_move_the_moment(client):
    """Idempotent, and the recorded time stays the first one — the certificate
    says when they gave it back, not when something last touched the row."""
    _claim(client.conn, "rel-twice", minutes_left=20)
    client.post("/api/v1/dibs/rel-twice/release")
    _, first = _row(client.conn, "rel-twice")

    assert client.post("/api/v1/dibs/rel-twice/release").json() == {"released": False}

    _, second = _row(client.conn, "rel-twice")
    assert second == first


def test_the_page_reads_gave_them_up_off_a_real_row(client):
    """End to end against the database, since the page's branch now turns on
    a column rather than on arithmetic the fake could fake."""
    _claim(client.conn, "rel-page", minutes_left=20)
    client.post("/api/v1/dibs/rel-page/release")

    html = client.get("/dibs/rel-page").text
    assert "gave them up at" in html
    assert "free for anyone." in html
    assert "null and void" not in html
    assert client.get("/api/v1/dibs/rel-page").json()["released_at"] is not None
