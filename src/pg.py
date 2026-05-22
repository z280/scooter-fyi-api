"""Postgres connection pool + idempotent SQL migration runner."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool

from .config import pg_dsn

log = logging.getLogger(__name__)

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=pg_dsn(),
            min_size=1,
            max_size=int(os.environ.get("PG_POOL_MAX", "8")),
            kwargs={"autocommit": False},
        )
    return _pool


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:
        yield conn


def run_migrations() -> list[str]:
    """Apply any *.sql files in sql/ that haven't been recorded yet.

    Tracked by the schema_migrations table — created by the first file itself.
    Returns the list of newly applied filenames.
    """
    applied: list[str] = []
    files = sorted(SQL_DIR.glob("*.sql"))
    if not files:
        log.warning("No SQL files found in %s", SQL_DIR)
        return applied

    with connection() as conn:
        # Bootstrap the tracking table by executing the first migration up to its
        # CREATE TABLE schema_migrations statement. Simpler: just run it whole;
        # IF NOT EXISTS makes every file safe to re-run.
        with conn.cursor() as cur:
            # If schema_migrations doesn't exist yet, every file is "new". Once
            # it exists, check before applying each.
            for path in files:
                name = path.name
                already = False
                try:
                    cur.execute(
                        "SELECT 1 FROM schema_migrations WHERE filename = %s",
                        (name,),
                    )
                    already = cur.fetchone() is not None
                except psycopg.errors.UndefinedTable:
                    conn.rollback()
                    already = False

                if already:
                    log.info("migration already applied: %s", name)
                    continue

                sql = path.read_text()
                log.info("applying migration: %s", name)
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s) ON CONFLICT DO NOTHING",
                    (name,),
                )
                applied.append(name)
        conn.commit()

    return applied
