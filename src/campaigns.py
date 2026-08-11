"""Marketing-campaign registry and attribution (sql/074_campaigns.sql).

Campaigns are links WE publish (QR stickers, social posts, email) tagged
with ?utm_campaign=<code>. The frontend forwards the code with telemetry
batches, and ingest stamps it onto events — but only after resolving it
against this registry, because the analytics schema's privacy contract
(sql/061) forbids unbounded client-controlled text:

    absent / not a string / malformed  ->  'none'   (untagged traffic)
    well-formed but unknown/archived   ->  'other'  (tagged, unattributed)
    live campaign code                 ->  the code itself

So the telemetry_events.campaign column stays a bounded vocabulary no
matter what a client sends. Codes are slugs (CODE_RE), enforced here in
code per house convention (sql/043): product limits move, DDL doesn't.

Managed from /admin/campaigns (src/api_admin.py) by GitHub-OAuth
operators; nothing here touches rider accounts or identity.
"""

from __future__ import annotations

import re

from .pg import connection

# Sentinels for telemetry_events.campaign. NONE marks untagged traffic;
# UNKNOWN marks tagged traffic whose code we don't (or no longer) run.
NONE = "none"
UNKNOWN = "other"

# Lowercase slug, 1-40 chars, must start alphanumeric. The sentinels
# above intentionally match this shape but are refused as codes.
CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

MAX_NAME_CHARS = 120
MAX_CHANNEL_CHARS = 40
MAX_NOTES_CHARS = 500


def normalize_code(raw: object) -> str | None:
    """The client-sent value as a candidate code, or None if malformed."""
    if not isinstance(raw, str):
        return None
    code = raw.strip().lower()
    if code in (NONE, UNKNOWN) or not CODE_RE.match(code):
        return None
    return code


def resolve(cur, raw: object) -> str:
    """Map a client-sent utm_campaign value to the bounded vocabulary."""
    if raw is None or raw == "":
        return NONE
    code = normalize_code(raw)
    if code is None:
        # A tag was present but unusable — count it as unattributed
        # tagged traffic rather than pretending the visit was untagged.
        return UNKNOWN
    cur.execute(
        "SELECT 1 FROM campaigns WHERE code = %s AND archived_at IS NULL",
        (code,),
    )
    return code if cur.fetchone() else UNKNOWN


def create(
    code: str, name: str, channel: str, notes: str, created_by: str
) -> bool:
    """Register a campaign. False if the code is already taken.

    Raises ValueError on a malformed code — the admin form should have
    caught it, so surfacing beats silently mangling.
    """
    normalized = normalize_code(code)
    if normalized is None:
        raise ValueError("campaign code must be a slug: a-z 0-9 - _, max 40")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO campaigns (code, name, channel, notes, created_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (code) DO NOTHING
                """,
                (
                    normalized,
                    name.strip()[:MAX_NAME_CHARS],
                    channel.strip()[:MAX_CHANNEL_CHARS],
                    notes.strip()[:MAX_NOTES_CHARS],
                    created_by,
                ),
            )
            created = cur.rowcount == 1
        conn.commit()
    return created


def set_archived(code: str, archived: bool) -> bool:
    """Archive (stop attributing) or reactivate a campaign."""
    with connection() as conn:
        with conn.cursor() as cur:
            if archived:
                cur.execute(
                    "UPDATE campaigns SET archived_at = NOW() "
                    "WHERE code = %s AND archived_at IS NULL",
                    (code,),
                )
            else:
                cur.execute(
                    "UPDATE campaigns SET archived_at = NULL "
                    "WHERE code = %s AND archived_at IS NOT NULL",
                    (code,),
                )
            changed = cur.rowcount == 1
        conn.commit()
    return changed


def list_campaigns() -> list[dict]:
    """All campaigns, live first, newest first within each group."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT code, name, channel, notes, created_by,
                       created_at, archived_at
                FROM campaigns
                ORDER BY archived_at IS NOT NULL, created_at DESC
                """
            )
            return [
                {
                    "code": r[0],
                    "name": r[1],
                    "channel": r[2],
                    "notes": r[3],
                    "created_by": r[4],
                    "created_at": r[5],
                    "archived_at": r[6],
                }
                for r in cur.fetchall()
            ]
