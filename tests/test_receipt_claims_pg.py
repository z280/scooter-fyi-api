"""sql/093 on real Postgres: the claim-shape CHECK holds the gate even if a
code path forgot it. SKIPS unless VEO_TEST_PG_DSN is set."""

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


def test_a_complete_claim_is_accepted_and_a_planless_one_is_not(pg):
    with pg.cursor() as cur:
        aid = _account(cur)
        cur.execute(f"INSERT INTO discount_reports ({COLS}) VALUES "
                    "(%s, 'equity', 2, '1018354', 16, 500, '2026-09-29', 'k')", (aid,))
    pg.commit()
    with pg.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(f"INSERT INTO discount_reports ({COLS}) VALUES "
                        "(%s, 'equity', 2, '1018354', 16, 500, '2026-09-29', NULL)", (aid,))
    pg.rollback()
    with pg.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(f"INSERT INTO discount_reports ({COLS}) VALUES "
                        "(%s, 'equity', 2, 'ABC', 16, 500, '2026-09-29', 'k')", (aid,))
    pg.rollback()
