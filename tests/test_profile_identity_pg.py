"""Postgres-backed coverage for royalty titles, ruling colours and the
generated display_name (sql/044).

Everything here depends on schema the app cannot fake: FK membership in
the curated lists, the unique index over the (fill, border) PAIR, and a
GENERATED column. See tests/test_user_preferences_pg.py for the fixture
contract — same VEO_TEST_PG_DSN rules, same warning about production.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

psycopg = pytest.importorskip("psycopg")

from src import api_lexicon, api_profile  # noqa: E402
from src.accounts import SessionUser, require_session, upsert_account  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_TEST_EMAIL_LIKE = "pgtest-identity-%@example.com"

# Two arbitrary palette entries, taken from the generated seed. Named here
# rather than SELECTed so a test failure points at a colour, not a query.
_RED = "#c53637"     # red-500
_BLUE = "#026fd7"    # blue-500
_GREEN = "#008a23"   # green-500


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture()
def pg_conn(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — identity Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM accounts WHERE email LIKE %s", (_TEST_EMAIL_LIKE,))
        # A (fill, border) claim is GLOBAL — unlike a saved map setting, it
        # is not scoped to an account, so deleting this file's own accounts
        # is not enough to guarantee the pairs below are free. Any account
        # in the database, seeded by anything, can be holding one. Release
        # every claim so each run starts from a known state; nothing else
        # in the suite asserts a colour survives across tests.
        cur.execute(
            "UPDATE accounts SET ruling_color = NULL, ruling_border_color = NULL "
            "WHERE ruling_color IS NOT NULL"
        )
    conn.commit()

    @contextmanager
    def _fake_connection():
        yield conn

    for module in (api_profile, api_lexicon):
        monkeypatch.setattr(module, "connection", _fake_connection)
    monkeypatch.setattr(api_profile, "enforce", lambda cur, **kw: None)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _client(pg_conn) -> tuple[TestClient, int]:
    with pg_conn.cursor() as cur:
        account_id = upsert_account(cur, f"pgtest-identity-{uuid.uuid4()}@example.com")
    pg_conn.commit()
    user = SessionUser(
        account_id=account_id, email="pgtest-identity@example.com", scopes=("rider",),
        expires_at=None, sliding=True, method="google", token_sha256="x",
    )
    app = FastAPI()
    app.include_router(api_profile.router)
    app.include_router(api_lexicon.router)
    app.dependency_overrides[require_session] = lambda: user
    return TestClient(app), account_id


# ---------------------------------------------------------------------------
# Palette integrity
# ---------------------------------------------------------------------------
def test_palette_has_at_least_128_distinct_colours(pg_conn):
    """The operator asked for at least 128 options. A duplicate hex would
    silently shorten the palette via ON CONFLICT DO NOTHING, so distinctness
    is asserted, not assumed."""
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT hex), COUNT(DISTINCT name) FROM ruling_colors")
        total, distinct_hex, distinct_name = cur.fetchone()
    assert total >= 128, f"palette has only {total} colours"
    assert distinct_hex == total, "palette contains duplicate hex values"
    assert distinct_name == total, "palette contains duplicate names"


def test_every_palette_colour_is_lowercase_six_digit_hex(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM ruling_colors WHERE hex !~ '^#[0-9a-f]{6}$'")
        assert cur.fetchone()[0] == 0


def test_palette_avoids_the_unusable_extremes(pg_conn):
    """A near-white fill vanishes under 60% alpha on a light basemap and a
    near-black one is indistinguishable from map ink. The generator's
    lightness bounds are what keep both out; this pins the outcome."""
    with pg_conn.cursor() as cur:
        cur.execute("SELECT hex FROM ruling_colors")
        hexes = [r[0] for r in cur.fetchall()]
    for hex_value in hexes:
        r, g, b = (int(hex_value[i:i + 2], 16) / 255 for i in (1, 3, 5))
        luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
        assert 0.05 <= luminance <= 0.90, f"{hex_value} is too close to black or white"


# ---------------------------------------------------------------------------
# display_name
# ---------------------------------------------------------------------------
def test_display_name_prefixes_the_title(pg_conn):
    c, account_id = _client(pg_conn)
    before = c.get("/api/v1/profile").json()
    assert before["display_name"] == before["public_username"], (
        "with no title, display_name should just be the username"
    )

    r = c.put("/api/v1/profile", json={"royalty_title": "Queen"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["display_name"] == f"Queen {body['public_username']}"


def test_clearing_the_title_reverts_display_name(pg_conn):
    c, _ = _client(pg_conn)
    c.put("/api/v1/profile", json={"royalty_title": "Sir"})
    body = c.put("/api/v1/profile", json={"royalty_title": None}).json()
    assert body["royalty_title"] is None
    assert body["display_name"] == body["public_username"]


def test_display_name_tracks_a_username_re_roll(pg_conn):
    """display_name is generated from the parts, so it cannot drift out of
    sync with the username the way a cached copy would."""
    c, _ = _client(pg_conn)
    c.put("/api/v1/profile", json={"royalty_title": "Duke"})
    new_username = c.post("/api/v1/profile/username/regenerate").json()["public_username"]
    assert c.get("/api/v1/profile").json()["display_name"] == f"Duke {new_username}"


def test_an_unknown_title_is_refused(pg_conn):
    c, _ = _client(pg_conn)
    r = c.put("/api/v1/profile", json={"royalty_title": "Supreme Overlord"})
    assert r.status_code == 400
    assert "available titles" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Ruling colours
# ---------------------------------------------------------------------------
def test_colours_round_trip(pg_conn):
    c, _ = _client(pg_conn)
    r = c.put("/api/v1/profile", json={
        "ruling_color": _RED, "ruling_border_color": _BLUE, "ruling_alpha": 0.4,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["ruling_color"], body["ruling_border_color"]) == (_RED, _BLUE)
    assert body["ruling_alpha"] == pytest.approx(0.4)


def test_a_claimed_pair_is_409_for_everyone_else(pg_conn):
    first, _ = _client(pg_conn)
    assert first.put("/api/v1/profile", json={
        "ruling_color": _RED, "ruling_border_color": _BLUE,
    }).status_code == 200

    second, _ = _client(pg_conn)
    r = second.put("/api/v1/profile", json={
        "ruling_color": _RED, "ruling_border_color": _BLUE,
    })
    assert r.status_code == 409
    assert "already claimed" in r.json()["detail"]


def test_sharing_one_half_of_the_pair_is_allowed(pg_conn):
    """Uniqueness is on the PAIR — 128 colours would otherwise cap the
    feature at 128 riders."""
    first, _ = _client(pg_conn)
    first.put("/api/v1/profile", json={"ruling_color": _RED, "ruling_border_color": _BLUE})

    second, _ = _client(pg_conn)
    assert second.put("/api/v1/profile", json={
        "ruling_color": _RED, "ruling_border_color": _GREEN,
    }).status_code == 200

    third, _ = _client(pg_conn)
    assert third.put("/api/v1/profile", json={
        "ruling_color": _GREEN, "ruling_border_color": _BLUE,
    }).status_code == 200


def test_re_saving_your_own_pair_is_not_a_conflict(pg_conn):
    c, _ = _client(pg_conn)
    c.put("/api/v1/profile", json={"ruling_color": _RED, "ruling_border_color": _BLUE})
    assert c.put("/api/v1/profile", json={
        "ruling_color": _RED, "ruling_border_color": _BLUE, "ruling_alpha": 0.9,
    }).status_code == 200


def test_clearing_colours_releases_the_pair(pg_conn):
    first, _ = _client(pg_conn)
    first.put("/api/v1/profile", json={"ruling_color": _RED, "ruling_border_color": _BLUE})
    assert first.put("/api/v1/profile", json={
        "ruling_color": None, "ruling_border_color": None,
    }).status_code == 200

    second, _ = _client(pg_conn)
    assert second.put("/api/v1/profile", json={
        "ruling_color": _RED, "ruling_border_color": _BLUE,
    }).status_code == 200, "a released pair stayed claimed"


def test_one_sided_colour_updates_are_refused(pg_conn):
    c, _ = _client(pg_conn)
    for payload in (
        {"ruling_color": _RED},
        {"ruling_border_color": _BLUE},
        {"ruling_color": _RED, "ruling_border_color": None},
    ):
        r = c.put("/api/v1/profile", json=payload)
        assert r.status_code == 400, f"{payload} should have been refused"


def test_border_may_not_equal_fill(pg_conn):
    c, _ = _client(pg_conn)
    r = c.put("/api/v1/profile", json={
        "ruling_color": _RED, "ruling_border_color": _RED,
    })
    assert r.status_code == 400
    assert "differ" in r.json()["detail"]


def test_a_colour_outside_the_palette_is_refused(pg_conn):
    c, _ = _client(pg_conn)
    r = c.put("/api/v1/profile", json={
        "ruling_color": "#123456", "ruling_border_color": _BLUE,
    })
    assert r.status_code == 400
    assert "available colours" in r.json()["detail"]


@pytest.mark.parametrize("alpha", [0.0, 0.05, 1.5])
def test_alpha_outside_the_range_is_refused(pg_conn, alpha):
    c, _ = _client(pg_conn)
    assert c.put("/api/v1/profile", json={"ruling_alpha": alpha}).status_code == 422


# ---------------------------------------------------------------------------
# Pickers
# ---------------------------------------------------------------------------
def test_ruling_colors_endpoint_reports_claimed_pairs(pg_conn):
    c, _ = _client(pg_conn)
    before = c.get("/api/v1/ruling-colors").json()
    assert len(before["ruling_colors"]) >= 128
    assert {"fill": _RED, "border": _BLUE} not in before["taken_pairs"]

    c.put("/api/v1/profile", json={"ruling_color": _RED, "ruling_border_color": _BLUE})

    after = c.get("/api/v1/ruling-colors").json()
    assert {"fill": _RED, "border": _BLUE} in after["taken_pairs"]
    # Who holds it is deliberately not exposed.
    assert all(set(p) == {"fill", "border"} for p in after["taken_pairs"])


def test_royalty_titles_endpoint_lists_and_searches(pg_conn):
    c, _ = _client(pg_conn)
    titles = c.get("/api/v1/royalty-titles").json()["royalty_titles"]
    assert "King" in titles and "Queen" in titles
    # Every gendered pair the operator named has its counterpart seeded.
    for a, b in (("King", "Queen"), ("Prince", "Princess"), ("Duke", "Duchess"),
                 ("His Highness", "Her Highness"), ("Sir", "Dame")):
        assert a in titles and b in titles, f"{a}/{b} pair incomplete"
    # ...and a neutral option exists for riders who want neither.
    assert {"Monarch", "Their Highness", "Noble"} <= set(titles)

    found = c.get("/api/v1/royalty-titles/search", params={"q": "highness"}).json()
    assert "His Highness" in found["royalty_titles"]
    assert all("highness" in t.lower() for t in found["royalty_titles"])
