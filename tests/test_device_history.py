"""Fleet history: compute.fleet_status_counts (sql/069's writer input) and
GET /api/v1/devices/history/hourly (src/api_device_history.py).

Defended:
  * disabled wins over reserved; absent booleans read as available (the
    live map's own reading); non-denver_core devices are out of scope,
    and the polygon-CORRECTED status decides, not the ingest-time tag;
  * `models` keys are the feed's display names, each carrying the same
    three status counts so every metric breaks down by model;
  * the endpoint samples the LAST cycle per hour, backfills totals-only
    hours from snapshot_metadata_core with null breakdowns (never zeros —
    a zero would chart a fleet collapse that never happened), and returns
    hours sorted ascending.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_device_history
from src.compute import fleet_status_counts


def device(
    device_id: str,
    *,
    disabled=None,
    reserved=None,
    model="Astro",
    spatial="denver_core",
):
    return SimpleNamespace(
        device_id=device_id,
        spatial_status=spatial,
        is_disabled=disabled,
        is_reserved=reserved,
        vehicle_model_name=model,
    )


def test_fleet_status_counts_semantics():
    devices = [
        device("a"),                                   # available (both None)
        device("b", disabled=False, reserved=False),   # available
        device("c", reserved=True),                    # reserved
        device("d", disabled=True, reserved=True),     # disabled wins
        device("e", model="Rover"),                    # available
        device("f", spatial="china_glitch"),           # out of scope
    ]
    counts = fleet_status_counts(devices, {})
    assert counts == {
        "total": 5,
        "available": 3,
        "reserved": 1,
        "out_of_service": 1,
        "models": {
            "Astro": {"available": 2, "reserved": 1, "out_of_service": 1},
            "Rover": {"available": 1, "reserved": 0, "out_of_service": 0},
        },
    }


def test_corrected_status_outranks_the_ingest_tag():
    devices = [device("promoted", spatial="other_outlier"), device("demoted")]
    counts = fleet_status_counts(
        devices,
        {"promoted": "denver_core", "demoted": "other_outlier"},
    )
    assert counts["total"] == 1
    assert counts["models"] == {
        "Astro": {"available": 1, "reserved": 0, "out_of_service": 0},
    }


def test_blank_model_counts_as_unknown():
    counts = fleet_status_counts([device("a", model="  ")], {})
    assert counts["models"] == {
        "Unknown": {"available": 1, "reserved": 0, "out_of_service": 0},
    }


# --- the endpoint -----------------------------------------------------------


class _FakeCursor:
    def __init__(self, fetches):
        self._fetches = list(fetches)
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self.statements.append(" ".join(str(sql).split()))

    def fetchall(self):
        return self._fetches.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, fetches):
        self.cur = _FakeCursor(fetches)

    def cursor(self):
        return self.cur

    def commit(self):
        pass


def _client(monkeypatch, snapshot_rows, core_rows):
    conn = _FakeConn([snapshot_rows, core_rows])

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_device_history, "connection", _fake_connection)
    app = FastAPI()
    app.include_router(api_device_history.router)
    return TestClient(app)


def _hour(h: int) -> datetime:
    return datetime(2026, 8, 10, h, tzinfo=timezone.utc)


def test_hourly_merges_snapshots_with_core_total_backfill(monkeypatch):
    client = _client(
        monkeypatch,
        # Snapshot rows cover 14:00 only.
        [(
            _hour(14), 500, 420, 30, 50,
            {"Astro": {"available": 200, "reserved": 20, "out_of_service": 30},
             "Rover": {"available": 220, "reserved": 10, "out_of_service": 20}},
        )],
        # Core totals cover 13:00 and 14:00 — 14:00 must NOT override the
        # richer snapshot row; 13:00 backfills with null breakdowns.
        [(_hour(13), 480), (_hour(14), 501)],
    )
    r = client.get("/api/v1/devices/history/hourly?days=2")
    assert r.status_code == 200
    hours = r.json()["hours"]
    assert [h["hour"] for h in hours] == [
        _hour(13).isoformat(),
        _hour(14).isoformat(),
    ]
    backfilled, rich = hours
    assert backfilled["total"] == 480
    assert backfilled["available"] is None
    assert backfilled["models"] is None
    assert rich == {
        "hour": _hour(14).isoformat(),
        "total": 500,
        "available": 420,
        "reserved": 30,
        "out_of_service": 50,
        "models": {
            "Astro": {"available": 200, "reserved": 20, "out_of_service": 30},
            "Rover": {"available": 220, "reserved": 10, "out_of_service": 20},
        },
    }


def test_days_is_clamped_to_the_two_week_window(monkeypatch):
    client = _client(monkeypatch, [], [])
    assert client.get("/api/v1/devices/history/hourly?days=15").status_code == 422
    assert client.get("/api/v1/devices/history/hourly?days=0").status_code == 422
    r = client.get("/api/v1/devices/history/hourly")
    assert r.status_code == 200
    assert r.json() == {"days": 14, "hours": []}
