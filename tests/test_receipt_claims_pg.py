"""sql/093 + sql/095 on real Postgres: the claim-shape CHECK holds the gate even
if a code path forgot it, and no longer demands a plan screenshot.
SKIPS unless VEO_TEST_PG_DSN is set.

NOTHING HERE COMMITS, and that is not tidiness — it is the whole reason CI went
red once. The `pg` fixture REPLAYS EVERY MIGRATION IN sql/ ON EVERY TEST, in
filename order. A committed row that is legal under the current schema but
illegal under an earlier migration therefore breaks that replay for every pg
test that runs afterwards, anywhere in the suite: sql/093's `ADD CONSTRAINT`
validates existing rows, and a planless v2 claim — exactly what sql/095 exists
to allow — makes it fail with "is violated by some row". An earlier draft of
this file committed one and took the whole `test` job down with it.

Asserting that an INSERT is ACCEPTED needs no commit, so these roll back
instead, leaving the database as they found it.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.test_ghost_stops_pg import pg  # noqa: E402,F401  (fixture)

COLS = ("account_id, zone_version, claim_version, vehicle_plate, trip_minutes, "
        "subtotal_cents, charge_date, plan_evidence_r2_key")


def _account(cur) -> int:
    cur.execute("INSERT INTO accounts (email) VALUES ('claims-pg@example.com') "
                "ON CONFLICT DO NOTHING")
    cur.execute("SELECT id FROM accounts WHERE email = 'claims-pg@example.com'")
    return cur.fetchone()[0]


def _insert(cur, aid: int, values: str) -> None:
    cur.execute(f"INSERT INTO discount_reports ({COLS}) VALUES {values}", (aid,))


def test_a_planless_claim_is_accepted_now(pg):
    """sql/095: the plan screenshot is no longer part of the shape rule."""
    try:
        with pg.cursor() as cur:
            aid = _account(cur)
            # With a key (a claim filed in the 2026-10-06..07 window) and
            # without one (every claim since). Both legal.
            _insert(cur, aid, "(%s, 'equity', 2, '1018354', 16, 500, '2026-09-29', 'k')")
            _insert(cur, aid, "(%s, 'equity', 2, '1018354', 16, 500, '2026-09-29', NULL)")
    finally:
        pg.rollback()


@pytest.mark.parametrize("bad,why", [
    ("(%s, 'equity', 2, 'ABC', 16, 500, '2026-09-29', NULL)", "a non-numeric plate"),
    ("(%s, 'equity', 2, '1018354', 0, 500, '2026-09-29', NULL)", "minutes below 1"),
    ("(%s, 'equity', 2, '1018354', 601, 500, '2026-09-29', NULL)", "minutes above 600"),
    ("(%s, 'equity', 2, '1018354', 16, NULL, '2026-09-29', NULL)", "no cost at all"),
    ("(%s, 'equity', 2, '1018354', 16, 500, NULL, NULL)", "no charge date"),
    # THE NULL HOLE sql/095 CLOSED. A CHECK accepts a NULL result, so
    # `trip_minutes BETWEEN 1 AND 600` with NULL minutes made the whole v2
    # conjunction NULL and the row was accepted — `INSERT 0 1`, verified against
    # Postgres 16. Same for a NULL plate through the regex. These two fail only
    # because sql/095 added the explicit IS NOT NULL tests.
    ("(%s, 'equity', 2, NULL, 16, 500, '2026-09-29', NULL)", "a NULL plate"),
    ("(%s, 'equity', 2, '1018354', NULL, 500, '2026-09-29', NULL)", "NULL minutes"),
])
def test_the_database_refuses_an_unusable_claim(pg, bad, why):
    """Not only the endpoint: the gate is in the schema too, which is the point
    sql/093 was making and could not quite keep."""
    try:
        with pg.cursor() as cur:
            aid = _account(cur)
        with pg.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation):
                _insert(cur, aid, bad)
    finally:
        pg.rollback()
