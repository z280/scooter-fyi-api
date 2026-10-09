"""The fleet's headline number: what share of rentals never moved.

The arithmetic is tested through `summarize_rows`, which takes the rows the SQL
produces and needs no database — the same split `parking_response` uses. What
is pinned here is the judgement rather than the GROUP BY: the sample floor, the
ordering, and above all the three self-describing fields in the payload. Those
exist because a percentage published without its window, its radius and its
sample size is the kind of number that gets quoted back at you with none of
those attached.
"""

from __future__ import annotations

from src import fleet_outcomes
from src.fleet_outcomes import MIN_RENTALS_FOR_RATE as FLOOR

R = 16.0


def model(name: str, rentals: int, no_gos: int, vehicles: int = 10):
    return {
        "model": name,
        "rentals": rentals,
        "no_gos": no_gos,
        "vehicles": vehicles,
    }


def summarize(rows):
    return fleet_outcomes.summarize_rows(rows, R)


class TestTheHeadlineRate:
    def test_is_the_share_of_rentals_that_went_nowhere(self):
        out = summarize([model("Cosmo", 1000, 91)])
        assert out["rentals"] == 1000
        assert out["no_gos"] == 91
        assert out["no_go_rate"] == 0.091

    def test_pools_models_rather_than_averaging_their_rates(self):
        # A mean of rates would weight a 300-rental model equally with a
        # 30,000-rental one and report a fleet nobody rides. The fleet rate is
        # one division over the pooled counts.
        out = summarize([model("Cosmo", 30_000, 1_500), model("Rover", 300, 150)])
        assert out["no_gos"] == 1_650
        assert out["rentals"] == 30_300
        assert out["no_go_rate"] == round(1_650 / 30_300, 4)
        # Not the 0.3 that averaging 0.05 and 0.5 would produce.
        assert out["no_go_rate"] < 0.1


class TestTheSampleFloor:
    def test_withholds_a_rate_under_the_floor_but_keeps_the_counts(self):
        # The model must not vanish: the client's copy for this case is "not
        # enough rides yet", which it cannot write without the counts.
        out = summarize([model("Rare", FLOOR - 1, 7)])
        only = out["by_model"][0]
        assert only["no_go_rate"] is None
        assert only["rentals"] == FLOOR - 1
        assert only["no_gos"] == 7
        assert out["min_rentals_for_rate"] == FLOOR

    def test_publishes_at_exactly_the_floor(self):
        out = summarize([model("Just", FLOOR, FLOOR // 2)])
        assert out["by_model"][0]["no_go_rate"] == 0.5

    def test_an_empty_fleet_reports_no_rate_rather_than_dividing_by_zero(self):
        out = summarize([])
        assert out["rentals"] == 0
        assert out["no_gos"] == 0
        assert out["no_go_rate"] is None
        assert out["by_model"] == []


class TestOrdering:
    def test_ranks_the_worst_publishable_model_first(self):
        # Ordering by volume buries the finding; the drawer leads with it.
        out = summarize(
            [
                model("Big", 50_000, 500),      # 1%
                model("Bad", 1_000, 300),       # 30%
                model("Middling", 5_000, 500),  # 10%
            ]
        )
        assert [m["model"] for m in out["by_model"]] == ["Bad", "Middling", "Big"]

    def test_sinks_the_models_with_no_publishable_rate_below_every_real_one(self):
        # A null rate sorting as 0 would read as the fleet's BEST model; a null
        # sorting as 1 would invent the fleet's worst. It sorts below both.
        out = summarize([model("Rare", 5, 5), model("Known", 10_000, 100)])
        assert [m["model"] for m in out["by_model"]] == ["Known", "Rare"]
        assert out["by_model"][-1]["no_go_rate"] is None


class TestThePayloadDescribesItself:
    """Each of these is a field a reader would otherwise have to assume."""

    def test_states_that_the_window_opens_at_the_reset(self):
        # The owner reset the counters (sql/089, 2026-10-07) so they describe
        # one radius. An unlabelled rate reads as "now"; this one is every
        # rental since the reset, and says when that was.
        out = summarize([model("Cosmo", 1_000, 90)])
        assert out["window"] == "since_reset"
        assert out["counted_since"] == "sql/089"
        assert "counted_since_at" in out

    def test_carries_when_the_reset_ran(self):
        from src.fleet_outcomes import summarize_rows
        out = summarize_rows([model("Cosmo", 1_000, 90)], R,
                             counted_since_at="2026-10-07T18:00:00+00:00")
        assert out["counted_since_at"] == "2026-10-07T18:00:00+00:00"

    def test_the_named_migration_exists(self):
        from pathlib import Path
        from src import fleet_outcomes
        sql = Path(__file__).resolve().parents[1] / "sql" / fleet_outcomes.COUNTED_SINCE_MIGRATION
        assert sql.is_file()
        text = sql.read_text()
        import re
        assert re.search(r"rentals_observed\s*=\s*0", text)
        assert re.search(r"rentals_no_go\s*=\s*0", text)
        # Table lock first: no row-by-row contention with the live ingest.
        assert "LOCK TABLE device_state IN EXCLUSIVE MODE" in text
        # The rolling signal is deliberately left alone.
        assert "recent_no_go_mask =" not in text

    def test_states_the_radius_it_was_counted_at(self):
        # Three circles exist in this codebase (ANALYTICS_PLAN §0.2: 16 m in
        # config.json, 25 m in sql/072's prose, 50 m in device_state.py). The
        # number travels with the one it was actually measured against instead
        # of leaving the reader to pick.
        assert summarize([model("Cosmo", 1_000, 90)])["radius_meters"] == R

    def test_carries_the_radius_from_config_rather_than_a_restated_constant(self):
        # If the ingest's threshold changes, the published figure's label has
        # to change with it — so it is read, not duplicated.
        from src.config import load

        assert (
            fleet_outcomes._radius_meters()
            == load().device_tracking.stationary_threshold_meters
        )

    def test_counts_vehicles_so_a_rate_can_be_read_against_a_fleet_size(self):
        out = summarize([model("A", 1_000, 10, vehicles=40), model("B", 1_000, 10, vehicles=2)])
        assert out["vehicles"] == 42


class TestDefensiveArithmetic:
    def test_a_model_with_no_no_gos_reports_zero_not_null(self):
        # Zero is a finding — "this model always goes" — and must not be
        # confused with the withheld null that means "we don't know yet".
        out = summarize([model("Perfect", 1_000, 0)])
        assert out["by_model"][0]["no_go_rate"] == 0.0

    def test_rounds_to_four_places_so_the_client_never_prints_float_noise(self):
        out = summarize([model("Odd", 3_000, 1_000)])
        assert out["by_model"][0]["no_go_rate"] == 0.3333


# ---------------------------------------------------------------------------
# The endpoint: wiring, degradation, and the conditional-GET flow
# ---------------------------------------------------------------------------
from contextlib import contextmanager  # noqa: E402

import pytest  # noqa: E402
from fastapi import Response  # noqa: E402
from starlette.requests import Request  # noqa: E402

from src import api_public  # noqa: E402

# (model, rentals, no_gos, vehicles, stayed_rentals, stayed) — the shape of
# the GROUP BY, verified against a real Postgres `device_state` while this was
# written. The last two are sql/098's, over their own (shorter) window.
_ROWS = [("Cosmo", 1500, 131, 2, 600, 12), ("Rover", 300, 150, 1, 100, 9)]


class _FakeCur:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self._last = sql
        if "schema_migrations" in sql:
            # The windows' starts: when the sql/089 reset ran, and when sql/098
            # started the stayed counter.
            assert params in ((fleet_outcomes.COUNTED_SINCE_MIGRATION,),
                              (fleet_outcomes.STAYED_COUNTED_SINCE_MIGRATION,))
            self._params = params
            return
        assert "device_state" in sql
        # The aggregation belongs in the database: 8k devices must not cross
        # the wire to be summed in Python.
        assert "SUM(" in sql

    def fetchone(self):
        from datetime import datetime, timezone
        if self._params == (fleet_outcomes.STAYED_COUNTED_SINCE_MIGRATION,):
            return (datetime(2026, 10, 9, 20, 30, tzinfo=timezone.utc),)
        return (datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc),)

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return _FakeCur(self._rows)


@pytest.fixture
def db(monkeypatch):
    def _install(rows=_ROWS, boom=False):
        @contextmanager
        def _conn():
            if boom:
                raise RuntimeError("pool exhausted")
            yield _FakeConn(rows)

        monkeypatch.setattr(fleet_outcomes, "connection", _conn)

    return _install


def _request(headers: dict[str, str] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/fleet/outcomes",
            "headers": [
                (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
            ],
            "query_string": b"",
        }
    )


def _call(headers=None):
    return api_public.fleet_outcomes(_request(headers), Response())


class TestTheEndpoint:
    def test_serves_the_aggregate_from_the_database(self, db):
        db()
        out = _call()
        assert out["rentals"] == 1800
        assert out["no_gos"] == 281
        assert [m["model"] for m in out["by_model"]] == ["Rover", "Cosmo"]
        # The window's start is read from the database, not hardcoded.
        assert out["counted_since_at"] == "2026-10-07T18:00:00+00:00"

    def test_labels_the_payload_with_the_live_config_radius(self, db):
        from src.config import load

        db()
        assert _call()["radius_meters"] == load().device_tracking.stationary_threshold_meters

    def test_a_database_failure_empties_the_drawer_rather_than_500ing(self, db):
        # An empty stats page is a worse page; a failed request is a broken
        # one. The client tells the two apart by `rentals` being zero.
        db(boom=True)
        out = _call()
        assert out["rentals"] == 0
        assert out["no_go_rate"] is None
        assert out["by_model"] == []

    def test_caches_and_answers_a_matching_if_none_match_with_304(self, db):
        db()
        first = Response()
        api_public.fleet_outcomes(_request(), first)
        etag = first.headers["ETag"]
        assert first.headers["Cache-Control"] == "public, max-age=300"

        again = _call({"if-none-match": etag})
        assert isinstance(again, Response)
        assert again.status_code == 304

    def test_the_etag_moves_when_a_counter_does(self, db):
        # The counters only climb, so the totals are a sufficient version —
        # but a stale drawer after a rental is counted is the bug this guards.
        db()
        a = Response()
        api_public.fleet_outcomes(_request(), a)
        db(rows=[("Cosmo", 1501, 131, 2, 601, 12), ("Rover", 300, 150, 1, 100, 9)])
        b = Response()
        api_public.fleet_outcomes(_request(), b)
        assert a.headers["ETag"] != b.headers["ETag"]

    def test_the_etag_carries_no_comma(self, db):
        # _if_none_match_hit splits the request header on commas, so an ETag
        # containing one can never match itself.
        db()
        r = Response()
        api_public.fleet_outcomes(_request(), r)
        assert "," not in r.headers["ETag"]


# --- sql/098: never left the spot -------------------------------------------

def stayed_model(name, rentals, no_gos, stayed_rentals, stayed, vehicles=10):
    return {**model(name, rentals, no_gos, vehicles),
            "stayed_rentals": stayed_rentals, "stayed": stayed}


class TestNeverLeftTheSpot:
    def test_is_published_beside_the_no_go_rate_with_its_own_denominator(self):
        out = fleet_outcomes.summarize_rows(
            [stayed_model("Cosmo", 3000, 255, 1000, 21)], R,
            counted_since_at="2026-10-07T18:00:00+00:00",
            stayed_counted_since="2026-10-09T20:30:00+00:00")
        # The no-go figure is untouched.
        assert (out["rentals"], out["no_gos"], out["no_go_rate"]) == (3000, 255, 0.085)
        assert out["radius_meters"] == R
        # stayed divides by rentals since ITS counter started, never by the
        # longer-running rentals_observed.
        assert (out["stayed_rentals"], out["stayed"], out["stayed_rate"]) == (1000, 21, 0.021)
        assert out["stayed_radius_meters"] == 50.0
        assert out["stayed_window"] == "since_stayed_counter"
        assert out["stayed_counted_since"] == "2026-10-09T20:30:00+00:00"
        assert out["stayed_counted_since_migration"] == "sql/098"
        assert out["stayed_definition"] == (
            "never left the spot: the vehicle never got more than 50 m from where "
            "it was unlocked, and was released there")
        (m,) = out["by_model"]
        assert (m["stayed_rentals"], m["stayed"], m["stayed_rate"]) == (1000, 21, 0.021)

    def test_withholds_the_rate_under_the_floor(self):
        out = summarize([stayed_model("Cosmo", 5000, 400, FLOOR - 1, 3)])
        assert out["stayed_rate"] is None and out["stayed"] == 3
        assert out["no_go_rate"] is not None

    def test_the_radius_is_the_ingest_in_place_circle(self):
        from src.device_state import IN_PLACE_RADIUS_M
        assert summarize([])["stayed_radius_meters"] == IN_PLACE_RADIUS_M

    def test_the_named_migration_exists(self):
        from pathlib import Path
        sql = Path(__file__).resolve().parents[1] / "sql"
        assert (sql / fleet_outcomes.STAYED_COUNTED_SINCE_MIGRATION).is_file()


class TestTheEndpointStayed:
    def test_serves_stayed_from_the_database(self, db):
        db()
        out = _call()
        assert (out["stayed_rentals"], out["stayed"]) == (700, 21)
        assert out["stayed_rate"] == 0.03
        assert out["stayed_counted_since"] == "2026-10-09T20:30:00+00:00"
        # The no-go window is still the sql/089 one.
        assert out["counted_since_at"] == "2026-10-07T18:00:00+00:00"

    def test_the_etag_moves_when_only_stayed_does(self, db):
        db()
        a = Response()
        api_public.fleet_outcomes(_request(), a)
        db(rows=[("Cosmo", 1500, 131, 2, 600, 13), ("Rover", 300, 150, 1, 100, 9)])
        b = Response()
        api_public.fleet_outcomes(_request(), b)
        assert a.headers["ETag"] != b.headers["ETag"]
        assert "," not in b.headers["ETag"]
