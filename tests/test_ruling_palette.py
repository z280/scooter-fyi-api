"""The palette generator (scripts/gen_ruling_palette.py).

The generated colours live in sql/044 as literals, so this does NOT test
what shipped — tests/test_profile_identity_pg.py does that, against the
seeded table. What this covers is the generator staying correct, so that
regenerating (to extend the palette, say) can't quietly produce colours
outside sRGB or duplicates that ON CONFLICT would swallow.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "gen_ruling_palette",
    Path(__file__).resolve().parents[1] / "scripts" / "gen_ruling_palette.py",
)
gen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen)


def test_builds_at_least_128_distinct_colours():
    rows = gen.build()
    assert len(rows) >= 128
    assert len({r[0] for r in rows}) == len(rows), "duplicate hex"
    assert len({r[1] for r in rows}) == len(rows), "duplicate name"


def test_every_colour_is_lowercase_six_digit_hex():
    for hex_value, _name, _family, _order in gen.build():
        assert len(hex_value) == 7 and hex_value[0] == "#"
        assert hex_value[1:] == hex_value[1:].lower()
        int(hex_value[1:], 16)  # raises if not hex


def test_sort_order_is_dense_and_unique():
    """sort_order drives picker layout; a gap or a repeat would render the
    palette in a jumbled order for no visible reason."""
    orders = sorted(r[3] for r in gen.build())
    assert orders == list(range(len(orders)))


def test_fitted_chroma_always_lands_inside_srgb():
    """_fit_chroma is the whole reason the palette has no clipped colours:
    out-of-gamut requests would clamp on conversion, collapsing distinct
    (L, C, H) inputs onto identical hex output."""
    for _step, L, C in gen.STEPS:
        for _family, hue in gen.HUE_FAMILIES:
            fitted = gen._fit_chroma(L, C, hue)
            assert fitted <= C
            assert gen._in_gamut(gen._oklch_to_linear_srgb(L, fitted, hue)), (
                f"L={L} C={fitted} H={hue} is outside sRGB after fitting"
            )


def test_chroma_is_only_reduced_when_the_gamut_demands_it():
    """Hue and lightness are what the eye uses to tell these apart, so
    they are never traded away — only saturation is. A request already in
    gamut must come back untouched."""
    assert gen._fit_chroma(0.55, 0.02, 25.0) == 0.02


@pytest.mark.parametrize("L", [L for _step, L, _C in gen.STEPS])
def test_lightness_steps_stay_inside_the_usable_band(L):
    """Above ~0.9 a fill washes out under alpha; below ~0.25 colours stop
    being distinguishable from each other and from map ink."""
    assert 0.25 <= L <= 0.90


def test_grey_input_produces_a_neutral_colour():
    """Sanity check on the OKLab coefficients themselves: zero chroma must
    give equal R, G and B. A transposed matrix row would still produce
    plausible-looking colours but fail this."""
    r, g, b = gen._oklch_to_linear_srgb(0.6, 0.0, 0.0)
    assert r == pytest.approx(g, abs=1e-6)
    assert g == pytest.approx(b, abs=1e-6)


def test_encode_matches_the_srgb_transfer_function():
    assert gen._encode(0.0) == 0
    assert gen._encode(1.0) == 255
    # Linear 0.5 is ~0.735 encoded — NOT 128. Getting this wrong is the
    # classic gamma bug and would make the whole palette too dark.
    assert gen._encode(0.5) == 188
