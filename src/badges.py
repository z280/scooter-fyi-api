"""Server-computed profile badges (API_REQUIREMENTS.md §4.3).

Recomputed on every profile read — no stored badge state, so thresholds
can be tuned without migrations and retroactively apply. Earned badges
are available to every account; only `supporter` is tied to payment.

This lands with §2 carrying just the supporter badge; the report/ride
badges are added by the §3/§4 changes as their tables appear.
"""

from __future__ import annotations

from typing import Any


def compute_badges(cur, account_id: int, *, supporter: bool) -> list[dict[str, Any]]:
    badges: list[dict[str, Any]] = []

    if supporter:
        cur.execute(
            "SELECT supporter_since FROM accounts WHERE id = %s", (account_id,)
        )
        row = cur.fetchone()
        badges.append({
            "id": "supporter",
            "label": "Supporter",
            "earned_at": row[0].isoformat() if row and row[0] else None,
        })

    return badges
