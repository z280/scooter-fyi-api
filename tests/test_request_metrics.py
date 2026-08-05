"""Request-metrics middleware: route templates, UA buckets, fail-open.

The middleware's one hard rule is that it can never fail a request —
capture errors are swallowed. These tests drive it through a real
FastAPI app (TestClient) with `record` captured, plus table-driven
coverage of the UA classifier and a fake-connection test of
flush_pending. No Postgres needed.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import request_metrics


@pytest.mark.parametrize(
    ("ua", "expected"),
    [
        (None, ("other", "other")),
        ("", ("other", "other")),
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X)", ("mobile", "ios")),
        ("Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X)", ("tablet", "ios")),
        ("Mozilla/5.0 (Linux; Android 14; Pixel 8) Mobile Safari", ("mobile", "android")),
        ("Mozilla/5.0 (Linux; Android 14; SM-X910) Safari", ("tablet", "android")),
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64)", ("desktop", "windows")),
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", ("desktop", "mac")),
        ("Mozilla/5.0 (X11; Linux x86_64)", ("desktop", "linux")),
        ("SomethingWeird/1.0 Mobi", ("mobile", "other")),
        ("curl/8.5.0", ("other", "other")),
    ],
)
def test_classify_user_agent(ua, expected):
    assert request_metrics.classify_user_agent(ua) == expected


def _app_with_capture(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(
        request_metrics, "record", lambda **kw: captured.append(kw)
    )
    app = FastAPI()
    app.middleware("http")(request_metrics.middleware)

    @app.get("/items/{item_id}")
    def item(item_id: int):
        return {"id": item_id}

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/boom")
    def boom():
        raise RuntimeError("handler exploded")

    return TestClient(app, raise_server_exceptions=False), captured


def test_route_template_not_raw_path(monkeypatch):
    client, captured = _app_with_capture(monkeypatch)
    assert client.get("/items/12345").status_code == 200
    [row] = captured
    assert row["route"] == "/items/{item_id}"
    assert row["method"] == "GET"
    assert row["status"] == 200
    assert row["duration_ms"] >= 0


def test_unmatched_path_gets_single_bucket(monkeypatch):
    client, captured = _app_with_capture(monkeypatch)
    assert client.get("/no/such/path/9f8e7d").status_code == 404
    [row] = captured
    assert row["route"] == "__unmatched__"
    assert row["status"] == 404


def test_health_and_admin_are_skipped(monkeypatch):
    client, captured = _app_with_capture(monkeypatch)
    client.get("/health")
    client.get("/admin/anything")
    assert captured == []


def test_bearer_presence_marks_authenticated(monkeypatch):
    client, captured = _app_with_capture(monkeypatch)
    client.get("/items/1", headers={"Authorization": "Bearer abc"})
    client.get("/items/2")
    assert [r["is_authenticated"] for r in captured] == [True, False]


def test_capture_failure_does_not_fail_the_request(monkeypatch):
    client, captured = _app_with_capture(monkeypatch)
    monkeypatch.setattr(
        request_metrics,
        "classify_user_agent",
        lambda ua: (_ for _ in ()).throw(RuntimeError("parser broke")),
    )
    assert client.get("/items/1").status_code == 200
    assert captured == []


def test_handler_exception_still_propagates(monkeypatch):
    """The middleware swallows ITS OWN errors, not the app's."""
    client, _ = _app_with_capture(monkeypatch)
    assert client.get("/boom").status_code == 500


def test_flush_pending_writes_and_drains(monkeypatch):
    from contextlib import contextmanager

    written: list = []

    class _Cur:
        def executemany(self, sql, rows):
            written.extend(rows)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

    @contextmanager
    def fake_connection():
        yield _Conn()

    monkeypatch.setattr(request_metrics, "connection", fake_connection)
    request_metrics._buffer.clear()
    for i in range(3):
        request_metrics.record(
            route="/items/{item_id}",
            method="GET",
            status=200,
            duration_ms=i,
            device_class="mobile",
            os_family="ios",
            is_authenticated=False,
        )
    assert request_metrics.flush_pending() == 3
    assert len(written) == 3
    assert not request_metrics._buffer
    assert request_metrics.flush_pending() == 0
