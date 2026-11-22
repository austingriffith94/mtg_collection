"""Pure helpers in dashboard_lib/formatting.py: image-path and URL guards,
mana-curve color buckets, mana-symbol URLs, deck accent colors, and the
win-rate color scale. No database, no Streamlit.

Replaces the pure-function checks of the old scripts/_test_*_offline.py
harnesses (NaN-image hotfix, Phase 4, Prompt Passes 9 and 10).
"""
import pytest

from dashboard_lib import formatting as fmt

NAN = float("nan")
SYMBOL = "https://svgs.scryfall.io/card-symbols/{}.svg"


# ------------------------------------------------------------------
# resolve_local_image / scryfall_card_url never choke on pandas NaN
# ------------------------------------------------------------------
# History: a column that is NULL for every row comes back from
# pandas.read_sql_query as float64 NaN, which is truthy, so an old
# `if not path:` guard let it reach os.path.join() and crash the home page.
@pytest.mark.parametrize("value", [NAN, None, "", "   ", 0])
def test_resolve_local_image_returns_none_for_non_paths(value):
    assert fmt.resolve_local_image(value) is None


def test_resolve_local_image_none_for_a_missing_file():
    assert fmt.resolve_local_image("image_cache/definitely_not_a_real_file_12345.jpg") is None


@pytest.mark.parametrize("set_code, number", [(NAN, NAN), (None, None)])
def test_scryfall_card_url_none_without_a_printing(set_code, number):
    assert fmt.scryfall_card_url(set_code, number) is None


def test_scryfall_card_url_builds_the_normal_url():
    assert fmt.scryfall_card_url("c21", "263") == "https://scryfall.com/card/c21/263"


# ------------------------------------------------------------------
# Mana-curve color buckets
# ------------------------------------------------------------------
@pytest.mark.parametrize("identity, bucket", [
    ("W", "W"), ("G", "G"),
    ("W/U", "Multi-color"), ("W/U/B", "Multi-color"),
    ("Colorless", "Colorless"), ("", "Colorless"), (None, "Colorless"),
])
def test_mana_curve_color_bucket(identity, bucket):
    assert fmt.mana_curve_color_bucket(identity) == bucket


def test_mana_curve_order_is_fixed_and_every_bucket_has_a_color():
    assert fmt.MANA_CURVE_COLOR_ORDER == ["W", "U", "B", "R", "G", "Multi-color", "Colorless"]
    assert all(c in fmt.MANA_CURVE_COLOR_HEX for c in fmt.MANA_CURVE_COLOR_ORDER)


# ------------------------------------------------------------------
# Mana-symbol URLs
# ------------------------------------------------------------------
@pytest.mark.parametrize("letter", ["W", "w", "C"])
def test_mana_symbol_svg_url_accepts_wubrgc_any_case(letter):
    assert fmt.mana_symbol_svg_url(letter) == SYMBOL.format(letter.upper())


@pytest.mark.parametrize("value", ["X", None, ""])
def test_mana_symbol_svg_url_rejects_anything_else(value):
    assert fmt.mana_symbol_svg_url(value) is None


@pytest.mark.parametrize("identity, letters", [
    ("RB", "BR"),       # WUBRG order, not input order
    ("GUW", "WUG"),
    ("", "C"),          # a colorless deck still gets one symbol
    (None, "C"),
])
def test_deck_color_identity_symbol_urls_in_wubrg_order(identity, letters):
    assert fmt.deck_color_identity_symbol_urls(identity) == [SYMBOL.format(c) for c in letters]


# ------------------------------------------------------------------
# Deck accent color / gradient
# ------------------------------------------------------------------
@pytest.mark.parametrize("identity, bucket", [
    ("RB", "B"),            # first of WUBRG wins
    ("GUW", "W"),
    ("", "Colorless"),
    (None, "Colorless"),
])
def test_deck_accent_hex_picks_the_first_color(identity, bucket):
    assert fmt.deck_accent_hex(identity) == fmt.MANA_CURVE_COLOR_HEX[bucket]


@pytest.mark.parametrize("identity, buckets", [
    ("RB", ["B", "R"]),             # the deck's real colors, not flat Multi-color
    ("GUW", ["W", "U", "G"]),
    ("R", ["R", "R"]),              # mono repeats its hex: a gradient needs 2 stops
    ("", ["Colorless", "Colorless"]),
])
def test_deck_accent_gradient_stops(identity, buckets):
    stops = ", ".join(fmt.MANA_CURVE_COLOR_HEX[b] for b in buckets)
    assert fmt.deck_accent_gradient(identity) == f"linear-gradient(90deg, {stops})"


# ------------------------------------------------------------------
# Win-rate colors: red below 20%, amber 20-35% inclusive, green above
# ------------------------------------------------------------------
@pytest.mark.parametrize("rate, color", [
    (0.0, fmt.WIN_RATE_RED_HEX), (0.19, fmt.WIN_RATE_RED_HEX),
    (0.20, fmt.WIN_RATE_YELLOW_HEX),    # inclusive boundaries
    (0.25, fmt.WIN_RATE_YELLOW_HEX),    # a fair 4-player pod rate
    (0.35, fmt.WIN_RATE_YELLOW_HEX),
    (0.36, fmt.WIN_RATE_GREEN_HEX), (1.0, fmt.WIN_RATE_GREEN_HEX),
    (1.5, fmt.WIN_RATE_GREEN_HEX),      # out of range clamps instead of erroring
    (-0.5, fmt.WIN_RATE_RED_HEX),
])
def test_win_rate_background_color_buckets(rate, color):
    assert fmt.win_rate_background_color(rate) == color


@pytest.mark.parametrize("value", [None, NAN, "n/a"])
def test_win_rate_background_color_unstyled_for_non_numbers(value):
    assert fmt.win_rate_background_color(value) is None


def test_win_rate_thresholds_are_parametrized():
    assert fmt.win_rate_background_color(0.5, low=0.45, high=0.55) == fmt.WIN_RATE_YELLOW_HEX
    assert (fmt.win_rate_background_color(0.4, low=0.45, high=0.55)
            != fmt.win_rate_background_color(0.4, low=0.20, high=0.35))


def test_win_rate_cell_style_is_css_or_empty_string():
    assert fmt.win_rate_cell_style(0.0) == f"background-color: {fmt.WIN_RATE_RED_HEX}"
    # "" (not None) is what a pandas Styler treats as a no-op.
    assert fmt.win_rate_cell_style(NAN) == ""
    assert fmt.win_rate_cell_style(None) == ""
