"""The equity cut's SQL against real Postgres (rental_outcomes_hourly, sql/090).
SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("psycopg")

from src import fleet_equity  # noqa: E402
from tests.test_ghost_stops_pg import pg  # noqa: E402,F401  (fixture)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def test_window_radius_and_origin_flag_are_applied_in_sql(pg, monkeypatch):
    with pg.cursor() as cur:
        cur.execute("DELETE FROM rental_outcomes_hourly")
        rows = [
            # (hour, cell, model, radius, rentals, no_gos, no_gos_max, max_known, origin_unknown)
            ("2026-10-09T10:00:00+00", 1, "Cosmo", 25, 300, 30, 20, 300, 0),
            ("2026-10-09T11:00:00+00", 1, "Rover", 25, 100, 10, 5, 100, 0),
            ("2026-10-09T11:00:00+00", 2, "Cosmo", 25, 400, 20, 10, 400, 0),
            ("2026-10-09T12:00:00+00", 1, "Halo", 25, 50, 5, 0, 0, 3),     # origin unknown
            ("2026-10-09T12:00:00+00", 2, "Cosmo", 16, 999, 999, 0, 0, 0),  # another radius
            ("2026-09-01T12:00:00+00", 2, "Cosmo", 25, 777, 7, 0, 0, 0),    # outside window
        ]
        cur.executemany(
            "INSERT INTO rental_outcomes_hourly (hour, h3_9, model, radius_m, rentals, no_gos, "
            "no_gos_max, max_known, origin_unknown) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    pg.commit()
    from contextlib import contextmanager

    @contextmanager
    def _conn():
        yield pg

    monkeypatch.setattr(fleet_equity, "connection", _conn)
    monkeypatch.setattr(fleet_equity, "classify_cell", lambda c: {1: "inside", 2: "outside"}[c])
    out = fleet_equity.summarize("7d", now=NOW)
    assert out["inside"]["rentals"] == 400 and out["inside"]["no_gos"] == 40
    assert out["outside"]["rentals"] == 400 and out["outside"]["no_gos"] == 20
    assert out["origin_unknown_excluded"] == 50
    assert out["difference_points"] == 5.0
