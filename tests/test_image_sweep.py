"""sweep_orphan_images and the image clean-up on ride delete.

R2 is a fake in-memory bucket. The property that matters most is the first
test: an object some row references is never deleted, whatever its age,
prefix or bucket. tests/test_image_sweep_pg.py repeats the important parts
against real tables.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import cli, image_sweep, ride_screenshots

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)
YOUNG = NOW - timedelta(days=2)
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


class FakeS3:
    """Just enough of boto3's S3 client: paginated list, batch delete."""

    def __init__(self, objects: dict[str, tuple[int, datetime]] | None = None,
                 fail_keys: set[str] | None = None, page_size: int = 3):
        self.objects = dict(objects or {})
        self.fail_keys = fail_keys or set()
        self.page_size = page_size
        self.delete_calls: list[list[str]] = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        s3 = self

        class _P:
            def paginate(self, Bucket, Prefix):
                keys = sorted(k for k in s3.objects if k.startswith(Prefix))
                for i in range(0, max(len(keys), 1), s3.page_size):
                    chunk = keys[i:i + s3.page_size]
                    yield {"Contents": [{"Key": k, "Size": s3.objects[k][0],
                                         "LastModified": s3.objects[k][1]}
                                        for k in chunk]} if chunk else {}
        return _P()

    def delete_objects(self, Bucket, Delete):
        keys = [o["Key"] for o in Delete["Objects"]]
        self.delete_calls.append(keys)
        errors = []
        for k in keys:
            if k in self.fail_keys:
                errors.append({"Key": k, "Code": "InternalError"})
            else:
                self.objects.pop(k, None)
        return {"Errors": errors} if errors else {}


def _buckets(private: FakeS3, archive: FakeS3):
    return {image_sweep.PRIVATE: image_sweep.Bucket("receipts-bkt", private),
            image_sweep.ARCHIVE: image_sweep.Bucket("archive-bkt", archive)}


def _run(private, archive, refs, **kw):
    return image_sweep.sweep(buckets=_buckets(private, archive),
                             referenced=lambda: set(refs), now=NOW, **kw)


# --- the safety property -------------------------------------------------------

def test_referenced_objects_are_never_deleted():
    private = FakeS3({
        "receipts/7/a.jpg": (100, OLD),            # referenced
        "receipts/7/orphan.jpg": (200, OLD),       # orphan
        "model-reports/7/m.jpg": (300, OLD),       # referenced
        "ride-screenshots/7/s.jpg": (400, OLD),    # referenced
        "ride-screenshots/8/orphan.jpg": (500, OLD),
    })
    archive = FakeS3({
        "device-photos/7/d.jpg": (600, OLD),       # referenced
        "device-photos/9/orphan.jpg": (700, OLD),
        "raw/2026/10/01/x.parquet": (999, OLD),    # not a swept prefix at all
    })
    refs = {"receipts/7/a.jpg", "model-reports/7/m.jpg",
            "ride-screenshots/7/s.jpg", "device-photos/7/d.jpg"}
    out = _run(private, archive, refs, apply=True)

    for key in refs:
        assert key in private.objects or key in archive.objects
    assert "raw/2026/10/01/x.parquet" in archive.objects
    assert set(private.objects) == {"receipts/7/a.jpg", "model-reports/7/m.jpg",
                                    "ride-screenshots/7/s.jpg"}
    assert set(archive.objects) == {"device-photos/7/d.jpg", "raw/2026/10/01/x.parquet"}
    deleted = [k for call in private.delete_calls + archive.delete_calls for k in call]
    assert not set(deleted) & refs
    assert out["totals"]["deleted"] == 3
    assert out["totals"]["orphaned_bytes"] == 200 + 500 + 700
    assert out["prefixes"]["receipts/"]["referenced"] == 1


def test_references_are_bucket_blind():
    """A key referenced by any column protects the object in either bucket."""
    archive = FakeS3({"device-photos/1/x.jpg": (1, OLD)})
    _run(FakeS3(), archive, {"device-photos/1/x.jpg"}, apply=True)
    assert "device-photos/1/x.jpg" in archive.objects


def test_dry_run_is_the_default_and_deletes_nothing():
    private = FakeS3({"receipts/1/o.jpg": (10, OLD)})
    out = image_sweep.sweep(buckets=_buckets(private, FakeS3()),
                            referenced=lambda: set(), now=NOW)
    assert out["dry_run"] is True
    assert out["prefixes"]["receipts/"]["orphaned"] == 1
    assert out["totals"]["deleted"] == 0
    assert private.delete_calls == [] and "receipts/1/o.jpg" in private.objects


def test_grace_period_protects_young_objects():
    private = FakeS3({"receipts/1/young.jpg": (10, YOUNG),
                      "receipts/1/edge.jpg": (10, NOW - timedelta(days=7) + timedelta(minutes=1)),
                      "receipts/1/old.jpg": (10, NOW - timedelta(days=7, minutes=1))})
    out = _run(private, FakeS3(), set(), apply=True)
    assert set(private.objects) == {"receipts/1/young.jpg", "receipts/1/edge.jpg"}
    assert out["prefixes"]["receipts/"]["in_grace"] == 2


def test_unrecognised_key_shapes_are_left_alone():
    private = FakeS3({"receipts/readme.txt": (1, OLD),
                      "receipts/abc/x.jpg": (1, OLD),
                      "receipts/1/nested/x.jpg": (1, OLD),
                      "receipts/1/ok.jpg": (1, OLD)})
    out = _run(private, FakeS3(), set(), apply=True)
    assert set(private.objects) == {"receipts/readme.txt", "receipts/abc/x.jpg",
                                    "receipts/1/nested/x.jpg"}
    assert out["prefixes"]["receipts/"]["unrecognised"] == 3


def test_circuit_breaker_skips_a_prefix_that_looks_wholesale_orphaned():
    objs = {f"device-photos/1/{i}.jpg": (1, OLD) for i in range(30)}
    archive = FakeS3(objs)
    out = _run(FakeS3(), archive, set(), apply=True)
    assert out["prefixes"]["device-photos/"]["skipped_by_breaker"] is True
    assert len(archive.objects) == 30 and archive.delete_calls == []

    out = _run(FakeS3(), archive, set(), apply=True, force=True)
    assert archive.objects == {} and out["totals"]["deleted"] == 30


def test_breaker_does_not_trip_on_a_small_or_minority_orphan_set():
    objs = {f"receipts/1/{i}.jpg": (1, OLD) for i in range(25)}
    refs = {f"receipts/1/{i}.jpg" for i in range(13)}   # 12 of 25 orphaned
    private = FakeS3(objs)
    out = _run(private, FakeS3(), refs, apply=True)
    assert out["prefixes"]["receipts/"]["deleted"] == 12
    assert out["prefixes"]["receipts/"]["skipped_by_breaker"] is False


def test_delete_failures_are_counted_not_raised():
    private = FakeS3({"receipts/1/a.jpg": (1, OLD), "receipts/1/b.jpg": (1, OLD)},
                     fail_keys={"receipts/1/b.jpg"})
    out = _run(private, FakeS3(), set(), apply=True)
    assert out["prefixes"]["receipts/"]["deleted"] == 1
    assert out["prefixes"]["receipts/"]["failed"] == 1


def test_batches_of_at_most_1000():
    objs = {f"receipts/1/{i:05d}.jpg": (1, OLD) for i in range(2300)}
    private = FakeS3(objs, page_size=1000)
    _run(private, FakeS3(), set(), apply=True, force=True)
    assert [len(c) for c in private.delete_calls] == [1000, 1000, 300]


def test_logs_and_summary_never_carry_keys_or_account_ids(caplog):
    caplog.set_level(logging.DEBUG)
    private = FakeS3({"receipts/48213/0b5c.jpg": (10, OLD)},
                     fail_keys={"receipts/48213/0b5c.jpg"})
    out = _run(private, FakeS3({"device-photos/48213/x.jpg": (1, OLD)}), set(), apply=True)
    text = "\n".join(r.getMessage() for r in caplog.records) + repr(out)
    assert "48213" not in text and "0b5c" not in text


def test_reference_query_failure_aborts_before_any_delete():
    private = FakeS3({"receipts/1/a.jpg": (1, OLD)})

    def boom():
        raise RuntimeError("db down")
    with pytest.raises(RuntimeError):
        image_sweep.sweep(buckets=_buckets(private, FakeS3()), referenced=boom,
                          now=NOW, apply=True)
    assert private.delete_calls == []


# --- the reference set is complete -------------------------------------------

def test_every_r2_key_column_in_the_schema_is_a_reference_column():
    """A migration that adds a key column without listing it here would let
    the sweep delete the images it points at."""
    declared = set()
    for path in SQL_DIR.glob("*.sql"):
        text = re.sub(r"--[^\n]*", "", path.read_text())
        declared.update(re.findall(r"\b(\w*r2_key)\s+TEXT\b", text, flags=re.I))
    listed = {col for _, col in image_sweep.REFERENCE_COLUMNS}
    assert declared and declared <= listed, declared - listed


def test_every_writer_prefix_is_swept():
    src = Path(__file__).resolve().parents[1] / "src"
    written = set()
    for name in ("receipts.py", "ride_screenshots.py", "device_photos.py"):
        written.update(re.findall(r'key = f"([a-z-]+/)\{account_id\}/', (src / name).read_text()))
    assert written == set(image_sweep.PREFIXES)


def test_referenced_keys_reads_every_column():
    seen = []

    class Cur:
        def execute(self, sql, params=None):
            seen.append(sql)
            self._col = sql.split()[1]

        def fetchall(self):
            return [(f"k-{self._col}",), (None,)]

    keys = image_sweep.referenced_keys(Cur())
    assert len(seen) == len(image_sweep.REFERENCE_COLUMNS)
    for table, col in image_sweep.REFERENCE_COLUMNS:
        assert any(f"FROM {table}" in s and col in s for s in seen)
    assert None not in keys


# --- CLI -----------------------------------------------------------------------

def test_cli_sweep_records_the_run_and_defaults_to_dry_run(monkeypatch, capsys):
    calls = {}
    def fake_sweep(**kw):
        calls["kw"] = kw
        return {"dry_run": not kw["apply"]}
    monkeypatch.setattr(image_sweep, "sweep", fake_sweep)
    ledger = []
    monkeypatch.setattr(cli.job_runs, "start", lambda cmd: ledger.append(("start", cmd)) or 42)
    monkeypatch.setattr(cli.job_runs, "finish",
                        lambda run_id, **kw: ledger.append(("finish", run_id, kw["status"])))
    assert cli.sweep_orphan_images_cli([]) == 0
    assert calls["kw"] == {"apply": False, "force": False}
    assert ledger == [("start", "sweep_orphan_images"), ("finish", 42, "ok")]

    calls.clear()
    assert cli.sweep_orphan_images_cli(["--apply"]) == 0
    assert calls["kw"] == {"apply": True, "force": False}
    assert cli.sweep_orphan_images_cli(["--nope"]) == 2


def test_cli_sweep_failure_is_recorded(monkeypatch):
    def boom(**kw):
        raise RuntimeError("R2 not configured")
    monkeypatch.setattr(image_sweep, "sweep", boom)
    ledger = []
    monkeypatch.setattr(cli.job_runs, "start", lambda cmd: 7)
    monkeypatch.setattr(cli.job_runs, "finish",
                        lambda run_id, **kw: ledger.append(kw["status"]))
    assert cli.main(["sweep_orphan_images", "--apply"]) == 1
    assert ledger == ["error"]


def test_crontab_runs_the_sweep_weekly_with_apply():
    crontab = (Path(__file__).resolve().parents[1] / "crontab").read_text()
    lines = [ln for ln in crontab.splitlines()
             if "sweep_orphan_images" in ln and not ln.lstrip().startswith("#")]
    assert len(lines) == 1
    fields = lines[0].split()
    assert fields[2:5] == ["*", "*", "0"]          # once a week (Sunday)
    assert lines[0].endswith("python -m src.cli sweep_orphan_images --apply")


@pytest.mark.parametrize("args", [[], ["--account-id"], ["--account-id", "x"],
                                  ["--account-id", "0"], ["--account-id", "-3"],
                                  ["5"]])
def test_cli_delete_account_usage(args):
    assert cli.delete_account_cli(args) == 2


def test_cli_delete_account_is_dry_run_by_default(monkeypatch):
    seen = {}

    def fake(account_id, apply):
        seen.update(account_id=account_id, apply=apply)
        return {"images_failed": 0}
    monkeypatch.setattr(image_sweep, "delete_account", fake)
    assert cli.delete_account_cli(["--account-id", "12"]) == 0
    assert seen == {"account_id": 12, "apply": False}
    assert cli.delete_account_cli(["--account-id", "12", "--apply"]) == 0
    assert seen == {"account_id": 12, "apply": True}


# --- tracked-ride delete removes the screenshot objects -----------------------

class _RideCur:
    def __init__(self, keys, rowcount):
        self.keys, self._rowcount = keys, rowcount
        self.sql: list[str] = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        self.rowcount = self._rowcount if sql.startswith("DELETE") else 0

    def fetchall(self):
        return [(k,) for k in self.keys]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RideConn:
    def __init__(self, cur, events):
        self.cur, self.events = cur, events

    def cursor(self):
        return self.cur

    def commit(self):
        self.events.append("commit")


def _ride_client(monkeypatch, keys, rowcount, fail=False):
    from src import api_tracked_rides
    from src.accounts import SessionUser, require_session

    events: list[str] = []
    cur = _RideCur(keys, rowcount)

    @contextmanager
    def fake_connection():
        yield _RideConn(cur, events)

    def fake_delete(key):
        events.append(f"r2:{key}")
        if fail:
            raise RuntimeError("R2 500")
    monkeypatch.setattr(api_tracked_rides, "connection", fake_connection)
    monkeypatch.setattr(ride_screenshots, "delete_screenshot", fake_delete)
    user = SessionUser(account_id=5, email="r@example.com", scopes=("rider",),
                       expires_at=NOW, sliding=True, method="google", token_sha256="x")
    app = FastAPI()
    app.include_router(api_tracked_rides.router)
    app.dependency_overrides[require_session] = lambda: user
    return TestClient(app), cur, events


RIDE = "11111111-2222-3333-4444-555555555555"


def test_single_ride_delete_removes_its_screenshots_after_commit(monkeypatch):
    client, cur, events = _ride_client(monkeypatch, ["ride-screenshots/5/a.jpg",
                                                     "ride-screenshots/5/b.jpg"], 1)
    r = client.delete(f"/api/v1/tracked-rides/{RIDE}")
    assert r.status_code == 200 and r.json() == {"deleted": True}
    assert cur.sql[0].startswith("SELECT r2_key FROM ride_transaction_screenshots")
    assert cur.sql[1].startswith("DELETE FROM tracked_rides")
    assert events == ["commit", "r2:ride-screenshots/5/a.jpg", "r2:ride-screenshots/5/b.jpg"]


def test_not_found_deletes_no_objects(monkeypatch):
    client, _, events = _ride_client(monkeypatch, ["ride-screenshots/5/a.jpg"], 0)
    assert client.delete(f"/api/v1/tracked-rides/{RIDE}").status_code == 404
    assert not [e for e in events if e.startswith("r2:")]


def test_storage_failure_does_not_fail_the_delete(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    client, _, events = _ride_client(monkeypatch, ["ride-screenshots/5/a.jpg"], 1, fail=True)
    r = client.delete(f"/api/v1/tracked-rides/{RIDE}")
    assert r.status_code == 200
    assert "commit" in events
    assert "ride-screenshots/5" not in "\n".join(m.getMessage() for m in caplog.records)


def test_delete_all_rides_removes_every_screenshot(monkeypatch):
    client, cur, events = _ride_client(monkeypatch, ["ride-screenshots/5/a.jpg",
                                                     "ride-screenshots/5/c.jpg"], 3)
    r = client.delete("/api/v1/tracked-rides")
    assert r.json() == {"deleted_count": 3}
    assert "JOIN tracked_rides" in cur.sql[0]
    assert events == ["commit", "r2:ride-screenshots/5/a.jpg", "r2:ride-screenshots/5/c.jpg"]
