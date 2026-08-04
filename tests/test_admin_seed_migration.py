"""sql/057 — the owner's address is seeded into admin_allowlist.

Two layers, because the failure this guards against is silent. An empty
allowlist doesn't error: /private/* just 403s, Administrator Mode
disappears, and the proximity bypass stops applying. Nothing announces it.

  * The static tests below run everywhere and pin the properties the seed
    has to have to work at all — normalized, idempotent, additive.
  * The Postgres test runs the real migration set and asserts the row is
    actually there afterwards, and that applying it twice is a no-op.
    Skipped without VEO_TEST_PG_DSN, like every other *_pg test here.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path

import pytest

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
SEED = SQL_DIR / "057_seed_admin_zneill.sql"
OWNER = "zneill@gmail.com"


# --- static: properties the seed must have ----------------------------------

def test_the_seed_file_exists_and_names_the_owner():
    assert SEED.exists(), "sql/057 is the seed; renaming it needs this test updated"
    assert OWNER in SEED.read_text()


def test_the_seeded_address_is_already_normalized():
    """accounts.normalize_email is strip().lower(), and admin_emails() matches
    on that. A seed row with different case would sit in the table looking
    correct while never matching a session — the worst shape of this bug."""
    from src.accounts import normalize_email

    body = SEED.read_text()
    literal = re.search(r"VALUES\s*\(\s*'([^']+)'", body)
    assert literal, "expected a quoted email literal in the INSERT"
    assert literal.group(1) == normalize_email(literal.group(1))


def _statements() -> str:
    """The file's executable SQL, with `--` comment lines stripped.

    The prose in this migration discusses DELETE and the empty-table guard,
    so a scan of the raw text would flag its own explanation. What these
    tests are about is what the file DOES.
    """
    lines = [
        line for line in SEED.read_text().splitlines()
        if not line.lstrip().startswith("--")
    ]
    return " ".join(" ".join(lines).split()).upper()


def test_the_seed_is_idempotent():
    """Migrations are recorded in schema_migrations and run once, but this
    file must also be safe if it is ever replayed by hand — and re-adding an
    address must not rewrite the added_by/added_at of a row somebody already
    created through the portal or CLI."""
    body = _statements()
    assert "ON CONFLICT (EMAIL) DO NOTHING" in body
    assert "DO UPDATE" not in body


def test_the_seed_only_adds():
    """It seeds a starting point, it does not enforce a floor: the address
    stays removable through the portal, the CLI, and the API."""
    body = _statements()
    assert body.startswith("INSERT INTO ADMIN_ALLOWLIST")
    for forbidden in ("DELETE", "TRUNCATE", "DROP", "UPDATE ", "ALTER"):
        assert forbidden not in body, forbidden


# --- Postgres: the row is really there after migrating ----------------------

psycopg = pytest.importorskip("psycopg")


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


@contextmanager
def _migrated():
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — admin seed Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")
    conn = psycopg.connect(dsn)
    try:
        with conn.cursor() as cur:
            for path in sorted(SQL_DIR.glob("*.sql")):
                cur.execute(path.read_text())
        conn.commit()
        yield conn
    finally:
        conn.rollback()
        conn.close()


def test_a_freshly_migrated_database_has_an_admin():
    """The property that matters: no environment that runs this code comes up
    with an empty allowlist."""
    with _migrated() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM admin_allowlist")
            assert cur.fetchone()[0] >= 1
            cur.execute(
                "SELECT added_by FROM admin_allowlist WHERE email = %s", (OWNER,)
            )
            row = cur.fetchone()
            assert row is not None, f"{OWNER} missing from admin_allowlist"


def test_replaying_the_seed_preserves_an_existing_rows_provenance():
    """Somebody added through the portal keeps their added_by; the seed does
    not overwrite history it did not create."""
    with _migrated() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_allowlist SET added_by = 'portal:someone' "
                "WHERE email = %s",
                (OWNER,),
            )
            cur.execute(SEED.read_text())
            cur.execute(
                "SELECT added_by FROM admin_allowlist WHERE email = %s", (OWNER,)
            )
            assert cur.fetchone()[0] == "portal:someone"
            cur.execute(
                "SELECT COUNT(*) FROM admin_allowlist WHERE email = %s", (OWNER,)
            )
            assert cur.fetchone()[0] == 1
        conn.rollback()
