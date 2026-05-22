import os
import sqlite3
from typing import Optional

from .config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc                      TEXT    NOT NULL,

    -- (1-3) fleet totals
    fleet_total                 INTEGER NOT NULL,
    fleet_in_denver             INTEGER NOT NULL,
    fleet_outside_denver        INTEGER NOT NULL,

    -- (2) all devices in v1/v2
    all_in_v1                   INTEGER NOT NULL,
    all_in_v2                   INTEGER NOT NULL,

    -- (4-6) scooters (type 1)
    scooters_in_denver          INTEGER NOT NULL,
    scooters_in_v1              INTEGER NOT NULL,
    scooters_in_v2              INTEGER NOT NULL,

    -- (7-9) eBikes (type 3)
    ebikes_in_denver            INTEGER NOT NULL,
    ebikes_in_v1                INTEGER NOT NULL,
    ebikes_in_v2                INTEGER NOT NULL,

    -- (15-17) % of each type that is in v1
    all_pct_in_v1               REAL    NOT NULL,
    scooters_pct_in_v1          REAL    NOT NULL,
    ebikes_pct_in_v1            REAL    NOT NULL,

    -- (18-20) % of each type that is in v2
    all_pct_in_v2               REAL    NOT NULL,
    scooters_pct_in_v2          REAL    NOT NULL,
    ebikes_pct_in_v2            REAL    NOT NULL,

    -- (21-22) type3 share of motorized devices within each area
    type3_pct_of_motorized_v1   REAL    NOT NULL,
    type3_pct_of_motorized_v2   REAL    NOT NULL,

    -- (23-24) compliance booleans
    v1_over_30pct               INTEGER NOT NULL,
    v2_over_30pct               INTEGER NOT NULL,

    -- extras
    reserved_count              INTEGER NOT NULL,
    disabled_count              INTEGER NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def insert_snapshot(row: dict) -> None:
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" * len(row))
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO snapshots ({cols}) VALUES ({placeholders})",
            list(row.values()),
        )


def get_latest() -> Optional[dict]:
    with _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM snapshots ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        return dict(row) if row else None
