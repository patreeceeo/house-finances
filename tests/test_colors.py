import random
import re

from housefin import colors

HEX = re.compile(r"^#[0-9a-f]{6}$")


def test_generate_hex_and_distinct_hues():
    cs = colors.generate_colors(8, random.Random(1))
    assert len(cs) == len(set(cs)) == 8
    assert all(HEX.match(c) for c in cs)
    hues = [colors.hue_of(c) for c in cs]
    for i, a in enumerate(hues):
        for b in hues[i + 1 :]:
            assert colors.hue_distance(a, b) > 15


def test_golden_angle_spacing():
    cs = colors.generate_colors(3, random.Random(2))
    h = [colors.hue_of(c) for c in cs]
    assert abs(colors.hue_distance(h[0], h[1]) - 137.5) < 3


def test_deterministic_with_rng():
    assert colors.generate_colors(4, random.Random(5)) == colors.generate_colors(4, random.Random(5))


def test_dark_text_readable_on_every_hue():
    for hue in range(0, 360, 3):
        bg = colors.color_for_hue(hue)
        assert HEX.match(bg)
        assert colors.contrast_ratio("#111111", bg) >= 4.5
        assert colors.contrast_ratio(colors.text_color_for(bg), bg) >= 4.5


def test_text_color_for_dark_and_light_backgrounds():
    assert colors.text_color_for("#101010") == "#ffffff"
    assert colors.text_color_for("#f0f0f0") == "#111111"


def test_new_color_far_from_existing():
    existing = colors.generate_colors(4, random.Random(3))
    c = colors.new_color(existing, random.Random(4))
    assert HEX.match(c) and c not in existing
    gap = min(colors.hue_distance(colors.hue_of(c), colors.hue_of(e)) for e in existing)
    assert gap > 25


def test_new_color_with_none_existing():
    assert HEX.match(colors.new_color([], random.Random(0)))


def test_another_color_differs_and_avoids_others():
    cs = colors.generate_colors(4, random.Random(7))
    current, others = cs[0], cs[1:]
    for seed in range(10):
        c = colors.another_color(current, others, random.Random(seed))
        assert HEX.match(c) and c != current
        assert colors.hue_distance(colors.hue_of(c), colors.hue_of(current)) >= 15
        assert min(colors.hue_distance(colors.hue_of(c), colors.hue_of(o)) for o in others) > 20
