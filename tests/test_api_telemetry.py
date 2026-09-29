"""POST /api/v1/telemetry/events — drop-don't-reject validation.

The ingest endpoint's contract is that nothing a client sends can turn
into an error loop: unknown names, oversized bodies, and junk props are
dropped or truncated, and the response is 204 either way. These tests pin
that behavior plus the caps and the rate-limit wiring. enforce() and the
connection are recorded rather than driven, so no Postgres is needed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_telemetry


class _FakeCursor:
    def __init__(self, calls):
        self.calls = calls

    def execute(self, sql, params=None):
        self.calls.append(("execute", sql, params))

    def executemany(self, sql, rows):
        self.calls.append(("executemany", sql, rows))

    def fetchone(self):
        return ("test-salt",)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, calls):
        self.calls = calls

    def cursor(self):
        return _FakeCursor(self.calls)

    def commit(self):
        pass


def _install(monkeypatch):
    """Recording connection + enforce; returns (calls, enforce_calls)."""
    calls: list = []
    enforce_calls: list = []

    from contextlib import contextmanager

    @contextmanager
    def fake_connection():
        yield _FakeConn(calls)

    def fake_enforce(cur, **kwargs):
        enforce_calls.append(kwargs)

    monkeypatch.setattr(api_telemetry, "connection", fake_connection)
    monkeypatch.setattr(api_telemetry, "enforce", fake_enforce)
    return calls, enforce_calls


def _client():
    app = FastAPI()
    app.include_router(api_telemetry.router)
    return TestClient(app)


def _inserted_rows(calls):
    return [
        rows
        for kind, sql, rows in calls
        if kind == "executemany" and "telemetry_events" in sql
    ]


def _batch(events, page=None):
    return {
        "v": 1,
        "page": page
        or {"vp": "md", "dc": "mobile", "os": "ios", "ref": "google.com",
            "auth": True},
        "events": events,
    }


def _event(name, **props):
    return {
        "n": name,
        "t": int(datetime.now(timezone.utc).timestamp() * 1000),
        "sid": "sess-abc",
        "p": props,
    }


def test_valid_batch_inserts_and_rate_limits(monkeypatch):
    calls, enforce_calls = _install(monkeypatch)
    r = _client().post(
        "/api/v1/telemetry/events",
        json=_batch([_event("page_load"), _event("drawer_open", drawer="filters")]),
    )
    assert r.status_code == 204
    [rows] = _inserted_rows(calls)
    assert [row[1] for row in rows] == ["page_load", "drawer_open"]
    # context propagated onto every row
    assert all(row[4] == "mobile" and row[5] == "ios" for row in rows)
    assert all(row[8] is True for row in rows)
    [rl] = enforce_calls
    assert rl["bucket"] == "telemetry_ip"
    assert rl["limit"] == api_telemetry._RATE_LIMIT


def test_unknown_event_names_are_dropped_not_rejected(monkeypatch):
    calls, _ = _install(monkeypatch)
    r = _client().post(
        "/api/v1/telemetry/events",
        json=_batch([_event("page_load"), _event("totally_new_event")]),
    )
    assert r.status_code == 204
    [rows] = _inserted_rows(calls)
    assert [row[1] for row in rows] == ["page_load"]


def test_all_unknown_batch_touches_no_db(monkeypatch):
    calls, enforce_calls = _install(monkeypatch)
    r = _client().post(
        "/api/v1/telemetry/events", json=_batch([_event("nope")])
    )
    assert r.status_code == 204
    assert not calls and not enforce_calls


def test_batch_truncated_to_cap(monkeypatch):
    calls, _ = _install(monkeypatch)
    events = [_event("page_load") for _ in range(80)]
    assert _client().post(
        "/api/v1/telemetry/events", json=_batch(events)
    ).status_code == 204
    [rows] = _inserted_rows(calls)
    assert len(rows) == api_telemetry.MAX_BATCH_EVENTS


def test_oversized_body_dropped(monkeypatch):
    calls, _ = _install(monkeypatch)
    batch = _batch([_event("page_load", pad="x" * 40_000)])
    r = _client().post(
        "/api/v1/telemetry/events",
        content=json.dumps(batch),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 204
    assert not calls


def test_garbage_body_dropped(monkeypatch):
    calls, _ = _install(monkeypatch)
    r = _client().post(
        "/api/v1/telemetry/events",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 204
    assert not calls


def test_props_truncated_and_nonscalar_dropped(monkeypatch):
    calls, _ = _install(monkeypatch)
    props = {"long": "y" * 500, "nested": {"a": 1}, "ok": "fine", "n": 3}
    _client().post(
        "/api/v1/telemetry/events", json=_batch([_event("page_load", **props)])
    )
    [rows] = _inserted_rows(calls)
    stored = json.loads(rows[0][9])
    assert stored["ok"] == "fine"
    assert stored["n"] == 3
    assert len(stored["long"]) == api_telemetry.MAX_PROP_VALUE_CHARS
    assert "nested" not in stored


def test_prop_key_count_capped(monkeypatch):
    calls, _ = _install(monkeypatch)
    props = {f"k{i}": "v" for i in range(30)}
    _client().post(
        "/api/v1/telemetry/events", json=_batch([_event("page_load", **props)])
    )
    [rows] = _inserted_rows(calls)
    assert len(json.loads(rows[0][9])) == api_telemetry.MAX_PROP_KEYS


def test_implausible_timestamp_replaced_with_arrival_time(monkeypatch):
    calls, _ = _install(monkeypatch)
    stale = _event("page_load")
    stale["t"] = int(
        (datetime.now(timezone.utc) - timedelta(days=3)).timestamp() * 1000
    )
    _client().post("/api/v1/telemetry/events", json=_batch([stale]))
    [rows] = _inserted_rows(calls)
    received_at = rows[0][0]
    assert datetime.now(timezone.utc) - received_at < timedelta(minutes=1)


def test_unknown_context_vocab_falls_back_to_other(monkeypatch):
    calls, _ = _install(monkeypatch)
    page = {"vp": "gigantic", "dc": "smartfridge", "os": "beos",
            "ref": "evil.example/path?q=1", "auth": "yes-string"}
    _client().post(
        "/api/v1/telemetry/events", json=_batch([_event("page_load")], page=page)
    )
    [rows] = _inserted_rows(calls)
    row = rows[0]
    assert row[4] == "other"  # device_class
    assert row[5] == "other"  # os_family
    assert row[6] == "other"  # viewport
    assert row[7] == "evil.example"  # host only, path stripped
    assert row[8] is False  # auth must be literal True


def test_wrong_schema_version_dropped(monkeypatch):
    calls, _ = _install(monkeypatch)
    body = _batch([_event("page_load")])
    body["v"] = 2
    assert _client().post(
        "/api/v1/telemetry/events", json=body
    ).status_code == 204
    assert not calls


# Frontend events the allowlist used to drop on the floor (owner decision:
# collect all eight). `active_vehicle` is deliberately absent — the frontend
# is deleting it.
_NEWLY_COLLECTED = (
    "dibs", "home_bar", "arrival_panel", "about_founder_open",
    "device_gone", "dibs_alert", "recenter", "ride_mode_free",
)


def test_newly_collected_events_are_allowlisted():
    assert set(_NEWLY_COLLECTED) <= api_telemetry.ALLOWED_EVENTS
    assert "active_vehicle" not in api_telemetry.ALLOWED_EVENTS


def test_newly_collected_events_are_stored(monkeypatch):
    calls, _ = _install(monkeypatch)
    r = _client().post(
        "/api/v1/telemetry/events",
        json=_batch([_event(n, action="x") for n in _NEWLY_COLLECTED]),
    )
    assert r.status_code == 204
    [rows] = _inserted_rows(calls)
    assert [row[1] for row in rows] == list(_NEWLY_COLLECTED)


def test_drop_log_names_the_dropped_events(monkeypatch, caplog):
    _install(monkeypatch)
    caplog.set_level("INFO", logger=api_telemetry.__name__)
    _client().post(
        "/api/v1/telemetry/events",
        json=_batch([
            _event("page_load"),
            _event("active_vehicle"),
            _event("active_vehicle"),
            _event("brand_new"),
        ]),
    )
    [msg] = [r.getMessage() for r in caplog.records if "dropped" in r.getMessage()]
    assert "dropped 3 event(s)" in msg
    # deduplicated, in first-seen order
    assert msg.endswith(": active_vehicle, brand_new")


def test_drop_log_sanitizes_client_supplied_names(monkeypatch, caplog):
    _install(monkeypatch)
    caplog.set_level("INFO", logger=api_telemetry.__name__)
    hostile = "evil\nFAKE LOG LINE\x1b[31m" + "x" * 500
    _client().post(
        "/api/v1/telemetry/events",
        json=_batch([_event(hostile), {"n": 42}, "not-an-object"]),
    )
    [msg] = [r.getMessage() for r in caplog.records if "dropped" in r.getMessage()]
    assert "\n" not in msg and "\x1b" not in msg
    # truncated: nowhere near the 500-char payload
    assert len(msg) < 200
    assert "evil?FAKE?LOG?LINE?" in msg
    assert "<non-string-name>" in msg and "<non-object>" in msg


def test_drop_log_caps_name_count():
    names = [f"n{i}" for i in range(25)]
    out = api_telemetry._dropped_summary(names)
    assert out.startswith("n0, n1,")
    assert out.endswith(f"(+{25 - api_telemetry._DROP_LOG_MAX_NAMES} more)")
    assert "n10" not in out.split(" (+")[0].split(", ")
