"""The palette generator (scripts/gen_ruling_palette.py).

The generated colours live in sql/107 as literals (sql/044 before it, for
the v1 palette that migration retires), so this does NOT test what
shipped — tests/test_profile_identity_pg.py does that, against the seeded
table. What this covers is the generator staying correct, so that
regenerating (to extend the palette, say) can't quietly produce colours
outside sRGB, duplicates that ON CONFLICT would swallow, or a colour the
map already uses for something that means something.
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


def test_builds_enough_distinct_colours():
    """Smaller than v1's 128 on purpose (see sql/107) — but still far more
    (fill, border) pairs than there are riders, and every entry distinct."""
    rows, _dropped = gen.build()
    assert len(rows) >= 64
    assert len({r[0] for r in rows}) == len(rows), "duplicate hex"
    assert len({r[1] for r in rows}) == len(rows), "duplicate name"


def test_every_colour_is_lowercase_six_digit_hex():
    rows, _dropped = gen.build()
    for hex_value, _name, _family, _step, _order in rows:
        assert len(hex_value) == 7 and hex_value[0] == "#"
        assert hex_value[1:] == hex_value[1:].lower()
        int(hex_value[1:], 16)  # raises if not hex


def test_sort_order_is_dense_and_unique():
    """sort_order drives picker layout; a gap or a repeat would render the
    palette in a jumbled order for no visible reason. Dropped candidates
    must not leave holes — the counter advances per KEPT row."""
    rows, _dropped = gen.build()
    orders = sorted(r[4] for r in rows)
    assert orders == list(range(len(orders)))


def test_lightness_step_matches_the_name():
    rows, _dropped = gen.build()
    for _hex, name, _family, step, _order in rows:
        assert name.endswith(f"-{step}")


# ---------- the conflict filter --------------------------------------------

def test_no_colour_reads_as_a_map_feature():
    """The point of the filter: nothing in the shipped palette sits within
    CONFLICT_DISTANCE of a colour the map already uses to MEAN something
    (a no-ride zone, an equity area, the ride trail)."""
    rows, _dropped = gen.build()
    for hex_value, name, _family, _step, _order in rows:
        distance, reserved, why = gen.nearest_reserved(hex_value)
        assert distance >= gen.CONFLICT_DISTANCE, (
            f"{name} ({hex_value}) is {distance:.3f} from {reserved} — {why}"
        )


def test_the_filter_drops_something_but_not_a_whole_family():
    """A threshold that drops nothing is not filtering; one that empties a
    family has stopped being a filter and started being a redesign. Each
    family must keep at least the two entries the assigner's fill/border
    rule needs."""
    rows, dropped = gen.build()
    assert dropped, "no candidate conflicts — has the threshold gone to zero?"
    kept: dict[str, int] = {}
    for _hex, _name, family, _step, _order in rows:
        kept[family] = kept.get(family, 0) + 1
    assert set(kept) == {f for f, _hue in gen.HUE_FAMILIES}
    assert min(kept.values()) >= 2


def test_reserved_colours_are_their_own_nearest_match():
    """Guards the OKLab round trip the filter measures in: feeding a
    reserved colour back in must come out at distance ~0 from itself, or
    every distance in the filter is meaningless."""
    for hex_value, _why in gen.RESERVED_COLORS:
        distance, reserved, _ = gen.nearest_reserved(hex_value)
        assert reserved == hex_value
        assert distance == pytest.approx(0.0, abs=1e-9)


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
