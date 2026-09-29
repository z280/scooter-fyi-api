"""src/ghost_stops.py and its CLI door, without a database.

tests/test_ghost_stops_pg.py covers the real cleanup and dry run.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from src import cli, ghost_stops
from src.equity_backfill import Stop

_T = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _stop(vid, arrived, departed=None):
    return Stop(vehicle_identifier=vid, arrived=arrived, departed=departed,
                lat=39.7, lon=-104.9, form_factor="scooter")


def test_apply_closures_touches_only_the_matching_open_stop():
    gone = _T - timedelta(days=3)
    stops = [
        _stop("a", _T - timedelta(days=5)),                     # the ghost
        _stop("a", _T - timedelta(days=9), _T - timedelta(days=5)),  # its closed predecessor
        _stop("b", _T - timedelta(days=5)),                     # still in the feed
    ]
    after = ghost_stops.apply_closures(stops, {("a", _T - timedelta(days=5)): gone})
    assert [s.departed for s in after] == [gone, _T - timedelta(days=5), None]
    assert stops[0].departed is None   # input untouched


def test_summary_buckets_by_time_since_last_seen():
    now = _T
    c = [
        {"departed_at": now - timedelta(hours=2), "spatial_status": "denver_core"},
        {"departed_at": now - timedelta(hours=30), "spatial_status": "denver_core"},
        {"departed_at": now - timedelta(days=40), "spatial_status": "other_outlier"},
    ]
    out = ghost_stops._summarise(c, now)
    assert out["stops"] == 3 and out["stops_denver_core"] == 2
    assert out["by_time_since_last_seen"] == {
        "<6h": 1, "6-24h": 0, "1-7d": 1, "7-30d": 0, ">30d": 1}


def test_default_preview_is_the_five_days_before_today():
    assert ghost_stops.default_preview_days(date(2026, 9, 29)) == [
        date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 26),
        date(2026, 9, 27), date(2026, 9, 28)]


def test_cli_refuses_what_it_does_not_understand(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(ghost_stops, "run", lambda **k: called.append("run"))
    monkeypatch.setattr(ghost_stops, "dry_run", lambda *a, **k: called.append("dry"))
    assert cli.close_ghost_stops_cli(["--force"]) == 2
    # A date only means something to the preview. Without --dry-run it must
    # not silently fall through to the WRITING path.
    assert cli.close_ghost_stops_cli(["2026-09-25"]) == 2
    assert cli.close_ghost_stops_cli(["--dry-run", "2026-13-01"]) == 2
    assert called == []


def test_cli_dry_run_passes_the_days_and_prints_json(monkeypatch, capsys):
    seen = {}

    def fake_dry_run(days):
        seen["days"] = days
        return {"dry_run": True, "as_of": _T}

    monkeypatch.setattr(ghost_stops, "dry_run", fake_dry_run)
    monkeypatch.setattr(ghost_stops, "run", lambda **k: (_ for _ in ()).throw(AssertionError))
    assert cli.close_ghost_stops_cli(["--dry-run", "2026-09-24", "2026-09-25"]) == 0
    assert seen["days"] == [date(2026, 9, 24), date(2026, 9, 25)]
    assert json.loads(capsys.readouterr().out)["dry_run"] is True

    assert cli.close_ghost_stops_cli(["--dry-run"]) == 0
    assert seen["days"] is None    # ghost_stops picks the default five


def test_cli_without_dry_run_writes(monkeypatch, capsys):
    monkeypatch.setattr(ghost_stops, "run", lambda: {"dry_run": False, "closed": 3})
    assert cli.close_ghost_stops_cli([]) == 0
    assert json.loads(capsys.readouterr().out)["closed"] == 3
