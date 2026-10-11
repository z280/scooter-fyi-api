#!/usr/bin/env python3
"""Generate the `ruling_colors` palette — v2, seeded by sql/107.

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
against the basemap. Picking hex values by eye gives neither. OKLCH is
perceptually uniform: a fixed step in lightness or hue looks like the same
size step everywhere on the wheel, which sRGB emphatically does not (the
classic failure is a sweep through yellow, where naive HSL produces a band
of near-identical bright colours and a muddy blue range at the same
nominal lightness).

So: hue families x lightness steps, evenly spaced in OKLCH, then
converted, then filtered (see CONFLICTS).

WHAT CHANGED IN V2 (and why v1 read as a bad palette)
-----------------------------------------------------
v1 was 16 families x 8 steps at L 0.32..0.86, chroma 0.075..0.18. Three
things went wrong with it on an actual map:

1. THE BOTTOM THREE STEPS WERE MUD. At L 0.40 and below, a gamut-fitted
   chroma of ~0.11 is nearly neutral: red-800 (#5f1113), yellow-800
   (#3b3300) and lime-800 (#293900) are all "dark brownish", and at the
   55% fill opacity every territory renders at they collapse into the
   same grey-brown smear over the basemap. 48 of the 128 entries were
   effectively three colours.
2. THE CHROMA CEILING WAS TOO LOW. 0.18 at L 0.55 is a muted mid-tone.
   The palette's job is identity, not restraint — a rider's territory
   should be recognisable at a glance from across the map, and a wall of
   dusty mid-tones is exactly what "the palette sucks" describes.
3. SIXTEEN FAMILIES OVERSOLD THE DIFFERENCE. red (25 deg) and crimson
   (5 deg) are 20 degrees apart; so are cyan/teal (210/190). At the same
   L and a fitted chroma they are a hair apart, so the picker showed two
   columns that looked like one, which makes the whole grid feel like
   filler.

v2 is 14 families x 6 steps = 84 candidates, L 0.46..0.85, chroma
requested up to 0.23 and gamut-fitted down. Every family is >= 24 degrees
from its neighbours, the deepest step still carries real hue, and the
lightest is a usable pastel rather than a near-white. Fewer, better,
and every entry survives the conflict filter below.

CONFLICTS WITH THE REST OF THE MAP
----------------------------------
A territory fill is not alone on the map: the city's micromobility zones
(red/orange/yellow), the equity areas (purple), the Rover zone (teal),
the ride route and trail (blue/green/orange) and the neutral "held but
uncoloured" grey all draw over or beside it. A territory that renders in
the no-ride zone's red is actively misleading — a rider reads it as a
restriction, not as somebody's claim.

So RESERVED_COLORS lists what the frontend paints for those features,
and every candidate within CONFLICT_DISTANCE of one of them in OKLab is
dropped. OKLab distance (not hex proximity) because the question is
whether the EYE confuses them. The threshold is deliberately modest: it
removes the handful of entries that genuinely read as a map feature,
without gutting whole families — losing all reds because no-ride zones
are red would be a worse palette, not a safer one.

GAMUT
-----
Not every (L, C, H) exists in sRGB — the gamut is a lumpy solid, widest
around yellow and narrowest around blue. Requesting a fixed chroma at
every hue would silently clip, collapsing distinct requests onto the same
rendered colour. Instead each colour keeps its L and H and gives up
CHROMA until it fits (`_fit_chroma`), which is the standard trade: hue and
lightness are what the eye uses to tell these apart, saturation is what it
forgives.
"""

from __future__ import annotations

import math

# 14 hue families, named for what they look like, at their OKLCH hue
# angle. No two are closer than 24 degrees — see point 3 in the header.
# Angles are not evenly spaced: even spacing in OKLCH hue puts an
# unhelpful number of entries in the green-to-teal arc (where the eye
# discriminates poorly) and too few through the oranges (where it
# discriminates well). These are nudged for even PERCEIVED coverage.
HUE_FAMILIES: list[tuple[str, float]] = [
    ("red", 27.0),
    ("rose", 1.0),
    ("magenta", 337.0),
    ("purple", 313.0),
    ("violet", 289.0),
    ("indigo", 265.0),
    ("blue", 245.0),
    ("cyan", 215.0),
    ("teal", 191.0),
    ("emerald", 165.0),
    ("green", 141.0),
    ("lime", 117.0),
    ("amber", 85.0),
    ("orange", 55.0),
]

# 6 steps per family. Step number -> (lightness, chroma requested before
# gamut fitting). Chroma peaks just below the middle, where sRGB has the
# most of it to give; the two lightest steps are pastels by request, not
# by gamut accident.
STEPS: list[tuple[int, float, float]] = [
    (300, 0.46, 0.185),
    (400, 0.56, 0.225),
    (500, 0.64, 0.230),
    (600, 0.72, 0.195),
    (700, 0.79, 0.155),
    (800, 0.85, 0.115),
]

# What the rest of the map already paints, and must not be confused with.
# Kept in sync by hand with the frontend constants named alongside each —
# there is no shared source for them across the two repositories, so a
# frontend colour change means re-running this script and shipping the
# resulting palette as a new migration.
RESERVED_COLORS: list[tuple[str, str]] = [
    ("#c1121f", "no-ride zone (micromobility-zones.ts)"),
    ("#e07a00", "no-parking / slow-no-parking zone (micromobility-zones.ts)"),
    ("#eab308", "slow zone (micromobility-zones.ts)"),
    ("#8a8f98", "school / outside-Denver zone, and the neutral uncoloured "
                "territory (micromobility-zones.ts, leaderboard.ts)"),
    ("#6a1b9a", "equity area (equity-areas.ts)"),
    ("#00897b", "Rover zone (rover-zone.ts)"),
    ("#0066ff", "ride trail (ride-trail.ts)"),
    ("#238636", "trail start point (ride-trail.ts)"),
    ("#d55e00", "route destination (ride-route-line.ts)"),
    ("#2171b5", "hex-metric ramp outline (hexdensity.ts)"),
]

# OKLab Euclidean distance below which a candidate reads as one of the
# reserved colours. 0.115 is calibrated, not picked: at 0.10 the no-ride
# red still swallows a red-400 that nobody would mistake for a zone, and
# at 0.15 it takes most of two families with it. See the CONFLICTS note.
CONFLICT_DISTANCE = 0.055


def _oklch_to_oklab(L: float, C: float, H_deg: float) -> tuple[float, float, float]:
    h = math.radians(H_deg)
    return (L, C * math.cos(h), C * math.sin(h))


def _oklab_to_linear_srgb(L: float, a: float, b: float) -> tuple[float, float, float]:
    """OKLab -> linear sRGB. Coefficients are Björn Ottosson's OKLab."""
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b

    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3

    return (
        +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def _oklch_to_linear_srgb(L: float, C: float, H_deg: float) -> tuple[float, float, float]:
    return _oklab_to_linear_srgb(*_oklch_to_oklab(L, C, H_deg))


def _linear_srgb_to_oklab(r: float, g: float, b: float) -> tuple[float, float, float]:
    """The inverse of _oklab_to_linear_srgb, for measuring distances to the
    reserved colours (which arrive as hex, not as OKLCH requests)."""
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b

    l_, m_, s_ = _cbrt(l), _cbrt(m), _cbrt(s)

    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def _cbrt(x: float) -> float:
    return math.copysign(abs(x) ** (1 / 3), x)


def _in_gamut(rgb: tuple[float, float, float], *, eps: float = 1e-6) -> bool:
    return all(-eps <= c <= 1 + eps for c in rgb)


def _encode(c: float) -> int:
    """Linear -> sRGB 0..255 with the standard transfer function."""
    c = min(1.0, max(0.0, c))
    srgb = 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055
    return round(srgb * 255)


def _decode(v: int) -> float:
    """sRGB 0..255 -> linear, the inverse of _encode."""
    c = v / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def hex_to_oklab(hex_value: str) -> tuple[float, float, float]:
    h = hex_value.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return _linear_srgb_to_oklab(_decode(r), _decode(g), _decode(b))


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


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.dist(a, b)


RESERVED_OKLAB = [(hex_to_oklab(h), h, why) for h, why in RESERVED_COLORS]


def nearest_reserved(hex_value: str) -> tuple[float, str, str]:
    """(distance, reserved hex, what paints it) for the closest map feature."""
    lab = hex_to_oklab(hex_value)
    return min(
        ((_distance(lab, r_lab), r_hex, why) for r_lab, r_hex, why in RESERVED_OKLAB),
        key=lambda t: t[0],
    )


def build() -> tuple[list[tuple[str, str, str, int, int]], list[tuple[str, str, float, str]]]:
    """((hex, name, hue_family, lightness_step, sort_order) rows, dropped rows)."""
    rows: list[tuple[str, str, str, int, int]] = []
    dropped: list[tuple[str, str, float, str]] = []
    order = 0
    for family, hue in HUE_FAMILIES:
        for step, L, C in STEPS:
            fitted = _fit_chroma(L, C, hue)
            r, g, b = _oklch_to_linear_srgb(L, fitted, hue)
            hex_value = f"#{_encode(r):02x}{_encode(g):02x}{_encode(b):02x}"
            name = f"{family}-{step}"
            distance, reserved_hex, why = nearest_reserved(hex_value)
            if distance < CONFLICT_DISTANCE:
                dropped.append((hex_value, name, distance, f"{reserved_hex} {why}"))
                continue
            rows.append((hex_value, name, family, step, order))
            order += 1
    return rows, dropped


def main() -> None:
    rows, dropped = build()

    # The palette is a PRIMARY KEY in the migration, so a duplicate would
    # turn into an ON CONFLICT no-op and silently ship a short palette.
    # Fail here, where it is fixable by moving a hue or a lightness step.
    seen: dict[str, str] = {}
    for hex_value, name, _family, _step, _order in rows:
        if hex_value in seen:
            raise SystemExit(
                f"duplicate colour {hex_value}: {seen[hex_value]} and {name} — "
                "adjust HUE_FAMILIES or STEPS so every entry is distinct"
            )
        seen[hex_value] = name

    # Every family must keep at least two steps. The auto-assigner
    # (sql/107's assign_ruling_colors) pairs a fill with a darker border
    # and prefers one from the same family, and a one-entry family can
    # offer neither a fill with a darker sibling nor that sibling.
    per_family: dict[str, int] = {}
    for _hex, _name, family, _step, _order in rows:
        per_family[family] = per_family.get(family, 0) + 1
    thin = sorted(f for f, n in per_family.items() if n < 2)
    if thin:
        raise SystemExit(
            f"families left with fewer than 2 colours after the conflict "
            f"filter: {', '.join(thin)} — widen STEPS or move the hue"
        )

    print(f"-- {len(rows)} colours: {len(HUE_FAMILIES)} hue families x "
          f"{len(STEPS)} steps, minus {len(dropped)} that read as a map feature.")
    print("-- Generated by scripts/gen_ruling_palette.py — see that file for the method.")
    for hex_value, name, distance, why in dropped:
        print(f"-- dropped {name} ({hex_value}): {distance:.3f} from {why}")
    print("INSERT INTO ruling_colors")
    print("    (hex, name, hue_family, lightness_step, sort_order, selectable) VALUES")
    lines = [
        f"    ('{hex_value}', '{name}', '{family}', {step}, {order}, TRUE)"
        for hex_value, name, family, step, order in rows
    ]
    print(",\n".join(lines))
    print("ON CONFLICT (hex) DO UPDATE SET")
    print("    name = EXCLUDED.name,")
    print("    hue_family = EXCLUDED.hue_family,")
    print("    lightness_step = EXCLUDED.lightness_step,")
    print("    sort_order = EXCLUDED.sort_order,")
    print("    selectable = TRUE;")


if __name__ == "__main__":
    main()
