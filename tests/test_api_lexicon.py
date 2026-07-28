"""GET /api/v1/emoji-nouns[/search] + GET /api/v1/adjectives[/search]."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_lexicon
from src.accounts import SessionUser, require_session

_EMOJI_ROWS = [("🐶", "dog"), ("🦉", "owl"), ("🦁", "lion")]
_ADJECTIVE_ROWS = [("bold",), ("brave",), ("cool",)]
_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    expires_at=datetime.now(timezone.utc),
    sliding=True, method="google", token_sha256="x",
)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.queries.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.cur: _FakeCursor | None = None

    def cursor(self):
        self.cur = _FakeCursor(self._rows)
        return self.cur

    def commit(self):
        pass


def _app_with_auth():
    app = FastAPI()
    app.include_router(api_lexicon.router)
    app.dependency_overrides[require_session] = lambda: _USER
    return app


def _client(monkeypatch, rows):
    conn = _FakeConn(rows)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_lexicon, "connection", _fake_connection)
    return TestClient(_app_with_auth()), conn


def test_list_emoji_nouns_returns_all_rows(monkeypatch):
    c, _ = _client(monkeypatch, _EMOJI_ROWS)
    r = c.get("/api/v1/emoji-nouns")
    assert r.status_code == 200
    assert r.json() == {"emoji_nouns": [
        {"emoji": "🐶", "word": "dog"},
        {"emoji": "🦉", "word": "owl"},
        {"emoji": "🦁", "word": "lion"},
    ]}


def test_search_emoji_nouns_wraps_query_in_ilike_wildcards(monkeypatch):
    c, conn = _client(monkeypatch, _EMOJI_ROWS)
    r = c.get("/api/v1/emoji-nouns/search", params={"q": "owl"})
    assert r.status_code == 200
    sql, params = conn.cur.queries[-1]
    assert "ILIKE" in sql
    assert params[0] == "%owl%"


def test_search_emoji_nouns_requires_at_least_one_character():
    r = TestClient(_app_with_auth()).get("/api/v1/emoji-nouns/search", params={"q": ""})
    assert r.status_code == 422


def test_emoji_nouns_requires_signed_in_rider():
    app = FastAPI()
    app.include_router(api_lexicon.router)
    r = TestClient(app).get("/api/v1/emoji-nouns")
    assert r.status_code == 401


def test_list_adjectives_returns_all_rows(monkeypatch):
    c, _ = _client(monkeypatch, _ADJECTIVE_ROWS)
    r = c.get("/api/v1/adjectives")
    assert r.status_code == 200
    assert r.json() == {"adjectives": ["bold", "brave", "cool"]}


def test_search_adjectives_wraps_query_in_ilike_wildcards(monkeypatch):
    c, conn = _client(monkeypatch, _ADJECTIVE_ROWS)
    r = c.get("/api/v1/adjectives/search", params={"q": "bra"})
    assert r.status_code == 200
    sql, params = conn.cur.queries[-1]
    assert "ILIKE" in sql
    assert params[0] == "%bra%"


def test_adjectives_requires_signed_in_rider():
    app = FastAPI()
    app.include_router(api_lexicon.router)
    r = TestClient(app).get("/api/v1/adjectives")
    assert r.status_code == 401
