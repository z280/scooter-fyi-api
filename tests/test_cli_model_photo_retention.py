"""python -m src.cli cleanup_model_report_photos.

sql/038 added model_reports.photo_deleted_at and NOTHING ever set it:
cleanup_receipts scans discount_reports only, so every `model-reports/`
object was retained forever — in the same private bucket, under the same
published 18-month promise as receipts, and with none of the three places
that document retention saying so.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from src import cli


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows):
        self.cur = _FakeCursor(rows)
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True


def _run(monkeypatch, rows, deleter=None):
    conn = _FakeConn(rows)
    deleted: list[str] = []

    @contextmanager
    def _fake_connection():
        yield conn

    def _delete(key):
        deleted.append(key)
        if deleter is not None:
            deleter(key)

    monkeypatch.setattr(cli, "connection", _fake_connection)
    monkeypatch.setattr("src.receipts.delete_receipt", _delete)
    return cli.cleanup_model_report_photos(), conn, deleted


def test_deletes_the_object_and_stamps_the_row(monkeypatch):
    result, conn, deleted = _run(monkeypatch, [(7, "model-reports/1/a.jpg")])
    assert result == {"deleted": 1, "failed": 0}
    assert deleted == ["model-reports/1/a.jpg"]
    assert conn.committed
    update = next(sql for sql, _ in conn.cur.executed if sql.startswith("UPDATE"))
    assert "model_reports SET photo_deleted_at = NOW()" in update


def test_keeps_the_report_row(monkeypatch):
    """Mirrors cleanup_receipts, not cleanup_ride_screenshots: a model
    report carries the correction itself (description, resolved model,
    queue state), which outlives the image. A screenshot row is nothing
    but its image, which is why that job deletes the row."""
    _, conn, _ = _run(monkeypatch, [(7, "model-reports/1/a.jpg")])
    assert not any(sql.startswith("DELETE") for sql, _ in conn.cur.executed)


def test_skips_rows_already_stamped_and_rows_with_no_photo(monkeypatch):
    """Idempotence lives in the WHERE clause — cron re-runs this daily."""
    _, conn, _ = _run(monkeypatch, [])
    select = conn.cur.executed[0][0]
    assert "photo_r2_key IS NOT NULL" in select
    assert "photo_deleted_at IS NULL" in select


def test_uses_the_same_18_month_calendar_window_as_receipts(monkeypatch):
    from datetime import datetime, timezone

    assert cli._MODEL_PHOTO_RETENTION_MONTHS == cli._RECEIPT_RETENTION_MONTHS == 18
    _, conn, _ = _run(monkeypatch, [])
    cutoff = conn.cur.executed[0][1][0]
    expected = cli._months_ago(datetime.now(timezone.utc), 18)
    assert abs((cutoff - expected).total_seconds()) < 60


def test_a_failed_delete_leaves_the_row_unstamped_for_the_next_run(monkeypatch):
    """The stamp is the tombstone. Stamping a row whose object survived
    would strand the object forever, which is the bug this job exists to
    fix — so the failure is counted and retried, never papered over."""
    def _boom(key):
        raise RuntimeError("R2 timeout")

    result, conn, _ = _run(monkeypatch, [(7, "model-reports/1/a.jpg")], deleter=_boom)
    assert result == {"deleted": 0, "failed": 1}
    assert not any(sql.startswith("UPDATE") for sql, _ in conn.cur.executed)


def test_unconfigured_r2_aborts_rather_than_silently_doing_nothing(monkeypatch):
    from src.receipts import ReceiptError

    def _boom(key):
        raise ReceiptError("receipt storage not configured")

    with pytest.raises(ReceiptError):
        _run(monkeypatch, [(7, "model-reports/1/a.jpg")], deleter=_boom)


def test_registered_in_cli_commands():
    assert "cleanup_model_report_photos" in cli.COMMANDS
