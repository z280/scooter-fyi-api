"""Reprocessing prior days' Equity Area compliance (src/equity_backfill.py).

The interesting parts of this job are the ones that decide what NOT to
write, so that is where the coverage is:

  * interval reconstruction — "the fleet at instant T" out of an
    append-only stop log, including the double-open-stop case that would
    otherwise count one vehicle twice;
  * the fidelity gate — a reconstruction that disagrees with the fleet
    count the cycle actually recorded is skipped, not written; and
  * the columns the rebuild deliberately leaves alone (sitting/standing,
    which device_history cannot reconstruct).

Postgres is faked here: every DB touch in `reprocess_date` goes through
`_load_snapshots` / `_load_stops` / `_write_metrics`, so patching those
three exercises the real decision logic without a database. The spatial
half is NOT faked — `tag_equity_membership` runs the production DuckDB
predicate against the real data/equity.geojson, because the whole point of
using ST_Within there is that a hand-rolled stand-in would disagree at the
boundary.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import equity_backfill as eb
from src.equity_backfill import Stop


REPO_DATA = Path(__file__).resolve().parents[1] / "data"

# Verified against data/equity.geojson: the interior of area EQ_001, and a
# Denver point (Washington Park) that is in the city but in no equity area.
IN_EQUITY = (39.785137, -104.826320)
OUT_OF_EQUITY = (39.700000, -104.970000)

T0 = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def _stop(vid, arrived, departed=None, ff="scooter", in_equity=False, at=IN_EQUITY):
    return Stop(
        vehicle_identifier=vid,
        arrived=arrived,
        departed=departed,
        lat=at[0],
        lon=at[1],
        form_factor=ff,
        in_equity=in_equity,
    )


# ---------------------------------------------------------------------------
# Interval reconstruction
# ---------------------------------------------------------------------------
def test_a_stop_covers_its_own_interval_half_open():
    s = _stop("v1", T0, T0 + timedelta(hours=1))
    assert not s.covers(T0 - timedelta(seconds=1))
    assert s.covers(T0)                                  # arrival is inclusive
    assert s.covers(T0 + timedelta(minutes=59))
    # Departure is exclusive: at the instant it moved, it is at the NEXT
    # stop, whose own row covers that instant. Counting both would
    # double-count the vehicle for exactly one cycle.
    assert not s.covers(T0 + timedelta(hours=1))


def test_an_open_stop_covers_everything_after_it():
    s = _stop("v1", T0, None)
    assert s.covers(T0 + timedelta(days=30))


def test_fleet_at_returns_only_the_stops_covering_that_instant():
    stops = [
        _stop("parked", T0 - timedelta(hours=2), None),
        _stop("left_earlier", T0 - timedelta(hours=5), T0 - timedelta(hours=3)),
        _stop("arrives_later", T0 + timedelta(hours=1), None),
    ]
    assert {s.vehicle_identifier for s in eb.fleet_at(stops, T0)} == {"parked"}


def test_fleet_at_counts_a_vehicle_once_even_with_two_open_stops():
    """A MOVED transition that failed to close its predecessor leaves two
    open rows for one vehicle. That must not become two devices in the
    compliance denominator — and the LATEST arrival is the live one."""
    old = _stop("v1", T0 - timedelta(hours=4), None, at=OUT_OF_EQUITY)
    new = _stop("v1", T0 - timedelta(minutes=5), None, in_equity=True, at=IN_EQUITY)
    fleet = eb.fleet_at([old, new], T0)
    assert len(fleet) == 1
    assert fleet[0].arrived == new.arrived
    assert fleet[0].in_equity is True


def test_fleet_at_is_order_independent():
    old = _stop("v1", T0 - timedelta(hours=4), None)
    new = _stop("v1", T0 - timedelta(minutes=5), None, in_equity=True)
    forward = eb.fleet_at([old, new], T0)
    backward = eb.fleet_at([new, old], T0)
    assert forward[0].arrived == backward[0].arrived == new.arrived


# ---------------------------------------------------------------------------
# Metric rebuild
# ---------------------------------------------------------------------------
def test_rebuild_metrics_counts_and_percentages():
    fleet = [
        _stop("a", T0, in_equity=True, ff="scooter"),
        _stop("b", T0, in_equity=True, ff="bicycle"),
        _stop("c", T0, in_equity=False, ff="scooter"),
        _stop("d", T0, in_equity=False, ff="scooter"),
    ]
    m = eb.rebuild_metrics(fleet)
    assert m["total_devices_equity"] == 2
    assert m["total_bike_equity"] == 1
    assert m["total_scooter_equity"] == 1
    # "% of ALL devices that are in equity areas" — 2 of 4.
    assert m["percent_all_devices_equity"] == 50.0
    # 1 of 1 bikes citywide; 1 of 3 scooters citywide.
    assert m["percent_all_bikes_equity"] == 100.0
    assert m["percent_all_scooters_equity"] == 33.33
    # "of the devices IN equity areas, what share are bikes" — 1 of 2.
    assert m["percent_bikes_equity"] == 50.0
    assert m["percent_scooters_equity"] == 50.0


def test_rebuild_metrics_reports_none_not_zero_for_an_empty_denominator():
    """An all-scooter fleet has no answer to "what share of bikes are in
    equity areas". compute.py's SQL returns NULL there (NULLIF); this must
    match, or a reprocessed day would average a fabricated 0.0 against
    live rows that averaged nothing."""
    fleet = [_stop("a", T0, in_equity=True, ff="scooter")]
    m = eb.rebuild_metrics(fleet)
    assert m["percent_all_bikes_equity"] is None
    assert m["percent_bikes_equity"] == 0.0   # 0 of 1 device in the area IS 0%


def test_rebuild_never_produces_a_sitting_or_standing_column():
    """device_history carries no vehicle_use_type, so the sitting/standing
    split cannot be rebuilt. Those columns must be left NULL rather than
    written as zero — see REBUILT_COLUMNS."""
    m = eb.rebuild_metrics([_stop("a", T0, in_equity=True)])
    assert set(m) == set(eb.REBUILT_COLUMNS)
    assert not [c for c in eb.REBUILT_COLUMNS if "sitting" in c or "standing" in c]


# ---------------------------------------------------------------------------
# Window math
# ---------------------------------------------------------------------------
def test_window_only_bounds_are_the_contractual_six_to_nine():
    from src.daily_sla import window_for_date

    d = date(2026, 8, 10)
    assert eb._bounds(d, window_only=True) == window_for_date(d)


def test_full_day_bounds_are_one_denver_calendar_day():
    start, end = eb._bounds(date(2026, 8, 10), window_only=False)
    assert (end - start) == timedelta(days=1)
    assert start.astimezone(eb.DENVER_TZ).hour == 0
    assert start.astimezone(eb.DENVER_TZ).date() == date(2026, 8, 10)


def test_full_day_bounds_survive_the_spring_forward_day():
    """2026-03-08 is 23 real hours in Denver. Using wall-clock midnight
    boundaries (not start + 24h) is what keeps a DST day from borrowing an
    hour of its neighbour's snapshots."""
    start, end = eb._bounds(date(2026, 3, 8), window_only=False)
    assert (end - start) == timedelta(hours=23)
    assert end.astimezone(eb.DENVER_TZ).date() == date(2026, 3, 9)


# ---------------------------------------------------------------------------
# The fidelity gate
# ---------------------------------------------------------------------------
def _patch_io(monkeypatch, snapshots, stops, sla_row=None):
    """Replace every Postgres touch in reprocess_date; collect the writes."""
    written: list[tuple] = []
    monkeypatch.setattr(eb, "_load_snapshots", lambda s, e: snapshots)
    monkeypatch.setattr(eb, "_load_stops", lambda s, e: stops)
    monkeypatch.setattr(eb, "tag_equity_membership", lambda st: list(st))
    monkeypatch.setattr(
        eb, "_write_metrics", lambda rows: (written.extend(rows), len(rows))[1]
    )
    monkeypatch.setattr(
        eb.daily_sla, "compute_for_date", lambda d: sla_row or {}
    )
    return written


def _snapshot(t, recorded):
    return {
        "cycle_id": uuid.uuid4(),
        "snapshot_time": t,
        "total_devices_denver": recorded,
        "total_bike_denver": 0,
        "total_scooter_denver": recorded,
    }


def test_a_faithful_reconstruction_is_written(monkeypatch):
    stops = [_stop(f"v{i}", T0 - timedelta(hours=1), None, in_equity=i < 4) for i in range(10)]
    written = _patch_io(
        monkeypatch,
        [_snapshot(T0, recorded=10)],
        stops,
        sla_row={"avg_percent_all_devices_equity": 40.0, "compliance_equity_pass": True},
    )
    r = eb.reprocess_date(date(2026, 8, 10))
    assert r.snapshots_written == 1
    assert r.snapshots_skipped_low_fidelity == 0
    assert written[0][1]["percent_all_devices_equity"] == 40.0
    assert r.compliance_equity_pass is True
    assert r.fidelity == [1.0]


def test_a_reconstruction_that_disagrees_with_the_record_is_skipped(monkeypatch):
    """The ghost-stop failure mode: device_history says 20 vehicles were
    parked, the cycle recorded 10. Half those stops belong to vehicles that
    left the feed and never came back. A 100%-drift reconstruction gets no
    vote — the column stays NULL rather than reporting a confident,
    wrong percentage."""
    stops = [_stop(f"v{i}", T0 - timedelta(hours=1), None, in_equity=i < 4) for i in range(20)]
    written = _patch_io(monkeypatch, [_snapshot(T0, recorded=10)], stops)
    r = eb.reprocess_date(date(2026, 8, 10))
    assert r.snapshots_written == 0
    assert r.snapshots_skipped_low_fidelity == 1
    assert written == []


def test_the_gate_is_two_sided(monkeypatch):
    """Under-reconstruction is just as disqualifying as over: a day whose
    history is partly missing would report a percentage over a fleet that
    is not the one the contract measured."""
    stops = [_stop(f"v{i}", T0 - timedelta(hours=1), None) for i in range(5)]
    _patch_io(monkeypatch, [_snapshot(T0, recorded=10)], stops)
    r = eb.reprocess_date(date(2026, 8, 10))
    assert r.snapshots_skipped_low_fidelity == 1


def test_drift_inside_the_tolerance_is_accepted(monkeypatch):
    """±10% is the default gate, so 19 reconstructed against 20 recorded
    passes — the reconstruction is never going to be exact and demanding
    exactness would reject every day."""
    stops = [_stop(f"v{i}", T0 - timedelta(hours=1), None, in_equity=i < 8) for i in range(19)]
    written = _patch_io(monkeypatch, [_snapshot(T0, recorded=20)], stops)
    r = eb.reprocess_date(date(2026, 8, 10))
    assert r.snapshots_written == 1
    assert written[0][1]["percent_all_devices_equity"] == pytest.approx(42.11, abs=0.01)


def test_a_snapshot_with_no_recorded_denominator_is_not_written(monkeypatch):
    """A row from before total_devices_denver was populated cannot be
    checked, so it cannot be trusted."""
    stops = [_stop("v1", T0 - timedelta(hours=1), None, in_equity=True)]
    written = _patch_io(monkeypatch, [_snapshot(T0, recorded=None)], stops)
    r = eb.reprocess_date(date(2026, 8, 10))
    assert r.snapshots_written == 0
    assert r.snapshots_skipped_no_history == 1
    assert written == []


def test_a_day_with_no_reconstructable_history_leaves_the_sla_row_alone(monkeypatch):
    """No writes means no reason to re-average — and re-running daily_sla
    on a day we changed nothing about would just churn computed_at."""
    called: list[date] = []
    _patch_io(monkeypatch, [_snapshot(T0, recorded=10)], [])
    monkeypatch.setattr(
        eb.daily_sla, "compute_for_date", lambda d: called.append(d) or {}
    )
    r = eb.reprocess_date(date(2026, 8, 10))
    assert r.snapshots_written == 0
    assert called == []


def test_a_day_with_no_snapshots_is_a_clean_no_op(monkeypatch):
    _patch_io(monkeypatch, [], [_stop("v1", T0, None)])
    r = eb.reprocess_date(date(2026, 8, 10))
    assert r.snapshots_considered == 0
    assert r.snapshots_written == 0


def test_result_summary_is_json_safe(monkeypatch):
    """run_backlog's return value lands in the job_runs ledger as JSON, so
    a date or a Decimal in there fails the job after the work succeeded."""
    import json

    stops = [_stop(f"v{i}", T0 - timedelta(hours=1), None, in_equity=i < 4) for i in range(10)]
    _patch_io(monkeypatch, [_snapshot(T0, recorded=10)], stops)
    d = eb.reprocess_date(date(2026, 8, 10)).as_dict()
    assert json.loads(json.dumps(d))["sla_date"] == "2026-08-10"


# ---------------------------------------------------------------------------
# The real spatial predicate
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not (REPO_DATA / "equity.geojson").exists(), reason="equity.geojson missing"
)
def test_membership_uses_the_real_map(monkeypatch):
    """Points are tagged against data/equity.geojson through the same
    ST_Within the live pipeline uses."""
    import src.config
    from src.config import BoundaryLayer

    layer = BoundaryLayer(
        region_category="equity_areas",
        region_type="equity",
        file=str(REPO_DATA / "equity.geojson"),
        name_prefix="EQ_",
        name_strategy="field",
        name_field="EQUITY_AREA_ID",
    )
    monkeypatch.setattr(eb, "official_layer", lambda: layer)

    tagged = eb.tag_equity_membership([
        _stop("inside", T0, None, at=IN_EQUITY),
        _stop("outside", T0, None, at=OUT_OF_EQUITY),
    ])
    by_id = {s.vehicle_identifier: s.in_equity for s in tagged}
    assert by_id == {"inside": True, "outside": False}


def test_membership_on_an_empty_list_never_opens_duckdb(monkeypatch):
    """A window with no stops is ordinary (an outage, a gap in history) and
    must not pay for a spatial session or trip over a missing file."""
    monkeypatch.setattr(
        eb, "official_layer", lambda: (_ for _ in ()).throw(AssertionError("called"))
    )
    assert eb.tag_equity_membership([]) == []


def test_official_layer_is_configured():
    """config.json must carry a boundary for the official group — without
    it the whole job is a silent no-op."""
    layer = eb.official_layer()
    assert layer.region_type == eb.OFFICIAL_GROUP
    assert layer.file.endswith("equity.geojson")
