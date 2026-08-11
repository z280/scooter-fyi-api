"""A memorable name for every scooter — "Lunar 🐸", "Warp Drive 💿".

Riders already get one (``Resourceful 🌈``, from sfw_adjectives × emoji_nouns).
Vehicles get the same shape from a different vocabulary — space_words ×
emoji_nouns, sql/073 — so the two are instantly distinguishable as species
while feeling like they belong to the same product.

DERIVED, NOT ASSIGNED. src/accounts.py assigns usernames from a pool with an
advisory lock and a uniqueness constraint, because an account's name is
identity: it must be unique and it must survive. A scooter's name is a
*label* — it exists so a rider standing in front of four of them can say
"the Lunar one". Deriving it from the vehicle_identifier means:

  * no assignment job, no table to keep in sync, no lock;
  * it never changes, for the life of the vehicle, without being stored;
  * a redeployed or re-plated scooter simply becomes a new vehicle, which is
    already how every other table here treats it.

The cost is that duplicates are possible: ~8,000 vehicles drawn from 33,485
combinations collide roughly a thousand times city-wide. That is fine for a
label and would not be fine for identity. What matters is the handful a rider
can see at once, where a collision is vanishingly unlikely — and the plate
suffix disambiguates wherever it is permitted to appear.

THE PLATE SUFFIX IS NOT PUBLIC. src/identity.py is emphatic that the raw plate
never crosses an unauthenticated wire, and records that a previous promotion of
it to the public endpoint "was later reverted". Publishing the last three
digits for all ~8,000 vehicles would partition the fleet into ~1,000 buckets
which, combined with model and position, is often a unique identification — a
real erosion of the HMAC the whole privacy model rests on. So:

    public / bulk map     "Lunar 🐸"                 name only
    plate-permitted paths "Lunar 🐸 928"             the same call sites that
                                                     already emit vehicle_plate

which keeps the rider-facing benefit (matching the app to the scooter in front
of you) without handing a scraper a partial plate for the entire fleet.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache

from .pg import connection

# Fallbacks used only if the vocabulary tables are unreachable, so a database
# hiccup degrades the name rather than failing the map payload.
_FALLBACK_WORDS = ("Lunar", "Solar", "Orbital", "Cosmic", "Stellar")
_FALLBACK_EMOJI = ("🚀", "🛰", "🌙", "⭐", "🪐")


@lru_cache(maxsize=1)
def _vocab() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(space words, emoji), sorted so the mapping is stable across processes.

    Cached for the process lifetime: the tables are seeded by migration and
    never written at runtime, and this is called once per device per map
    payload — ~8,000 times a request.
    """
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT word FROM space_words ORDER BY word")
                words = tuple(r[0] for r in cur.fetchall())
                cur.execute("SELECT emoji FROM emoji_nouns ORDER BY emoji")
                emoji = tuple(r[0] for r in cur.fetchall())
    except Exception:  # noqa: BLE001 — a name is never worth failing a map for
        return _FALLBACK_WORDS, _FALLBACK_EMOJI
    if not words or not emoji:
        return _FALLBACK_WORDS, _FALLBACK_EMOJI
    return words, emoji


def public_name(vehicle_identifier: str | None) -> str | None:
    """"Lunar 🐸" — stable for the life of the vehicle, no storage.

    Two INDEPENDENT slices of a fresh digest, not the identifier's own hex:
    the identifier is already an HMAC, but slicing one value twice would
    correlate the word and the emoji and shrink the effective vocabulary.
    """
    if not vehicle_identifier:
        return None
    words, emoji = _vocab()
    digest = hashlib.sha256(vehicle_identifier.encode()).digest()
    word = words[int.from_bytes(digest[0:8], "big") % len(words)]
    icon = emoji[int.from_bytes(digest[8:16], "big") % len(emoji)]
    return f"{word} {icon}"


def plate_suffix(vehicle_plate: str | None) -> str | None:
    """Last three characters of the plate — what is printed on the scooter.

    ONLY for call sites already permitted to emit the plate itself. See the
    module docstring: this is deliberately absent from the public map payload.
    """
    if not vehicle_plate:
        return None
    digits = "".join(ch for ch in str(vehicle_plate) if ch.isalnum())
    return digits[-3:] if len(digits) >= 3 else (digits or None)


def display_name(vehicle_identifier: str | None,
                 vehicle_plate: str | None = None) -> str | None:
    """"Lunar 🐸 928" when the plate is permitted, "Lunar 🐸" when it is not."""
    name = public_name(vehicle_identifier)
    if name is None:
        return None
    suffix = plate_suffix(vehicle_plate)
    return f"{name} {suffix}" if suffix else name
