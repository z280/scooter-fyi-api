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
