"""Postgres-backed coverage for royalty titles, ruling colours and the
generated display_name (sql/044), and for the v2 palette, the retirement
of v1 and the emoji-matched auto-assignment on top of them (sql/107).

Everything here depends on schema the app cannot fake: FK membership in
the curated lists, the unique index over the (fill, border) PAIR, a
GENERATED column, and a plpgsql function whose whole job is to choose
against live data. See tests/test_user_preferences_pg.py for the fixture
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

# Three arbitrary SELECTABLE palette entries, taken from sql/107's v2
# seed. Named here rather than SELECTed so a test failure points at a
# colour, not a query. They must stay selectable: a retired colour is
# refused on save (src/api_profile.py:_reject_retired_colours), which is
# what _V1_RETIRED below is for.
_RED = "#f93534"     # red-500
_BLUE = "#3bacff"    # blue-600
_GREEN = "#27a900"   # green-500

# One of sql/044's colours that sql/107 retired — still a row in
# ruling_colors (riders hold these), no longer offered or claimable.
_V1_RETIRED = "#c53637"   # v1's red-500


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
def test_palette_entries_are_distinct(pg_conn):
    """A duplicate hex would silently shorten the palette via ON CONFLICT,
    so distinctness is asserted, not assumed.

    Hex over the WHOLE table — it is the primary key and the thing riders
    hold. Names only within the offered palette: v1 and v2 both have a
    'red-500' (different colours, a generation apart), which is fine
    because a picker only ever shows one generation plus whatever the
    caller holds, and src/api_lexicon.py flags that exception as
    `retired`."""
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT hex) FROM ruling_colors")
        total, distinct_hex = cur.fetchone()
        assert distinct_hex == total, "palette contains duplicate hex values"
        cur.execute(
            "SELECT COUNT(*), COUNT(DISTINCT name) FROM ruling_colors WHERE selectable"
        )
        offered, distinct_name = cur.fetchone()
    assert distinct_name == offered, "offered palette contains duplicate names"


def test_the_offered_palette_is_v2_only(pg_conn):
    """sql/107 retires sql/044's 128 and offers its own. Both halves
    matter: too few selectable colours and the picker is bare, any v1
    colour left selectable and the conflict filter was for nothing."""
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM ruling_colors WHERE selectable")
        assert cur.fetchone()[0] == 74
        cur.execute(
            "SELECT COUNT(*) FROM ruling_colors WHERE selectable AND hex = %s",
            (_V1_RETIRED,),
        )
        assert cur.fetchone()[0] == 0


def test_every_offered_colour_belongs_to_an_offered_hue_family(pg_conn):
    """The assigner walks the hue wheel to find a fallback family; a
    selectable colour in a retired family would be a destination it never
    reaches, and a family it reaches with nothing in it."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT c.hue_family FROM ruling_colors c "
            "  JOIN ruling_hue_families f ON f.family = c.hue_family "
            " WHERE c.selectable AND NOT f.selectable"
        )
        assert cur.fetchall() == []


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
        "ruling_color": _RED, "ruling_border_color": _BLUE,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["ruling_color"], body["ruling_border_color"]) == (_RED, _BLUE)


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
        "ruling_color": _RED, "ruling_border_color": _BLUE,
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


@pytest.mark.parametrize("alpha", [0.0, 0.6, 1.5, None])
def test_a_stale_client_sending_ruling_alpha_is_ignored_not_refused(pg_conn, alpha):
    """sql/085 dropped the per-rider fill opacity. A client built before
    that may still send the field; it is an unknown key now, so it is
    ignored (200) rather than refused, and it is never echoed back."""
    c, _ = _client(pg_conn)
    r = c.put("/api/v1/profile", json={
        "ruling_color": _RED, "ruling_border_color": _BLUE, "ruling_alpha": alpha,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["ruling_color"], body["ruling_border_color"]) == (_RED, _BLUE)
    assert "ruling_alpha" not in body
    assert "ruling_alpha" not in c.get("/api/v1/profile").json()


# ---------------------------------------------------------------------------
# Pickers
# ---------------------------------------------------------------------------
def test_ruling_colors_endpoint_reports_claimed_pairs(pg_conn):
    c, _ = _client(pg_conn)
    before = c.get("/api/v1/ruling-colors").json()
    # The offered palette, not the whole table: v1's retired colours are
    # still rows, and this caller holds none of them.
    assert len(before["ruling_colors"]) == 74
    assert all(not col["retired"] for col in before["ruling_colors"])
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


# ---------------------------------------------------------------------------
# Retired colours (sql/107)
# ---------------------------------------------------------------------------
def test_a_retired_colour_is_refused(pg_conn):
    """v1's colours are still valid rows — riders hold them — but they are
    no longer claimable: ten of the 128 sit close enough to a zone fill or
    the ride trail to read as a map feature."""
    c, _ = _client(pg_conn)
    r = c.put("/api/v1/profile", json={
        "ruling_color": _V1_RETIRED, "ruling_border_color": _BLUE,
    })
    assert r.status_code == 400
    assert "no longer offered" in r.json()["detail"]


def test_a_retired_colour_you_already_hold_is_still_saveable(pg_conn):
    """The exception that makes the rule liveable: a v1 holder changing
    only their border (or taking a title) must not be told their own fill
    is invalid."""
    c, account_id = _client(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts SET ruling_color = %s, ruling_border_color = %s "
            "WHERE id = %s",
            (_V1_RETIRED, _BLUE, account_id),
        )
    pg_conn.commit()

    r = c.put("/api/v1/profile", json={
        "ruling_color": _V1_RETIRED, "ruling_border_color": _GREEN,
    })
    assert r.status_code == 200
    assert r.json()["ruling_color"] == _V1_RETIRED


def test_the_picker_offers_back_a_retired_colour_you_hold(pg_conn):
    """...and GET /api/v1/ruling-colors has to include it, or the editor
    renders a grid with a hole where the rider's own colour should be."""
    c, account_id = _client(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts SET ruling_color = %s, ruling_border_color = %s "
            "WHERE id = %s",
            (_V1_RETIRED, _BLUE, account_id),
        )
    pg_conn.commit()

    colors = c.get("/api/v1/ruling-colors").json()["ruling_colors"]
    mine = [col for col in colors if col["hex"] == _V1_RETIRED]
    assert mine and mine[0]["retired"] is True
    assert len(colors) == 75  # the 74 offered, plus the one held


# ---------------------------------------------------------------------------
# Automatic colours (sql/107's assign_ruling_colors)
# ---------------------------------------------------------------------------
def _colours_of(pg_conn, account_id: int) -> tuple[str | None, str | None]:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT ruling_color, ruling_border_color FROM accounts WHERE id = %s",
            (account_id,),
        )
        return cur.fetchone()


def test_a_new_account_is_coloured_the_moment_it_is_named(pg_conn):
    """upsert_account mints a username; assign_public_username hands the
    account the colours that emoji suggests. Nobody renders as a grey
    ghost waiting to discover the profile editor."""
    _c, account_id = _client(pg_conn)
    fill, border = _colours_of(pg_conn, account_id)
    assert fill and border and fill != border


def test_the_fill_matches_the_emoji_s_hue_family(pg_conn):
    """🐸 rules in green, 🦉 in amber. Asserted against the mapping rather
    than a fixed hex so extending the palette doesn't break the test."""
    _c, account_id = _client(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT n.hue_family, f.hue_family, b.hue_family
              FROM accounts a
              JOIN emoji_nouns n ON n.emoji = a.username_emoji
              JOIN ruling_colors f ON f.hex = a.ruling_color
              JOIN ruling_colors b ON b.hex = a.ruling_border_color
             WHERE a.id = %s
            """,
            (account_id,),
        )
        wanted, fill_family, _border_family = cur.fetchone()
    # With one account in play nothing has been claimed ahead of it, so it
    # gets its first choice. (Under contention the assigner widens to the
    # neighbouring family — that is what test_colours_stay_unique covers.)
    assert fill_family == wanted


def test_the_border_is_darker_than_the_fill(pg_conn):
    """A border lighter than its fill reads as a halo, not an edge."""
    _c, account_id = _client(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT f.lightness_step, b.lightness_step FROM accounts a "
            "  JOIN ruling_colors f ON f.hex = a.ruling_color "
            "  JOIN ruling_colors b ON b.hex = a.ruling_border_color "
            " WHERE a.id = %s",
            (account_id,),
        )
        fill_step, border_step = cur.fetchone()
    assert border_step <= fill_step - 200


def test_every_assigned_colour_is_one_that_is_still_offered(pg_conn):
    """The assigner must never hand out a retired colour — the whole point
    of retiring them is that they read as a map feature."""
    _c, account_id = _client(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT f.selectable, b.selectable FROM accounts a "
            "  JOIN ruling_colors f ON f.hex = a.ruling_color "
            "  JOIN ruling_colors b ON b.hex = a.ruling_border_color "
            " WHERE a.id = %s",
            (account_id,),
        )
        assert cur.fetchone() == (True, True)


def test_colours_stay_unique_across_many_assignments(pg_conn):
    """accounts_ruling_pair_key is the constraint; the assigner filtering
    taken pairs and re-checking under an advisory lock is what keeps it
    from ever firing. Twenty accounts is well past the point where the
    same-family pairs for a popular hue run out."""
    ids = [_client(pg_conn)[1] for _ in range(20)]
    pairs = [_colours_of(pg_conn, account_id) for account_id in ids]
    assert all(fill and border for fill, border in pairs)
    assert len(set(pairs)) == len(pairs)


def test_the_assigner_never_overwrites_a_claim(pg_conn):
    c, account_id = _client(pg_conn)
    c.put("/api/v1/profile", json={"ruling_color": _RED, "ruling_border_color": _BLUE})
    with pg_conn.cursor() as cur:
        cur.execute("SELECT assign_ruling_colors(%s)", (account_id,))
        assert cur.fetchone()[0] is False
    assert _colours_of(pg_conn, account_id) == (_RED, _BLUE)


def test_an_account_with_no_emoji_is_left_alone(pg_conn):
    """The suggestion comes from the username; an account that has not got
    one yet (pre-sql/025, never backfilled) has nothing to match on."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (email) VALUES (%s) RETURNING id",
            (f"pgtest-identity-{uuid.uuid4()}@example.com",),
        )
        account_id = cur.fetchone()[0]
        cur.execute("SELECT assign_ruling_colors(%s)", (account_id,))
        assert cur.fetchone()[0] is False
    pg_conn.commit()
    assert _colours_of(pg_conn, account_id) == (None, None)


def test_every_emoji_has_a_hue_family(pg_conn):
    """An unmapped emoji still gets colours (the assigner hashes it), but
    they are a stable accident rather than a match — so the seed list is
    expected to be complete."""
    with pg_conn.cursor() as cur:
        cur.execute("SELECT emoji FROM emoji_nouns WHERE hue_family IS NULL")
        assert cur.fetchall() == []
