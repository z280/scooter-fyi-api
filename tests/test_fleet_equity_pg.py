"""The equity cut's SQL against real Postgres (rental_outcomes_hourly,
sql/090 + sql/092). SKIPS unless VEO_TEST_PG_DSN points at a reachable,
migratable database."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

pytest.importorskip("psycopg")

from src import fleet_equity  # noqa: E402
from tests.test_ghost_stops_pg import pg  # noqa: E402,F401  (fixture)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def test_window_radius_and_areas_are_applied_in_sql(pg, monkeypatch):
    with pg.cursor() as cur:
        cur.execute("DELETE FROM rental_outcomes_hourly")
        rows = [
            # (hour, cell, model, radius, area, rentals, no_gos, no_gos_max, max_known, origin_unknown)
            ("2026-10-09T10:00:00+00", 1, "Cosmo", 25, "EQ_003", 300, 30, 20, 300, 0),
            # Same hour/cell/model/radius, different area: the sql/092 key allows it.
            ("2026-10-09T10:00:00+00", 1, "Cosmo", 25, "outside", 100, 5, 2, 100, 0),
            ("2026-10-09T11:00:00+00", 2, "Rover", 25, "EQ_018", 100, 10, 5, 100, 0),
            ("2026-10-09T11:00:00+00", 3, "Cosmo", 25, "outside", 300, 15, 10, 300, 0),
            ("2026-10-09T12:00:00+00", 1, "Halo", 25, "unknown", 50, 5, 0, 0, 50),
            ("2026-10-09T12:00:00+00", 9, "Cosmo", 25, "outside_city", 40, 2, 0, 0, 0),
            ("2026-10-09T12:00:00+00", 2, "Cosmo", 16, "outside", 999, 999, 0, 0, 0),   # another radius
            ("2026-09-01T12:00:00+00", 2, "Cosmo", 25, "outside", 777, 7, 0, 0, 0),     # outside window
            ("2026-10-07T04:00:00+00", 2, "Cosmo", 25, "unrecorded", 452, 26, 7, 452, 0),
        ]
        cur.executemany(
            "INSERT INTO rental_outcomes_hourly (hour, h3_9, model, radius_m, equity_area, rentals, "
            "no_gos, no_gos_max, max_known, origin_unknown) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    pg.commit()

    @contextmanager
    def _conn():
        yield pg

    monkeypatch.setattr(fleet_equity, "connection", _conn)
    out = fleet_equity.summarize("7d", now=NOW)
    assert out["status"] == "ok"
    assert out["inside"]["rentals"] == 400 and out["inside"]["ended_within_radius"] == 40
    assert out["inside"]["areas_represented"] == 2
    assert out["outside"]["rentals"] == 400 and out["outside"]["ended_within_radius"] == 20
    assert out["excluded"] == {"unknown_origin": 50, "outside_city": 40, "unrecorded": 452}
    assert out["inside"]["cells"] == 2 and out["outside"]["cells"] == 2
    assert out["difference_points"] == 5.0
    # data_since ignores pre-sql/092 rows.
    assert out["data_since"].startswith("2026-09-01T12:00:00")


def test_the_new_key_has_no_default_area(pg):
    import psycopg
    with pg.cursor() as cur:
        with pytest.raises(psycopg.errors.NotNullViolation):
            cur.execute("INSERT INTO rental_outcomes_hourly (hour, h3_9, model, radius_m, rentals) "
                        "VALUES (now(), 1, 'Cosmo', 25, 1)")
    pg.rollback()
