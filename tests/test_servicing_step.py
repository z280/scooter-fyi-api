"""src/servicing.step — the one charge rule (sql/105 + sql/106) — as a pure
function: swaps, rebounds, the settled reading, and the archive replay that
shares it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import servicing as sv
from src.fleet_reports import full_battery_meters, service_from_meters
from src.quality import full_charge_range_meters

FULL = full_charge_range_meters()
FULL95 = full_battery_meters()
LOW = service_from_meters()
T0 = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)
A = (39.74, -104.99)


def run(readings, state=None):
    """readings: (range, reserved, minutes_after_T0, (lat, lon)?)"""
    s, hits, prev_res = state or sv.ChargeState(), [], False
    for item in readings:
        r, res, mins = item[:3]
        lat, lon = item[3] if len(item) > 3 else A
        s, hit = sv.step(s, r=r, t=T0 + timedelta(minutes=mins), lat=lat, lon=lon,
                         reserved=res, in_rental_before=prev_res)
        prev_res = res
        if hit:
            hits.append(hit)
    return s, hits


def test_a_swap_from_low_to_full_is_one_servicing_with_where_it_waited():
    s, hits = run([(3000, False, 0), (2500, False, 60), (FULL, False, 62, (39.7401, -104.99))])
    assert len(hits) == 1
    h = hits[0]
    assert (h.low, h.full, h.low_at) == (2500, FULL, T0 + timedelta(minutes=60))
    assert h.moved_m is not None and h.moved_m < sv.IN_PLACE_M
    assert s.low == FULL                                   # reset after full


def test_a_rides_sag_and_rebound_is_not_servicing():
    _, hits = run([(21947, False, 0), (10153, True, 2), (7514, True, 4),
                   (11231, False, 6), (17876, False, 8)])
    assert hits == []


def test_a_full_start_ridden_and_rebounding_to_full_is_not_servicing():
    _, hits = run([(FULL, False, 0), (41000, True, 2), (38000, False, 4), (FULL, False, 6)])
    assert hits == []


def test_settled_reading_holds_the_high_after_a_release_then_expires():
    s, _ = run([(30000, False, 0), (20000, True, 2), (12000, False, 4)])
    assert s.settled == 12000 and s.settling_until == T0 + timedelta(minutes=4 + sv.SETTLE_MINUTES)
    s, _ = run([(18000, False, 6), (16000, False, 8)], state=s)
    assert s.settled == 18000                              # highest since the release
    now = T0 + timedelta(minutes=8)
    assert sv.settled_range(16000, s.settled, s.settling_until, now) == 18000
    later = T0 + timedelta(minutes=4 + sv.SETTLE_MINUTES + 1)
    assert sv.settled_range(16000, s.settled, s.settling_until, later) == 16000


def test_the_release_reading_is_not_a_parked_low():
    s, hits = run([(FULL, False, 0), (30000, True, 2), (5000, False, 4), (FULL, False, 6)])
    assert hits == [] and s.low == FULL


def test_a_reservation_clears_the_settling_window():
    s, _ = run([(30000, False, 0), (20000, True, 2), (12000, False, 4), (11000, True, 6)])
    assert (s.settled, s.settling_until) == (None, None)


def test_replay_carries_state_across_batches_and_marks_absence():
    states, prev, seen = {}, {}, {}
    t = T0
    b1 = [("v1", t, 4000, False, *A, "Cosmo", 1)]
    b2 = [("v1", t + timedelta(hours=5), FULL, False, *A, "Cosmo", 1)]
    assert sv.replay_charge(b1, states, prev, seen, timedelta(hours=1)) == []
    rows = sv.replay_charge(b2, states, prev, seen, timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0]["absent"] is True and rows[0]["src"] == "backfill"
    assert rows[0]["low"] == 4000 and rows[0]["in_place"] is True


def test_the_archive_replay_reads_a_parquet_file_across_days(tmp_path):
    """The DuckDB half of backfill_service_events on a synthetic archive file:
    a low on one day and its full reading the next still pair up."""
    import duckdb

    path = tmp_path / "raw.parquet"
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone='UTC'")
    con.execute(f"""
        COPY (SELECT * FROM (VALUES
            ('v1', TIMESTAMPTZ '2026-10-08 23:50:00+00', 3000, FALSE, 39.74, -104.99, 'Cosmo', 1::BIGINT),
            ('v1', TIMESTAMPTZ '2026-10-09 00:10:00+00', {FULL}, FALSE, 39.74, -104.99, 'Cosmo', 1::BIGINT),
            ('v2', TIMESTAMPTZ '2026-10-08 12:00:00+00', 20000, FALSE, 39.75, -104.98, 'Astro', 2::BIGINT),
            ('v2', TIMESTAMPTZ '2026-10-08 12:02:00+00', 9000, TRUE, 39.75, -104.98, 'Astro', 2::BIGINT),
            ('v2', TIMESTAMPTZ '2026-10-08 12:04:00+00', 18000, FALSE, 39.76, -104.98, 'Astro', 2::BIGINT)
        ) t(vehicle_identifier, snapshot_time, current_range_meters, is_reserved,
            latitude, longitude, vehicle_model_name, h3_9_index))
        TO '{path}' (FORMAT PARQUET)""")
    days = list(sv.replay_archive_file(con, str(path), {}, {}, {}, timedelta(hours=1)))
    assert [d for d, _ in days] == ["2026-10-08", "2026-10-09"]
    events = [e for _, ev in days for e in ev]
    assert [(e["v"], e["low"], e["full"]) for e in events] == [("v1", 3000, FULL)]
