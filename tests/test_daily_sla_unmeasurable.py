"""daily_sla's half of the unmeasurable verdict (sql/084): the daily upsert
is the one writer of the averages, so it is the one place that can promise
a row never carries both a figure and "this could not be measured".

The behaviour against real Postgres is in tests/test_equity_unmeasurable_pg.py;
this pins the SQL the upsert sends, without a database.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

from src import daily_sla


class _Cur:
    def __init__(self, sink):
        self._sink = sink
        self.description = None

    def execute(self, sql, params=None):
        self._sink.append(" ".join(sql.split()))
        if sql.lstrip().startswith("SELECT"):
            names = ["snapshot_count"] + [f"avg_{f}" for f in daily_sla._AVG_FIELDS]

            class _C:
                def __init__(self, n):
                    self.name = n

            self.description = [_C(n) for n in names]

    def fetchone(self):
        return (0,) + (None,) * len(daily_sla._AVG_FIELDS)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_clearing_clause_drops_the_verdict_only_when_there_is_a_figure():
    assert daily_sla._clear_unmeasurable_clause() == (
        ", equity_unmeasurable_reason = CASE WHEN "
        "EXCLUDED.avg_percent_all_devices_equity IS NULL "
        "THEN daily_sla_compliance.equity_unmeasurable_reason ELSE NULL END"
    )


def test_the_upsert_carries_the_clearing_clause_but_never_inserts_a_verdict(monkeypatch):
    """ON CONFLICT is where an existing verdict meets a new average. A fresh
    INSERT has no verdict to keep, so the column is not in the insert list
    and defaults to NULL."""
    sent: list[str] = []

    class _Conn:
        def cursor(self):
            return _Cur(sent)

        def commit(self):
            pass

    @contextmanager
    def _connection():
        yield _Conn()

    monkeypatch.setattr(daily_sla, "connection", _connection)
    daily_sla.compute_for_date(date(2026, 8, 9))
    upsert = next(s for s in sent if s.startswith("INSERT INTO daily_sla_compliance"))
    insert_cols = upsert.split("(", 1)[1].split(")", 1)[0]
    assert "equity_unmeasurable_reason" not in insert_cols
    assert upsert.endswith(
        "computed_at = NOW(), equity_unmeasurable_reason = CASE WHEN "
        "EXCLUDED.avg_percent_all_devices_equity IS NULL "
        "THEN daily_sla_compliance.equity_unmeasurable_reason ELSE NULL END"
    )
