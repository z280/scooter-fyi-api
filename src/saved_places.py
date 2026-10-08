"""A rider's saved places: the shape, the validation, and the legacy fold-in.

WHAT A SAVED PLACE IS. The same thing the frontend's `favorites.ts` has always
stored on the device — an id, a label the rider chose, an emoji, and a
coordinate — now with somewhere to live that survives a new phone. The
frontend's four reserved "slots" (`slot:home`, `slot:work`, `slot:custom1`,
`slot:custom2`) are ordinary saved places with reserved ids, so this module
needs no special case for them and the server stays ignorant of a UI concept.

THE SERVER VALIDATES SHAPE, NOT MEANING. A label is anything printable within
a length; an id is anything within a length. The server is not going to have an
opinion about what a rider may call their own house, and a list of permitted
ids would be a second copy of a frontend concept that would then drift.

WHY THE FOLD-IN LIVES HERE rather than in a backfill script. Three plaintext
columns hold saved places today — `favorites`, the home pair and the work pair
— and the replacement is encrypted, which means Postgres cannot do the
migration and a script would have to hold the key against production with no
way to check its own work afterwards. Instead each rider's row is migrated the
next time they read their profile, and this is the function that does it.
"""

from __future__ import annotations

from typing import Any

# Matches the frontend's `MAX_FAVORITES` in favorites.ts. Not imported from
# anywhere — the two repos cannot share a constant — so the number is repeated
# with a note, and the server's job is to refuse absurd input rather than to be
# the authority on a UI cap.
MAX_SAVED_PLACES = 24
MAX_LABEL_LEN = 80
MAX_ID_LEN = 64
MAX_EMOJI_LEN = 16

# The reserved ids the frontend's slots use. Needed ONLY so the legacy
# home/work columns fold into the right rows — nothing else here treats them
# differently, and a rider is free to delete or rename them like any other.
SLOT_HOME_ID = "slot:home"
SLOT_WORK_ID = "slot:work"


def _num(value: Any) -> float | None:
    # `bool` is an `int` in Python, and `True` as a latitude is the kind of
    # thing that gets stored once and found years later.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if f == f and abs(f) != float("inf") else None


def clean_place(raw: Any) -> dict[str, Any] | None:
    """One place, validated, or None.

    Coordinates are RANGE-CHECKED and not merely parsed: a latitude of 412 is
    not a place, and storing it means a map somewhere later tries to fly to it.
    """
    if not isinstance(raw, dict):
        return None
    place_id = raw.get("id")
    label = raw.get("label")
    lat = _num(raw.get("lat"))
    lon = _num(raw.get("lon"))
    if not isinstance(place_id, str) or not place_id or len(place_id) > MAX_ID_LEN:
        return None
    if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL_LEN:
        return None
    if lat is None or not (-90 <= lat <= 90):
        return None
    if lon is None or not (-180 <= lon <= 180):
        return None
    emoji = raw.get("emoji")
    return {
        "id": place_id,
        "label": label.strip(),
        "emoji": emoji[:MAX_EMOJI_LEN] if isinstance(emoji, str) else "",
        "lat": lat,
        "lon": lon,
    }


def clean_places(raw: Any) -> list[dict[str, Any]]:
    """A whole list, validated.

    DROPS BAD ENTRIES RATHER THAN REFUSING THE LIST, which is the opposite of
    what the PUT handler does with a malformed body and is right for a
    different reason: this also runs over data we wrote ourselves years ago and
    over the legacy columns, where one unparseable row must not cost a rider
    every other place they saved.

    De-duplicated on id, last write winning, because the frontend's own store
    dedupes on id and a list with two `slot:home` rows would have no defined
    meaning on either side.
    """
    if not isinstance(raw, list):
        return []
    by_id: dict[str, dict[str, Any]] = {}
    for entry in raw:
        place = clean_place(entry)
        if place is not None:
            by_id[place["id"]] = place
    return list(by_id.values())[:MAX_SAVED_PLACES]


def fold_legacy(
    places: list[dict[str, Any]],
    *,
    legacy_favorites: Any,
    home_lat: Any,
    home_lng: Any,
    work_lat: Any,
    work_lng: Any,
) -> list[dict[str, Any]]:
    """Merge the three plaintext sources into an already-decrypted list.

    PRECEDENCE IS "WHAT IS ALREADY ENCRYPTED WINS", and it is the only
    defensible order. The encrypted blob is what the rider's app last wrote;
    the legacy columns are what some older build left behind. A rider who moved
    house, updated Home in the app, and still has the old coordinates sitting
    in `home_lat` must not have the move undone by a migration.

    So this ADDS what is missing and never overwrites. Run repeatedly it is a
    no-op, which matters because it runs on every profile read until the
    plaintext columns are dropped.

    The home and work pairs become the frontend's reserved slot rows, because
    that is what they are — the two fixed saved places the app has always had —
    and landing them anywhere else would show a rider their house twice.
    """
    by_id = {p["id"]: p for p in places}

    for entry in clean_places(legacy_favorites):
        by_id.setdefault(entry["id"], entry)

    for place_id, label, emoji, lat, lng in (
        (SLOT_HOME_ID, "Home", "🏠", home_lat, home_lng),
        (SLOT_WORK_ID, "Work", "💼", work_lat, work_lng),
    ):
        if place_id in by_id:
            continue
        pair = clean_place({"id": place_id, "label": label, "emoji": emoji,
                            "lat": lat, "lon": lng})
        if pair is not None:
            by_id[place_id] = pair

    return list(by_id.values())[:MAX_SAVED_PLACES]


def legacy_present(
    *,
    legacy_favorites: Any,
    home_lat: Any,
    home_lng: Any,
    work_lat: Any,
    work_lng: Any,
) -> bool:
    """Is there anything in the plaintext columns worth migrating?

    Asked so a read can skip the write when there is nothing to do — which is
    every read after the first, and every read for the great majority of riders
    who never had a legacy row at all. Without it, every profile GET would
    become a GET plus an UPDATE.
    """
    if clean_places(legacy_favorites):
        return True
    return any(
        clean_place({"id": "x", "label": "x", "lat": lat, "lon": lng}) is not None
        for lat, lng in ((home_lat, home_lng), (work_lat, work_lng))
    )
