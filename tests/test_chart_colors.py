"""Phase 6 of UI_ACTION_PLAN.md: one mana palette, a separate category palette."""
from dashboard_lib import formatting as fmt


def test_mana_colors_cover_every_color_bucket():
    assert set(fmt.MANA_COLORS) == {"W", "U", "B", "R", "G", "C", "Multi"}


def test_legacy_palettes_derive_from_mana_colors():
    for letter in fmt.WUBRG_ORDER:
        assert fmt.MANA_CURVE_COLOR_HEX[letter] == fmt.MANA_COLORS[letter]
        assert fmt.MANA_SOURCE_COLOR_HEX[letter] == fmt.MANA_COLORS[letter]
    assert fmt.MANA_CURVE_COLOR_HEX["Multi-color"] == fmt.MANA_COLORS["Multi"]
    assert fmt.MANA_CURVE_COLOR_HEX["Colorless"] == fmt.MANA_COLORS["C"]


def test_category_palette_never_reuses_a_mana_color():
    assert not set(fmt.CATEGORY_COLORS) & set(fmt.MANA_COLORS.values())


def test_category_colors_cycles_past_the_palette():
    n = len(fmt.CATEGORY_COLORS)
    colors = fmt.category_colors(n + 2)
    assert len(colors) == n + 2
    assert colors[n] == colors[0]
