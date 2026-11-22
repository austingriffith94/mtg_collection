"""Rendering helpers in dashboard_lib/card_view.py that can run outside a
page: the deck image fallback chain, the Decks page header and accent CSS,
the home-page tile grid, and the win-rate Styler. Streamlit calls are
captured with monkeypatch, so the HTML/CSS they would emit can be read.

Replaces the card_view checks of the old scripts/_test_*_offline.py
harnesses (NaN-image hotfix, Prompt Passes 9 and 10).
"""
import pandas as pd
import pytest

from dashboard_lib import card_view as cv
from dashboard_lib import formatting as fmt
from dashboard_lib import queries as q

NAN = float("nan")


@pytest.fixture
def markdown_calls(monkeypatch):
    """Every st.markdown() text card_view emits, in order."""
    calls = []
    monkeypatch.setattr(cv.st, "markdown", lambda text, *a, **kw: calls.append(text))
    return calls


# ------------------------------------------------------------------
# deck_image_src: local cover -> local commander art -> remote URL -> None
# ------------------------------------------------------------------
def test_deck_image_src_is_public_and_the_old_private_name_is_gone():
    assert hasattr(cv, "deck_image_src")
    assert not hasattr(cv, "_deck_image_src")


def test_deck_image_src_falls_back_to_the_remote_commander_image():
    row = pd.Series({"cover_image_path": None, "commander_local_image_path": None,
                     "commander_image_uri": "https://example.com/commander.jpg"})
    assert cv.deck_image_src(row) == "https://example.com/commander.jpg"


def test_deck_image_src_none_when_nothing_resolves():
    row = pd.Series({"cover_image_path": None, "commander_local_image_path": None,
                     "commander_image_uri": None})
    assert cv.deck_image_src(row) is None


def test_deck_image_src_resolves_a_real_list_decks_with_covers_row(conn):
    conn.execute("INSERT INTO decks (name, commander, color_identity) VALUES ('Test Deck', 'Test Commander', 'RB')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number, image_uri) "
                 "VALUES ('sid-1', 'Test Commander', 'tst', '1', 'https://example.com/art.jpg')")
    deck_id = conn.execute("SELECT deck_id FROM decks").fetchone()[0]
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?, 'sid-1', 1)", (deck_id,))
    conn.commit()
    covers = q.list_decks_with_covers(conn)
    assert cv.deck_image_src(covers.iloc[0]) == "https://example.com/art.jpg"


def test_landing_grid_survives_all_nan_image_columns(monkeypatch):
    # The home-page crash: when no deck has a cover, pandas types the whole
    # column float64 NaN. Built by hand because whether read_sql_query does
    # that depends on the pandas version.
    decks = pd.DataFrame({
        "deck_id": [1, 2], "name": ["Test Deck One", "Test Deck Two"],
        "representative": [None, None], "commander": ["Some Commander", None],
        "cover_image_path": [NAN, NAN], "commander_image_uri": [NAN, NAN],
        "commander_local_image_path": [NAN, NAN],
    })
    assert pd.api.types.is_float_dtype(decks["cover_image_path"])  # the bug's precondition
    for _, row in decks.iterrows():
        assert cv.deck_image_src(row) is None

    class _Column:
        def markdown(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cv.st, "columns", lambda n, *a, **kw: [_Column() for _ in range(n)])
    monkeypatch.setattr(cv.st, "markdown", lambda *a, **kw: None)
    cv.render_deck_landing_grid(decks)  # must not raise


# ------------------------------------------------------------------
# render_deck_header / inject_deck_accent_css
# ------------------------------------------------------------------
def test_deck_header_without_art_shows_name_tagline_and_color_symbols(markdown_calls):
    cv.render_deck_header("Raktres, Lord of Discounts", "Big Mana Group Slug", None, "RB")
    html = markdown_calls[-1]
    assert "Raktres, Lord of Discounts" in html
    assert "Big Mana Group Slug" in html
    assert "card-symbols/B.svg" in html and "card-symbols/R.svg" in html
    # The mana badges are <img> too, so look for the thumbnail's own style.
    assert "width:96px" not in html


def test_deck_header_with_art_but_no_tagline(markdown_calls):
    cv.render_deck_header("Test Deck", None, "https://example.com/art.jpg", "")
    html = markdown_calls[-1]
    assert "https://example.com/art.jpg" in html
    assert "opacity:0.75" not in html          # no empty tagline div
    assert "card-symbols/C.svg" in html        # colorless deck


def test_deck_header_escapes_html(markdown_calls):
    cv.render_deck_header("A & B <Deck>", "5 < 10 & counting", None, "")
    html = markdown_calls[-1]
    assert "&amp;" in html and "&lt;Deck&gt;" in html
    assert "5 &lt; 10 &amp; counting" in html


def test_accent_css_carries_the_gradient_and_targets_the_themed_elements(markdown_calls):
    gradient, accent = fmt.deck_accent_gradient("RB"), fmt.deck_accent_hex("RB")
    cv.inject_deck_accent_css(gradient, accent)
    css = markdown_calls[-1]
    assert gradient in css and accent in css
    assert "hr {" in css                         # st.divider()
    assert 'data-testid="stExpander"' in css
    assert 'data-testid="stSidebar"' in css


# ------------------------------------------------------------------
# style_win_rate_percentages
# ------------------------------------------------------------------
def test_win_rate_styler_colors_and_formats_the_chosen_columns():
    df = pd.DataFrame({"Deck": ["Alpha", "Bravo", "Charlie"], "Played": [4, 6, 2],
                       "Win %": [0.0, 0.25, 1.0]})
    html = cv.style_win_rate_percentages(df, columns=["Win %"]).to_html()
    assert fmt.WIN_RATE_RED_HEX in html and fmt.WIN_RATE_GREEN_HEX in html
    assert "100%" in html
    assert ">4<" in html                          # "Played" is not percent-formatted


def test_win_rate_styler_columns_none_styles_everything_and_skips_nan():
    # The head-to-head matrix: every column, NaN diagonal left plain.
    h2h = pd.DataFrame({"Alice": [NAN, 1.0], "Bob": [0.0, NAN]}, index=["Alice", "Bob"])
    html = cv.style_win_rate_percentages(h2h).to_html()
    assert fmt.WIN_RATE_RED_HEX in html and fmt.WIN_RATE_GREEN_HEX in html
    assert html.count("—") == 2             # em-dash for the two NaN cells
    assert html.count("background-color") == 2   # only the two real cells


def test_win_rate_styler_custom_na_rep():
    html = cv.style_win_rate_percentages(pd.DataFrame({"Win %": [NAN]}),
                                         columns=["Win %"], na_rep="N/A").to_html()
    assert "N/A" in html
