"""Tests for src/identity.py's cosmetic plate_display_code. hash_plate
(the actual privacy-relevant HMAC) is already covered by the salt/identity
tests in tests/test_envelope.py."""

from __future__ import annotations

from src.identity import plate_display_code


def test_matches_the_worked_example():
    assert plate_display_code("1231234") == "ZTRZTRF"


def test_maps_every_digit():
    assert plate_display_code("0123456789") == "WZTRFVASHN"


def test_non_digit_characters_pass_through_unchanged():
    assert plate_display_code("AB-12") == "AB-ZT"


def test_empty_string():
    assert plate_display_code("") == ""
