"""sql/093 + sql/094 on real Postgres: the claim-shape CHECK holds the gate even
if a code path forgot it — and no longer demands a plan screenshot.
SKIPS unless VEO_TEST_PG_DSN is set."""

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


def test_a_planless_claim_is_accepted_now(pg):
    """sql/094: the plan screenshot is no longer part of the shape rule."""
    with pg.cursor() as cur:
        aid = _account(cur)
        # With a key (a claim filed in the 2026-10-06..07 window) and without
        # one (every claim since). Both legal.
        cur.execute(f"INSERT INTO discount_reports ({COLS}) VALUES "
                    "(%s, 'equity', 2, '1018354', 16, 500, '2026-09-29', 'k')", (aid,))
        cur.execute(f"INSERT INTO discount_reports ({COLS}) VALUES "
                    "(%s, 'equity', 2, '1018354', 16, 500, '2026-09-29', NULL)", (aid,))
    pg.commit()


def test_the_rest_of_the_gate_still_holds(pg):
    """Dropping one clause must not have loosened the others."""
    with pg.cursor() as cur:
        aid = _account(cur)
    pg.commit()
    # A non-numeric plate, no minutes, no cost, and no charge date: each is
    # still refused by the database, not only by the endpoint.
    for bad in (
        "(%s, 'equity', 2, 'ABC', 16, 500, '2026-09-29', NULL)",
        "(%s, 'equity', 2, '1018354', NULL, 500, '2026-09-29', NULL)",
        "(%s, 'equity', 2, '1018354', 16, NULL, '2026-09-29', NULL)",
        "(%s, 'equity', 2, '1018354', 16, 500, NULL, NULL)",
    ):
        with pg.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(f"INSERT INTO discount_reports ({COLS}) VALUES {bad}", (aid,))
        pg.rollback()
