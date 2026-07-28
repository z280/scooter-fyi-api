#!/usr/bin/env python3
"""Generate the 128-colour `ruling_colors` palette seeded by sql/044.

Run once; paste the printed VALUES block into the migration. This script
is PROVENANCE, not a runtime dependency — the palette lives in SQL as
literals, exactly like sfw_adjectives/emoji_nouns (sql/025), so seed data
has one home and a colour never silently changes under a rider who
already claimed it.

    python scripts/gen_ruling_palette.py

WHY OKLCH AND NOT HAND-PICKED HEX
---------------------------------
These colours fill map hexagons that sit next to each other, so the thing
that matters is that any two are TELLABLE APART, and that none disappears
against the basemap. Picking 128 hex values by eye gives neither. OKLCH is
perceptually uniform: a fixed step in lightness or hue looks like the same
size step everywhere on the wheel, which sRGB emphatically does not (the
classic failure is a sweep through yellow, where naive HSL produces a band
of near-identical bright colours and a muddy blue range at the same
nominal lightness).

So: 16 hues x 8 lightness steps, evenly spaced in OKLCH, then converted.

GAMUT
-----
Not every (L, C, H) exists in sRGB — the gamut is a lumpy solid, widest
around yellow and narrowest around blue. Requesting a fixed chroma at
every hue would silently clip, collapsing distinct requests onto the same
rendered colour. Instead each colour keeps its L and H and gives up
CHROMA until it fits (`_fit_chroma`), which is the standard trade: hue and
lightness are what the eye uses to tell these apart, saturation is what it
forgives.

BOUNDS
------
Lightness runs 0.32..0.86. Above that a fill at 60% alpha washes out
against a light basemap; below it, dark colours stop being distinguishable
from each other and from map ink. Neither end is a taste call — they are
the range where a 2px border and a translucent fill both still read.
"""

from __future__ import annotations

import math

# 16 hue families, named for what they look like, at their OKLCH hue angle.
# Angles are not evenly spaced: even spacing in OKLCH hue puts an
# unhelpful number of entries in the green-to-teal arc (where the eye
# discriminates poorly) and too few through the oranges (where it
# discriminates well). These are nudged for even PERCEIVED coverage.
HUE_FAMILIES: list[tuple[str, float]] = [
    ("red", 25.0),
    ("crimson", 5.0),
    ("magenta", 340.0),
    ("purple", 315.0),
    ("violet", 295.0),
    ("indigo", 275.0),
    ("blue", 255.0),
    ("sky", 235.0),
    ("cyan", 210.0),
    ("teal", 190.0),
    ("emerald", 165.0),
    ("green", 145.0),
    ("lime", 125.0),
    ("yellow", 100.0),
    ("amber", 75.0),
    ("orange", 50.0),
]

# 8 steps per family. Step number -> (lightness, chroma requested before
# gamut fitting). Chroma peaks in the middle: very light and very dark
# colours cannot hold much of it in sRGB anyway.
STEPS: list[tuple[int, float, float]] = [
    (100, 0.86, 0.075),
    (200, 0.78, 0.110),
    (300, 0.70, 0.145),
    (400, 0.62, 0.170),
    (500, 0.55, 0.180),
    (600, 0.48, 0.165),
    (700, 0.40, 0.140),
    (800, 0.32, 0.110),
]


def _oklch_to_linear_srgb(L: float, C: float, H_deg: float) -> tuple[float, float, float]:
    """OKLCH -> linear sRGB. Coefficients are Björn Ottosson's OKLab."""
    h = math.radians(H_deg)
    a = C * math.cos(h)
    b = C * math.sin(h)

    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b

    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3

    return (
        +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def _in_gamut(rgb: tuple[float, float, float], *, eps: float = 1e-6) -> bool:
    return all(-eps <= c <= 1 + eps for c in rgb)


def _encode(c: float) -> int:
    """Linear -> sRGB 0..255 with the standard transfer function."""
    c = min(1.0, max(0.0, c))
    srgb = 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055
    return round(srgb * 255)


def _fit_chroma(L: float, C: float, H: float) -> float:
    """The largest chroma <= C that lands inside sRGB at this L and H.

    Bisection rather than an analytic solve: the sRGB gamut boundary in
    OKLCH has no closed form, and 24 halvings gets well inside a single
    8-bit step, which is the only precision that survives to a hex string.
    """
    if _in_gamut(_oklch_to_linear_srgb(L, C, H)):
        return C
    lo, hi = 0.0, C
    for _ in range(24):
        mid = (lo + hi) / 2
        if _in_gamut(_oklch_to_linear_srgb(L, mid, H)):
            lo = mid
        else:
            hi = mid
    return lo


def build() -> list[tuple[str, str, str, int]]:
    """(hex, name, hue_family, sort_order), 128 rows."""
    rows: list[tuple[str, str, str, int]] = []
    order = 0
    for family, hue in HUE_FAMILIES:
        for step, L, C in STEPS:
            fitted = _fit_chroma(L, C, hue)
            r, g, b = _oklch_to_linear_srgb(L, fitted, hue)
            hex_value = f"#{_encode(r):02x}{_encode(g):02x}{_encode(b):02x}"
            rows.append((hex_value, f"{family}-{step}", family, order))
            order += 1
    return rows


def main() -> None:
    rows = build()

    # The palette is a PRIMARY KEY in the migration, so a duplicate would
    # turn into an ON CONFLICT no-op and silently ship a short palette.
    # Fail here, where it is fixable by moving a hue or a lightness step.
    seen: dict[str, str] = {}
    for hex_value, name, _family, _order in rows:
        if hex_value in seen:
            raise SystemExit(
                f"duplicate colour {hex_value}: {seen[hex_value]} and {name} — "
                "adjust HUE_FAMILIES or STEPS so every entry is distinct"
            )
        seen[hex_value] = name
    if len(rows) < 128:
        raise SystemExit(f"palette has {len(rows)} entries, need at least 128")

    print(f"-- {len(rows)} colours: {len(HUE_FAMILIES)} hue families x {len(STEPS)} steps.")
    print("-- Generated by scripts/gen_ruling_palette.py — see that file for the method.")
    print("INSERT INTO ruling_colors (hex, name, hue_family, sort_order) VALUES")
    lines = [
        f"    ('{hex_value}', '{name}', '{family}', {order})"
        for hex_value, name, family, order in rows
    ]
    print(",\n".join(lines))
    print("ON CONFLICT (hex) DO NOTHING;")


if __name__ == "__main__":
    main()
