"""Postgres-backed coverage for src/image_sweep.py: the reference set built
from REAL tables protects every stored key, the schema has no key column the
sweep does not know about, and delete_account removes the account, its
cascades and its images while leaving everyone else's alone.

R2 is the in-memory fake from tests/test_image_sweep.py.

SKIPS unless a reachable, migratable test database is provided via
VEO_TEST_PG_DSN (see tests/test_daily_trips_rollup_pg.py for the pattern).
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from src import image_sweep  # noqa: E402
from src.accounts import upsert_account  # noqa: E402
from tests.test_image_sweep import FakeS3, _buckets  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
OLD = datetime.now(timezone.utc) - timedelta(days=60)


@pytest.fixture()
def pg_conn(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — image sweep Postgres test skipped")
    try:
        conn = psycopg.connect(dsn, connect_timeout=3)
    except Exception:  # noqa: BLE001
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(image_sweep, "connection", _fake_connection)
    try:
        yield conn
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM accounts WHERE email LIKE 'pgtest-sweep-%%'")
            cur.execute("DELETE FROM model_reports WHERE device_id = 'pgtest-sweep'")
        conn.commit()
        conn.close()


def _seed_account(conn) -> tuple[int, dict[str, str]]:
    """One account with one image in every table that stores a key."""
    with conn.cursor() as cur:
        aid = upsert_account(cur, f"pgtest-sweep-{uuid.uuid4()}@example.com")
        keys = {
            "receipt": f"receipts/{aid}/{uuid.uuid4()}.jpg",
            "plan": f"receipts/{aid}/{uuid.uuid4()}.jpg",
            "model": f"model-reports/{aid}/{uuid.uuid4()}.jpg",
            "shot": f"ride-screenshots/{aid}/{uuid.uuid4()}.jpg",
            "device": f"device-photos/{aid}/{uuid.uuid4()}.jpg",
        }
        cur.execute(
            "INSERT INTO discount_reports (account_id, zone_version, ride_ended_at, "
            "receipt_r2_key, plan_evidence_r2_key) VALUES (%s, 'v1', NOW(), %s, %s)",
            (aid, keys["receipt"], keys["plan"]))
        cur.execute(
            "INSERT INTO model_reports (account_id, device_id, description, photo_r2_key) "
            "VALUES (%s, 'pgtest-sweep', 'a test model report', %s)", (aid, keys["model"]))
        cur.execute(
            "INSERT INTO tracked_rides (account_id, vehicle_identifier, start_lat, start_lon, "
            "watch_expires_at) VALUES (%s, 'aaaa000000000000', 39.74, -104.99, NOW()) "
            "RETURNING id", (aid,))
        (ride_id,) = cur.fetchone()
        cur.execute(
            "INSERT INTO ride_transaction_screenshots (ride_id, account_id, screenshot_type, "
            "r2_key) VALUES (%s, %s, 'overview', %s)", (ride_id, aid, keys["shot"]))
        cur.execute(
            "INSERT INTO device_photos (vehicle_identifier, account_id, r2_key) "
            "VALUES ('aaaa000000000000', %s, %s)", (aid, keys["device"]))
    conn.commit()
    return aid, keys


def _stores(*accounts: tuple[int, dict[str, str]]):
    private, archive = FakeS3(page_size=1000), FakeS3(page_size=1000)
    for aid, keys in accounts:
        for name, key in keys.items():
            (archive if name == "device" else private).objects[key] = (100, OLD)
        # An old orphan under each of this account's prefixes.
        for prefix, kind in image_sweep.PREFIXES.items():
            store = archive if kind == image_sweep.ARCHIVE else private
            store.objects[f"{prefix}{aid}/orphan-{uuid.uuid4()}.jpg"] = (50, OLD)
    return private, archive


def test_schema_key_columns_match_reference_columns(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name LIKE '%%r2_key'")
        found = set(cur.fetchall())
    assert found == set(image_sweep.REFERENCE_COLUMNS)


def test_sweep_against_real_tables_never_deletes_a_referenced_object(pg_conn):
    a = _seed_account(pg_conn)
    b = _seed_account(pg_conn)
    private, archive = _stores(a, b)
    referenced = set(a[1].values()) | set(b[1].values())

    out = image_sweep.sweep(apply=True, force=True, buckets=_buckets(private, archive))

    remaining = set(private.objects) | set(archive.objects)
    assert referenced <= remaining
    assert not any("/orphan-" in k for k in remaining)
    assert out["totals"]["deleted"] == 8          # 4 prefixes x 2 accounts


def test_delete_account_dry_run_changes_nothing(pg_conn):
    a = _seed_account(pg_conn)
    private, archive = _stores(a)
    before = (dict(private.objects), dict(archive.objects))
    out = image_sweep.delete_account(a[0], apply=False, buckets=_buckets(private, archive))
    assert out["dry_run"] and out["account_existed"]
    assert out["row_referenced_images"] == 5 and out["images_targeted"] == 9
    assert (private.objects, archive.objects) == before
    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM accounts WHERE id = %s", (a[0],))
        assert cur.fetchone() is not None


def test_delete_account_removes_the_account_and_only_its_images(pg_conn):
    a = _seed_account(pg_conn)
    b = _seed_account(pg_conn)
    private, archive = _stores(a, b)
    with pg_conn.cursor() as cur:
        email = f"pgtest-sweep-code-{uuid.uuid4()}@example.com"
        cur.execute("UPDATE accounts SET email = %s WHERE id = %s", (email, a[0]))
        cur.execute("INSERT INTO login_codes (email, code_hash, expires_at) "
                    "VALUES (%s, 'h', NOW() + INTERVAL '10 minutes')", (email.upper(),))
    pg_conn.commit()

    out = image_sweep.delete_account(a[0], apply=True, buckets=_buckets(private, archive))
    assert out["images_deleted"] == 9 and out["images_failed"] == 0
    assert out["model_report_photos_detached"] == 1
    assert out["login_codes_deleted"] == 1

    remaining = set(private.objects) | set(archive.objects)
    assert not any(f"/{a[0]}/" in k for k in remaining)
    assert set(b[1].values()) <= remaining          # the other account untouched

    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM accounts WHERE id = %s", (a[0],))
        assert cur.fetchone() is None
        for table in ("discount_reports", "tracked_rides",
                      "ride_transaction_screenshots", "device_photos"):
            cur.execute(f"SELECT count(*) FROM {table} WHERE account_id = %s", (a[0],))
            assert cur.fetchone()[0] == 0, table
        # The model report survives (ON DELETE SET NULL) without its photo.
        cur.execute("SELECT account_id, photo_r2_key, photo_deleted_at FROM model_reports "
                    "WHERE photo_r2_key = %s OR (device_id = 'pgtest-sweep' AND "
                    "account_id IS NULL)", (a[1]["model"],))
        rows = cur.fetchall()
        assert rows and all(r[0] is None and r[1] is None and r[2] is not None for r in rows)
        cur.execute("SELECT photo_r2_key FROM model_reports WHERE account_id = %s", (b[0],))
        assert cur.fetchone()[0] == b[1]["model"]


def test_delete_account_still_clears_prefixes_of_an_account_already_gone(pg_conn):
    a = _seed_account(pg_conn)
    private, archive = _stores(a)
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM accounts WHERE id = %s", (a[0],))   # by hand, as before
    pg_conn.commit()
    out = image_sweep.delete_account(a[0], apply=True, buckets=_buckets(private, archive))
    assert out["account_existed"] is False
    assert not private.objects and not archive.objects
    assert out["model_report_photos_detached"] == 1
