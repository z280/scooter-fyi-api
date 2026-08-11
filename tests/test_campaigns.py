"""Campaign attribution — the bounded-vocabulary guarantee.

The whole point of src/campaigns.py is that telemetry_events.campaign can
never carry client-controlled free text: absent tags become 'none',
anything malformed/unknown/archived becomes the literal 'other', and only
a code an admin actually registered survives. These tests pin resolve()
and normalize_code(), plus the ingest wiring in api_telemetry (the `cmp`
page field), with recorded fakes — no Postgres.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_telemetry, campaigns


class _CampaignCursor:
    """fetchone() answers the campaigns lookup from a fixed live set, and
    the telemetry salt query with a salt — keyed off the last SQL run."""

    def __init__(self, live_codes, calls):
        self.live_codes = live_codes
        self.calls = calls
        self._last_sql = ""
        self._last_params = None

    def execute(self, sql, params=None):
        self.calls.append(("execute", sql, params))
        self._last_sql = sql
        self._last_params = params

    def executemany(self, sql, rows):
        self.calls.append(("executemany", sql, rows))

    def fetchone(self):
        if "FROM campaigns" in self._last_sql:
            code = self._last_params[0]
            return (1,) if code in self.live_codes else None
        return ("test-salt",)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --- resolve() / normalize_code() ------------------------------------------


def _resolve(raw, live={"sticker-2026"}):
    cur = _CampaignCursor(live, [])
    return campaigns.resolve(cur, raw)


def test_absent_tag_is_none():
    assert _resolve(None) == "none"
    assert _resolve("") == "none"


def test_live_code_survives_and_is_normalized():
    assert _resolve("sticker-2026") == "sticker-2026"
    assert _resolve("  STICKER-2026  ") == "sticker-2026"


def test_unknown_code_collapses_to_other():
    assert _resolve("never-registered") == "other"


def test_malformed_tags_collapse_to_other_not_stored():
    for junk in ("has space", "x" * 41, "-leads-dash", "<script>", 42, True):
        assert _resolve(junk) == "other", junk


def test_sentinels_are_not_valid_codes():
    # A visitor sending cmp=none / cmp=other must not masquerade as
    # untagged or mint a fake registered campaign.
    assert campaigns.normalize_code("none") is None
    assert campaigns.normalize_code("other") is None
    assert _resolve("none") == "other"


def test_malformed_tag_never_queries_the_registry():
    calls = []
    cur = _CampaignCursor(set(), calls)
    campaigns.resolve(cur, "not a slug")
    assert not calls


# --- ingest wiring (api_telemetry `cmp` page field) -------------------------


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass


def _post_batch(monkeypatch, page, live_codes):
    calls: list = []
    cur = _CampaignCursor(live_codes, calls)

    @contextmanager
    def fake_connection():
        yield _Conn(cur)

    monkeypatch.setattr(api_telemetry, "connection", fake_connection)
    monkeypatch.setattr(api_telemetry, "enforce", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(api_telemetry.router)
    r = TestClient(app).post(
        "/api/v1/telemetry/events",
        json={
            "v": 1,
            "page": page,
            "events": [
                {
                    "n": "page_load",
                    "t": int(datetime.now(timezone.utc).timestamp() * 1000),
                    "sid": "sess-abc",
                }
            ],
        },
    )
    assert r.status_code == 204
    [rows] = [
        rows
        for kind, sql, rows in calls
        if kind == "executemany" and "telemetry_events" in sql
    ]
    return rows


_PAGE = {"vp": "md", "dc": "mobile", "os": "ios", "ref": "direct", "auth": False}


def test_ingest_stamps_live_campaign_on_every_row(monkeypatch):
    rows = _post_batch(
        monkeypatch,
        {**_PAGE, "cmp": "sticker-2026"},
        live_codes={"sticker-2026"},
    )
    assert all(row[10] == "sticker-2026" for row in rows)


def test_ingest_collapses_unknown_campaign_to_other(monkeypatch):
    rows = _post_batch(monkeypatch, {**_PAGE, "cmp": "bogus"}, live_codes=set())
    assert all(row[10] == "other" for row in rows)


def test_ingest_defaults_untagged_to_none(monkeypatch):
    rows = _post_batch(monkeypatch, _PAGE, live_codes={"sticker-2026"})
    assert all(row[10] == "none" for row in rows)
