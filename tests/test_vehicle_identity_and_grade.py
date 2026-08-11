"""Scooter identity and the Smart Ride Grade."""

from __future__ import annotations

import pytest

from src import vehicle_identity as vi
from src.quality import smart_ride_grade


# --- identity ----------------------------------------------------------------

def test_a_name_is_stable_for_the_life_of_the_vehicle(monkeypatch):
    """Derived, not assigned — so it survives restarts, redeploys and having
    no storage at all. A scooter a rider learned to recognise last week is
    the same scooter this week."""
    monkeypatch.setattr(vi, "_vocab", lambda: (("Lunar", "Solar", "Warp"), ("🐸", "🐧", "💿")))
    first = vi.public_name("a56a83688e01cd4e")
    assert first == vi.public_name("a56a83688e01cd4e")
    assert " " in first


def test_different_vehicles_get_different_names(monkeypatch):
    monkeypatch.setattr(vi, "_vocab", lambda: (tuple(f"W{i}" for i in range(60)),
                                               tuple(chr(0x1F600 + i) for i in range(40))))
    names = {vi.public_name(f"{i:016x}") for i in range(400)}
    assert len(names) > 300, "the vocabulary is not being spread across"


def test_word_and_emoji_are_drawn_independently(monkeypatch):
    """Slicing ONE hash twice correlates the two choices and collapses the
    effective vocabulary; a fresh digest with disjoint byte ranges does not."""
    monkeypatch.setattr(vi, "_vocab", lambda: (("A", "B"), ("1", "2", "3", "4")))
    seen = {vi.public_name(f"{i:016x}") for i in range(200)}
    assert len(seen) == 8, "not all word x emoji pairs are reachable"


def test_the_plate_suffix_is_absent_without_a_plate():
    """The public map payload has no plate, so display_name degrades to the
    name alone rather than inventing digits."""
    assert vi.plate_suffix(None) is None
    assert vi.display_name("a56a83688e01cd4e", None) == vi.public_name("a56a83688e01cd4e")


def test_the_plate_suffix_is_the_last_three_printed_on_the_scooter():
    assert vi.plate_suffix("1025899") == "899"
    assert vi.plate_suffix("928") == "928"
    assert vi.plate_suffix("7") == "7"


def test_a_vocabulary_outage_degrades_the_name_not_the_map(monkeypatch):
    """A missing table must never fail a 9,000-device payload for the sake of
    a decorative label."""
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(vi, "connection", boom)
    vi._vocab.cache_clear()
    try:
        assert vi.public_name("a56a83688e01cd4e") is not None
    finally:
        vi._vocab.cache_clear()


# --- the grade ---------------------------------------------------------------

def test_too_few_rentals_means_no_grade_not_a_flattering_one():
    """A perfect record over three rides is not evidence. None lets a client
    say "not enough rides yet"; a 100 would be a claim."""
    assert smart_ride_grade(0, 0) is None
    assert smart_ride_grade(4, 0) is None
    assert smart_ride_grade(5, 0) is not None


def test_a_thin_clean_record_sits_near_the_fleet_not_at_the_top():
    """Beta smoothing toward the measured fleet rate: five clean rentals
    should read like an ordinary scooter, not the best in the city."""
    thin = smart_ride_grade(5, 0)
    proven = smart_ride_grade(60, 0)
    assert thin < proven
    assert 80 <= thin <= 90
    assert proven >= 95


def test_the_grade_falls_as_failures_rise():
    grades = [smart_ride_grade(20, k) for k in (0, 1, 3, 6, 10)]
    assert grades == sorted(grades, reverse=True)


def test_the_scale_is_compressed_on_purpose():
    """Vehicle-attributable persistence is r=+0.275. That supports separating
    ~7% from ~11% failure; it does not support 1% vs 50%. Nothing may render
    below 65 and imply a precision the data cannot carry."""
    assert smart_ride_grade(50, 45) == 65
    # 100 has to be EARNED, not merely unblemished: the fleet prior only
    # releases with volume, so a spotless century of rentals still reads 97
    # and full marks need several hundred. A scooter that has been perfect so
    # far has still only been perfect so far.
    assert smart_ride_grade(100, 0) == 97
    assert smart_ride_grade(1000, 0) == 100
    for n, k in [(9, 9), (30, 25), (12, 8)]:
        assert 65 <= smart_ride_grade(n, k) <= 100


def test_no_go_count_cannot_exceed_the_rentals_it_came_from():
    """Counters are incremented independently; a corrupt pair must not produce
    a grade above the ceiling or below the floor."""
    assert 65 <= smart_ride_grade(10, 999) <= 100
    assert 65 <= smart_ride_grade(10, -5) <= 100
