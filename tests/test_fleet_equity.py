"""The equity cut: rentals inside vs outside the official Equity Areas, from
rental_outcomes_hourly (sql/090), attributed to the unlock-point r9 cell.

Cell classification is tested against the REAL boundary file
(data/equity.geojson, 30 polygons) with the codebase's own point-in-polygon
test, not a toy polygon: the whole point is that a cell is only "inside" when
its whole hexagon is.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import h3
import pytest

from src import fleet_equity
from src.fleet_equity import BOUNDARY, INSIDE, OUTSIDE, classify_cell, summarize_cells
from src.fleet_outcomes import MIN_RENTALS_FOR_RATE as FLOOR
from src.geo import geometry_contains

EQUITY = json.loads((Path(__file__).resolve().parents[1] / "data" / "equity.geojson").read_text())


def contains(lon: float, lat: float) -> bool:
    return any(geometry_contains(f["geometry"], lon, lat) for f in EQUITY["features"])


def cell_at(lat: float, lon: float) -> int:
    return int(h3.latlng_to_cell(lat, lon, 9), 16)


def _find(kind: str) -> int:
    """A real r9 cell of the given kind near downtown Denver."""
    for i in range(-60, 61, 3):
        for j in range(-60, 61, 3):
            c = cell_at(39.74 + i * 0.002, -104.99 + j * 0.002)
            if classify_cell(c, contains) == kind:
                return c
    raise AssertionError(f"no {kind} cell found")


def test_a_whole_hexagon_inside_an_equity_area_is_inside():
    c = _find(INSIDE)
    hexid = h3.int_to_str(c)
    pts = [h3.cell_to_latlng(hexid), *h3.cell_to_boundary(hexid)]
    assert all(contains(lon, lat) for lat, lon in pts)


def test_a_hexagon_on_a_boundary_is_neither_side():
    c = _find(BOUNDARY)
    hexid = h3.int_to_str(c)
    pts = [h3.cell_to_latlng(hexid), *h3.cell_to_boundary(hexid)]
    hits = [contains(lon, lat) for lat, lon in pts]
    assert any(hits) and not all(hits)


def test_a_hexagon_clear_of_every_area_is_outside():
    c = _find(OUTSIDE)
    hexid = h3.int_to_str(c)
    pts = [h3.cell_to_latlng(hexid), *h3.cell_to_boundary(hexid)]
    assert not any(contains(lon, lat) for lat, lon in pts)


def _classifier(mapping):
    return lambda cell: mapping[cell]


def test_the_comparison_is_computed_only_when_both_sides_clear_the_floor():
    rows = [(1, 1000, 120, 0), (2, 1000, 60, 0)]
    out = summarize_cells(rows, classify=_classifier({1: INSIDE, 2: OUTSIDE}))
    assert out["inside"]["no_go_rate"] == 0.12
    assert out["outside"]["no_go_rate"] == 0.06
    assert out["difference_points"] == 6.0
    thin = summarize_cells([(1, FLOOR - 1, 9, 0), (2, 1000, 60, 0)],
                           classify=_classifier({1: INSIDE, 2: OUTSIDE}))
    assert thin["inside"]["no_go_rate"] is None
    assert thin["inside"]["rentals"] == FLOOR - 1, "counts stay when the rate is withheld"
    assert thin["difference_points"] is None


def test_boundary_cells_and_unobserved_origins_are_excluded_and_reported():
    rows = [(1, 500, 50, 0), (3, 300, 30, 0), (2, 400, 20, 0), (1, 40, 4, 5)]
    out = summarize_cells(rows, classify=_classifier({1: INSIDE, 2: OUTSIDE, 3: BOUNDARY}))
    assert out["inside"]["rentals"] == 500
    assert out["boundary_excluded"] == {"rentals": 300, "no_gos": 30, "cells": 1}
    assert out["origin_unknown_excluded"] == 40


def test_nothing_counted_is_nothing_counted():
    out = summarize_cells([], classify=_classifier({}))
    assert out["inside"]["rentals"] == 0 and out["inside"]["no_go_rate"] is None
    assert out["difference_points"] is None


def test_the_payload_states_window_sample_radius_and_method(monkeypatch):
    from contextlib import contextmanager

    class _Cur:
        def __init__(self): self.n = 0
        def execute(self, sql, params=None):
            self.n += 1
            if self.n == 1:
                assert "rental_outcomes_hourly" in sql and "radius_m = %s" in sql
        def fetchall(self): return []
        def fetchone(self): return (datetime(2026, 10, 7, 4, tzinfo=timezone.utc),)
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    @contextmanager
    def _conn():
        yield _Conn()

    monkeypatch.setattr(fleet_equity, "connection", _conn)
    out = fleet_equity.summarize("28d", now=datetime(2026, 10, 10, 12, 30, tzinfo=timezone.utc))
    assert out["window"] == "28d"
    assert out["window_end"] == "2026-10-10T12:00:00+00:00"
    assert out["window_start"] == "2026-09-12T12:00:00+00:00"
    assert out["data_since"] == "2026-10-07T04:00:00+00:00"
    assert out["radius_meters"] == 25.0
    assert out["min_rentals_for_rate"] == FLOOR
    assert (out["definition"], out["attribution"]) == ("end_displacement", "unlock_point_r9_cell")


def test_the_route_rejects_an_unknown_window():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src import api_public
    app = FastAPI()
    app.include_router(api_public.router)
    assert TestClient(app).get("/api/v1/fleet/outcomes/equity?window=1y").status_code == 400


def test_a_boundary_layer_failure_is_an_empty_comparison_not_a_500(monkeypatch):
    from contextlib import contextmanager

    class _Cur:
        def execute(self, *a, **k): pass
        def fetchall(self): return [(cell_at(39.74, -104.99), 500, 50, 0)]
        def fetchone(self): return (None,)
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    @contextmanager
    def _conn():
        yield _Conn()

    def boom(cell):
        raise FileNotFoundError("/app/data/equity.geojson")

    monkeypatch.setattr(fleet_equity, "connection", _conn)
    monkeypatch.setattr(fleet_equity, "classify_cell", boom)
    out = fleet_equity.summarize("7d")
    assert out["inside"]["rentals"] == 0 and out["difference_points"] is None
