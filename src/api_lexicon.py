"""Word-list reference data behind the public_username feature (sql/025,
accounts.generate_public_username/choose_public_username).

    GET /api/v1/emoji-nouns            full emoji -> noun-word list
    GET /api/v1/emoji-nouns/search     partial, case-insensitive word match
    GET /api/v1/adjectives             full adjective list
    GET /api/v1/adjectives/search      partial, case-insensitive match

A client uses these to build a picker for PUT /api/v1/profile/username
(choosing a specific adjective/emoji) rather than only being able to
re-roll a random pair.

Every endpoint in this project requires a signed-in rider (project-wide
convention, not a sensitivity call on this data) — see
accounts.require_session.

No pg_trgm/citext in this repo (accounts.normalize_email's convention
note) and no index is needed either: each table is a few hundred curated
rows, so a sequential scan with ILIKE '%term%' is effectively instant, and
a leading-wildcard pattern couldn't use a btree index anyway.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from .accounts import SessionUser, require_session
from .pg import connection

router = APIRouter()


@router.get("/api/v1/emoji-nouns")
def list_emoji_nouns(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    """The full emoji -> noun-word reference list."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT emoji, word FROM emoji_nouns ORDER BY word")
            rows = cur.fetchall()
    return {"emoji_nouns": [{"emoji": emoji, "word": word} for emoji, word in rows]}


@router.get("/api/v1/emoji-nouns/search")
def search_emoji_nouns(
    user: SessionUser = Depends(require_session),
    q: str = Query(..., min_length=1, max_length=64, description="Partial word match, e.g. 'owl'"),
) -> dict[str, Any]:
    """Case-insensitive substring match on the noun word."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT emoji, word FROM emoji_nouns WHERE word ILIKE %s ORDER BY word",
                (f"%{q}%",),
            )
            rows = cur.fetchall()
    return {"emoji_nouns": [{"emoji": emoji, "word": word} for emoji, word in rows]}


@router.get("/api/v1/adjectives")
def list_adjectives(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    """The full curated adjective list."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT word FROM sfw_adjectives ORDER BY word")
            rows = cur.fetchall()
    return {"adjectives": [word for (word,) in rows]}


@router.get("/api/v1/adjectives/search")
def search_adjectives(
    user: SessionUser = Depends(require_session),
    q: str = Query(..., min_length=1, max_length=64, description="Partial word match, e.g. 'bra'"),
) -> dict[str, Any]:
    """Case-insensitive substring match on the adjective."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT word FROM sfw_adjectives WHERE word ILIKE %s ORDER BY word",
                (f"%{q}%",),
            )
            rows = cur.fetchall()
    return {"adjectives": [word for (word,) in rows]}


@router.get("/api/v1/royalty-titles")
def list_royalty_titles(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    """The curated titles that can prefix a public username (sql/044).

    Ordered by the list's own sort_order, not alphabetically: the seed
    groups related titles together (the gendered pairs and their neutral
    form adjacent), which is the order a picker wants to render.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT title FROM royalty_titles ORDER BY sort_order, title")
            rows = cur.fetchall()
    return {"royalty_titles": [title for (title,) in rows]}


@router.get("/api/v1/royalty-titles/search")
def search_royalty_titles(
    user: SessionUser = Depends(require_session),
    q: str = Query(..., min_length=1, max_length=64, description="Partial match, e.g. 'high'"),
) -> dict[str, Any]:
    """Case-insensitive substring match on the title."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title FROM royalty_titles WHERE title ILIKE %s "
                "ORDER BY sort_order, title",
                (f"%{q}%",),
            )
            rows = cur.fetchall()
    return {"royalty_titles": [title for (title,) in rows]}


@router.get("/api/v1/ruling-colors")
def list_ruling_colors(user: SessionUser = Depends(require_session)) -> dict[str, Any]:
    """The curated leaderboard-map palette, plus the pairs already claimed.

    `taken_pairs` exists so a picker can grey out unavailable combinations
    instead of discovering them by 409 on save. It is bounded by the number
    of accounts that have chosen colours — NOT by the 16 256 possible
    pairs — so it stays small no matter how large the palette gets.

    Which accounts hold which pair is deliberately NOT exposed: the
    frontend needs to know a combination is gone, not who has it. That
    would be a cross-account identity read this endpoint has no reason to
    offer, and it would leak past show_public_username.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT hex, name, hue_family FROM ruling_colors ORDER BY sort_order"
            )
            colors = cur.fetchall()
            cur.execute(
                "SELECT ruling_color, ruling_border_color FROM accounts "
                "WHERE ruling_color IS NOT NULL AND ruling_border_color IS NOT NULL"
            )
            taken = cur.fetchall()
    return {
        "ruling_colors": [
            {"hex": hex_value, "name": name, "hue_family": family}
            for hex_value, name, family in colors
        ],
        "taken_pairs": [
            {"fill": fill, "border": border} for fill, border in taken
        ],
    }
