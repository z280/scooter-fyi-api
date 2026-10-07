"""sql/089 zeroed rentals_observed / rentals_no_go and kept recent_no_go_mask.

src/api_public.py loads each vehicle's mask with the counters. It used to
filter `WHERE rentals_observed > 0`, which after the reset would hide every
mask until the vehicle's next rental, and a vehicle whose only high_risk
reason is recent_rentals_no_go would read "ok" meanwhile.
"""

from __future__ import annotations

from contextlib import contextmanager

from src import api_public
from src.quality import compute_reliability_tier, recent_rentals_no_go


def _fake_db(monkeypatch, rows):
    seen = {}

    class _Cur:
        def execute(self, sql, params=None):
            seen["sql"] = sql
        def fetchall(self):
            return rows
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    @contextmanager
    def _conn():
        yield _Conn()

    monkeypatch.setattr(api_public, "connection", _conn)
    return seen


def test_the_query_keeps_a_mask_whose_counters_were_reset(monkeypatch):
    seen = _fake_db(monkeypatch, [("v1", 0, 0, 0b011)])
    out = api_public._rental_outcomes()
    assert "recent_no_go_mask <> 0" in seen["sql"]
    assert out == {"v1": (0, 0, 0b011)}


def test_such_a_vehicle_still_reads_high_risk():
    """Two of its last three rentals failed: high_risk, counters or not."""
    tier = compute_reliability_tier(
        number_failed_starts=0, first_observed_at_location=None,
        quality_designation="Good", has_negative_report=False,
        recent_rentals_no_go=recent_rentals_no_go(0b011),
    )
    assert tier == "high_risk"
