"""Saved places: the crypto box, the validator, and the legacy fold-in.

The assertions that matter most are the refusals. This module exists so that a
database dump stops containing people's home addresses, and the two ways that
fails quietly are a missing key degrading to plaintext and a migration
overwriting a rider's current address with a stale one.
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from src import place_crypto
from src.saved_places import (
    SLOT_HOME_ID,
    SLOT_WORK_ID,
    MAX_SAVED_PLACES,
    clean_place,
    clean_places,
    fold_legacy,
    legacy_present,
)


@pytest.fixture()
def key(monkeypatch):
    k = Fernet.generate_key().decode()
    monkeypatch.setenv("VEO_PLACES_KEY", k)
    monkeypatch.delenv("VEO_PLACES_KEY_OLD", raising=False)
    return k


def place(**over):
    base = {"id": "slot:home", "label": "Home", "emoji": "🏠",
            "lat": 39.7285, "lon": -105.0345}
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# place_crypto
# ---------------------------------------------------------------------------


def test_roundtrip(key):
    places = [place(), place(id="gym", label="Gym", emoji="🏋️")]
    blob = place_crypto.seal(places)
    assert place_crypto.unseal(blob) == places


def test_ciphertext_does_not_contain_the_coordinates(key):
    """The whole point, asserted rather than assumed.

    A column that still had "39.7285" in it would pass a round-trip test and
    fail at the only job it has.
    """
    blob = place_crypto.seal([place()])
    assert "39.7285" not in blob
    assert "Home" not in blob
    assert "105" not in blob


def test_a_missing_key_fails_the_write_rather_than_storing_plaintext(monkeypatch):
    # The silent degradation this module exists to prevent. It would look
    # exactly like success.
    monkeypatch.delenv("VEO_PLACES_KEY", raising=False)
    assert place_crypto.configured() is False
    with pytest.raises(RuntimeError) as exc:
        place_crypto.seal([place()])
    assert "VEO_PLACES_KEY" in str(exc.value)


def test_every_read_failure_is_none_and_never_an_exception(monkeypatch, key):
    # A profile GET that 500s over one unreadable field takes the rider's
    # email, phone and rate plan down with it — and those are the fields they
    # need to fix whatever is wrong.
    assert place_crypto.unseal(None) is None
    assert place_crypto.unseal("") is None
    assert place_crypto.unseal("not-a-fernet-token") is None
    # A token from a different key.
    other = Fernet(Fernet.generate_key()).encrypt(b"[]").decode()
    assert place_crypto.unseal(other) is None
    # And with no key at all, a read degrades rather than raising.
    monkeypatch.delenv("VEO_PLACES_KEY", raising=False)
    assert place_crypto.unseal("anything") is None


def test_rotation_reads_the_old_key_and_writes_the_new_one(monkeypatch):
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv("VEO_PLACES_KEY", old_key)
    written_with_old = place_crypto.seal([place()])

    # Rotate: new key primary, old key kept for reading.
    monkeypatch.setenv("VEO_PLACES_KEY", new_key)
    monkeypatch.setenv("VEO_PLACES_KEY_OLD", old_key)
    assert place_crypto.unseal(written_with_old) == [place()]

    # New writes use the NEW key only — so dropping the old one later is safe
    # once everything has been re-saved.
    fresh = place_crypto.seal([place()])
    monkeypatch.delenv("VEO_PLACES_KEY_OLD")
    assert place_crypto.unseal(fresh) == [place()]
    assert place_crypto.unseal(written_with_old) is None


def test_the_cipher_is_not_frozen_at_import(monkeypatch):
    """A module-level cipher would freeze whichever key was set first.

    Which is a bug that only ever shows up as "the wrong key in production".
    """
    first = Fernet.generate_key().decode()
    monkeypatch.setenv("VEO_PLACES_KEY", first)
    blob = place_crypto.seal([place()])
    second = Fernet.generate_key().decode()
    monkeypatch.setenv("VEO_PLACES_KEY", second)
    assert place_crypto.unseal(blob) is None


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_accepts_an_ordinary_place():
    assert clean_place(place())["lat"] == 39.7285


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"id": "", "label": "Home", "lat": 1, "lon": 1},
        {"id": "x", "label": "   ", "lat": 1, "lon": 1},
        {"id": "x", "label": "Home", "lat": 412, "lon": 1},
        {"id": "x", "label": "Home", "lat": 1, "lon": -999},
        {"id": "x", "label": "Home", "lat": "39.7", "lon": 1},
        {"id": "x", "label": "Home", "lat": None, "lon": 1},
        {"id": 7, "label": "Home", "lat": 1, "lon": 1},
        "not a dict",
        None,
    ],
)
def test_refuses_what_is_not_a_place(bad):
    assert clean_place(bad) is None


def test_a_boolean_is_not_a_latitude():
    # `bool` is an `int` in Python, and True as a latitude is the kind of thing
    # that gets stored once and found years later.
    assert clean_place({"id": "x", "label": "Home", "lat": True, "lon": 1}) is None


def test_a_list_drops_bad_entries_rather_than_refusing_everything():
    # This also runs over data we wrote ourselves and over the legacy columns,
    # where one unparseable row must not cost a rider every other place.
    out = clean_places([place(), "junk", {"id": "y"}, place(id="gym", label="Gym")])
    assert [p["id"] for p in out] == ["slot:home", "gym"]


def test_dedupes_on_id_with_the_last_write_winning():
    out = clean_places([place(label="Old"), place(label="New")])
    assert len(out) == 1
    assert out[0]["label"] == "New"


def test_caps_the_list():
    out = clean_places([place(id=f"p{i}", label=f"P{i}") for i in range(MAX_SAVED_PLACES + 10)])
    assert len(out) == MAX_SAVED_PLACES


def test_clean_places_of_nonsense_is_empty():
    for bad in (None, "x", 7, {"id": "x"}):
        assert clean_places(bad) == []


# ---------------------------------------------------------------------------
# the legacy fold-in
# ---------------------------------------------------------------------------


LEGACY = dict(legacy_favorites=[], home_lat=None, home_lng=None,
               work_lat=None, work_lng=None)


def test_folds_the_home_and_work_pairs_into_the_reserved_slots():
    out = fold_legacy([], **{**LEGACY, "home_lat": 39.7, "home_lng": -105.0,
                             "work_lat": 39.75, "work_lng": -104.99})
    by_id = {p["id"]: p for p in out}
    assert by_id[SLOT_HOME_ID]["lat"] == 39.7
    assert by_id[SLOT_WORK_ID]["lon"] == -104.99
    assert by_id[SLOT_HOME_ID]["label"] == "Home"


def test_WHAT_IS_ALREADY_ENCRYPTED_WINS():
    """The precedence rule, and the one that would hurt somebody.

    A rider who moved house, updated Home in the app, and still has the old
    coordinates sitting in `home_lat` must not have the move undone by a
    migration.
    """
    current = [place(lat=40.0, lon=-106.0)]
    out = fold_legacy(current, **{**LEGACY, "home_lat": 39.7, "home_lng": -105.0})
    assert out == current


def test_is_a_no_op_run_twice():
    # It runs on every profile read until the plaintext columns are dropped.
    once = fold_legacy([], **{**LEGACY, "home_lat": 39.7, "home_lng": -105.0})
    twice = fold_legacy(once, **{**LEGACY, "home_lat": 39.7, "home_lng": -105.0})
    assert twice == once


def test_folds_the_legacy_favorites_column():
    out = fold_legacy([], **{**LEGACY, "legacy_favorites": [place(id="gym", label="Gym")]})
    assert [p["id"] for p in out] == ["gym"]


def test_half_a_coordinate_pair_is_not_a_place():
    out = fold_legacy([], **{**LEGACY, "home_lat": 39.7, "home_lng": None})
    assert out == []


def test_legacy_present_is_what_stops_a_read_becoming_a_write():
    # Without it, every profile GET would become a GET plus an UPDATE, forever.
    assert legacy_present(**LEGACY) is False
    assert legacy_present(**{**LEGACY, "home_lat": 39.7, "home_lng": -105.0}) is True
    assert legacy_present(**{**LEGACY, "legacy_favorites": [place()]}) is True
    # Garbage in the legacy column is not something to migrate.
    assert legacy_present(**{**LEGACY, "legacy_favorites": ["junk"]}) is False
    assert legacy_present(**{**LEGACY, "home_lat": 39.7}) is False


def test_the_whole_path_end_to_end(key):
    folded = fold_legacy(
        clean_places(place_crypto.unseal(None)),
        legacy_favorites=[place(id="gym", label="Gym", emoji="🏋️")],
        home_lat=39.7285, home_lng=-105.0345, work_lat=None, work_lng=None,
    )
    blob = place_crypto.seal(folded)
    assert "39.7285" not in blob
    back = {p["id"]: p for p in place_crypto.unseal(blob)}
    assert set(back) == {"gym", SLOT_HOME_ID}
    assert back[SLOT_HOME_ID]["lat"] == 39.7285
