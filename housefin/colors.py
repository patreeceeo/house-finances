"""Person colors: distinct '#rrggbb' backgrounds that keep text readable.

Colors are built in OKLCH with fixed lightness and chroma, so every person's
color looks equally bright and only the hue differs. Hues are spaced by the
golden angle (137.5 degrees), which keeps any run of consecutive hues well
spread around the wheel however many people there are.

Lightness is high enough that dark text reads on every color
(:data:`LIGHTNESS`); the app shows these as cell backgrounds in both light and
dark themes, so use :func:`text_color_for` to pick near-black or white text
for a given background rather than assuming one.

Choice for "one more color" (:func:`new_color`): rather than blindly
continuing the golden-angle sequence (which breaks down once people are
removed or colors were re-rolled), it picks the hue whose nearest existing
hue is farthest away (max-min circular distance, one candidate per degree,
ties going to the hue nearest a random start). With no existing colors it is
a random hue.

Pure module: no database access, standard library only.
"""

from __future__ import annotations

import math
import random

GOLDEN_ANGLE = 137.5
LIGHTNESS = 0.80  # OKLCH L; dark text has > 4.5:1 contrast on every hue
CHROMA = 0.12  # reduced further only if a hue cannot show it in sRGB

_DARK_TEXT = "#111111"
_LIGHT_TEXT = "#ffffff"
_CANDIDATES = 360  # hues tried by new_color, one per degree


def _oklch_to_linear_srgb(lightness: float, chroma: float, hue: float):
    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )


def _in_gamut(rgb) -> bool:
    return all(-1e-6 <= c <= 1 + 1e-6 for c in rgb)


def _encode(channel: float) -> int:
    channel = min(1.0, max(0.0, channel))
    if channel <= 0.0031308:
        value = 12.92 * channel
    else:
        value = 1.055 * channel ** (1 / 2.4) - 0.055
    return round(value * 255)


def oklch_to_hex(lightness: float, chroma: float, hue: float) -> str:
    """Convert OKLCH to '#rrggbb'. Out-of-gamut colors lose chroma until they fit."""
    rgb = _oklch_to_linear_srgb(lightness, chroma, hue)
    if not _in_gamut(rgb):
        lo, hi = 0.0, chroma
        for _ in range(30):
            mid = (lo + hi) / 2
            if _in_gamut(_oklch_to_linear_srgb(lightness, mid, hue)):
                lo = mid
            else:
                hi = mid
        rgb = _oklch_to_linear_srgb(lightness, lo, hue)
    return "#" + "".join(f"{_encode(c):02x}" for c in rgb)


def color_for_hue(hue: float) -> str:
    """The person color at ``hue`` degrees (fixed lightness and chroma)."""
    return oklch_to_hex(LIGHTNESS, CHROMA, hue % 360)


def _parse(color: str) -> tuple[float, float, float]:
    text = color.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"not a '#rrggbb' color: {color!r}")
    return tuple(int(text[i : i + 2], 16) / 255 for i in (0, 2, 4))


def _linearize(value: float) -> float:
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def hue_of(color: str) -> float:
    """OKLCH hue (0-360 degrees) of a '#rrggbb' color."""
    r, g, b = (_linearize(c) for c in _parse(color))
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    b2 = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return math.degrees(math.atan2(b2, a)) % 360


def luminance(color: str) -> float:
    """WCAG relative luminance of a '#rrggbb' color."""
    r, g, b = (_linearize(c) for c in _parse(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(color_a: str, color_b: str) -> float:
    """WCAG contrast ratio (1 to 21) between two '#rrggbb' colors."""
    hi, lo = sorted((luminance(color_a), luminance(color_b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def text_color_for(background: str) -> str:
    """Near-black or white, whichever has more contrast on ``background``."""
    if contrast_ratio(_DARK_TEXT, background) >= contrast_ratio(_LIGHT_TEXT, background):
        return _DARK_TEXT
    return _LIGHT_TEXT


def hue_distance(a: float, b: float) -> float:
    """Circular distance between two hues, 0 to 180 degrees."""
    diff = abs(a - b) % 360
    return min(diff, 360 - diff)


def generate_colors(count: int, rng: random.Random | None = None) -> list[str]:
    """``count`` colors with golden-angle hues from a random starting hue."""
    rng = rng or random
    start = rng.uniform(0, 360)
    return [color_for_hue(start + i * GOLDEN_ANGLE) for i in range(count)]


def new_color(existing: list[str], rng: random.Random | None = None) -> str:
    """One color far from ``existing``: the hue whose nearest existing hue is farthest."""
    rng = rng or random
    start = rng.uniform(0, 360)
    if not existing:
        return color_for_hue(start)
    taken = [hue_of(c) for c in existing]
    best_hue, best_gap = start, -1.0
    for i in range(_CANDIDATES):
        hue = (start + i) % 360
        gap = min(hue_distance(hue, t) for t in taken)
        if gap > best_gap + 1e-9:
            best_hue, best_gap = hue, gap
    return color_for_hue(best_hue)


def another_color(current: str, others: list[str], rng: random.Random | None = None) -> str:
    """'Pick another color' for one person: far from ``others``, not ``current``.

    Picks at random among hues nearly as far from everyone else as the best
    one, excluding the neighbourhood of the current color.
    """
    rng = rng or random
    taken = [hue_of(c) for c in others]
    cur = hue_of(current)

    def gap(hue: float) -> float:
        return min((hue_distance(hue, t) for t in taken), default=180.0)

    hues = [h for h in range(_CANDIDATES) if hue_distance(h, cur) >= 20]
    best = max(gap(h) for h in hues)
    good = [h for h in hues if gap(h) >= best * 0.8]
    return color_for_hue(rng.choice(good) + rng.random())
