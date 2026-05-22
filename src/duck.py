"""Ephemeral DuckDB session factory with the spatial extension preloaded.

Every observation cycle opens a fresh ``:memory:`` connection, loads
boundaries, runs the spatial join, writes results to Postgres, and closes —
keeping steady-state RAM footprint near zero per spec §4.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import duckdb

log = logging.getLogger(__name__)


@contextmanager
def session() -> Iterator[duckdb.DuckDBPyConnection]:
    con = duckdb.connect(":memory:")
    try:
        # INSTALL is a no-op if already cached. LOAD makes ST_* available.
        # The spatial extension also brings ST_Read for direct GeoJSON ingestion.
        con.execute("INSTALL spatial;")
        con.execute("LOAD spatial;")
        # ST_Subdivide is intentionally NOT used: v1.json and v2.json are
        # now sets of small polygons after the reshape (see plan Step 4),
        # so the R-tree on raw geometries discriminates finely enough.
        yield con
    finally:
        con.close()
