"""
Offline test for Prompt Pass 10 changes: no Streamlit, no live network.

Prompt Pass 10 ("prompt3.txt" — Deck page branding, layout, color
identity highlights, and tag lists):
  1. Header & Title: the old generic "🃏 Decks" st.title() and the old
     per-deck st.header(representative) block are both gone, replaced by
     card_view.render_deck_header() — the deck's own Name (decks.name,
     not the separate "representative" field) next to its resolved
     thumbnail image (cover image, else commander art, "if available").
  2. Deck Details & Taglines: the short descriptive tagline
     (decks.description) now renders directly beneath the deck name in
     that header, not below the Commander/Partner/.../Built stat grid.
  3. Color Identity & Visual Accents: new formatting.py helpers
     (mana_symbol_svg_url() / deck_color_identity_symbol_urls()) build
     official Scryfall mana-symbol badge URLs for a deck's color
     identity, rendered in the header; new formatting.py helpers
     (deck_accent_hex() / deck_accent_gradient()) build CSS accent
     colors from a deck's color identity, applied page-wide via a new
     card_view.inject_deck_accent_css().
  4. Game Changers Section: the "Status" column is gone from the Decks
     page's Game Changers table display — Card and Your tag only.

Also covers the card_view.py refactor this pass required: the
previously-private `_deck_image_src()` (single caller:
render_deck_landing_grid()) was renamed to the public `deck_image_src()`
since the Decks page's new header is now a second caller reusing the
exact same cover-image/commander-art fallback chain.

Run from anywhere:
    python scripts/_test_prompt_pass10_offline.py
"""
import os
import sqlite3
import sys
import types

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# dashboard_lib.card_view has a real `import streamlit`; streamlit isn't
# installed in this offline sandbox (see PROJECT_STATE.md's testing
# caveat), so register a minimal stub before importing anything that
# pulls it in — same trick prior _test_*_offline.py files use. Calls to
# st.markdown() are captured so the rendered HTML/CSS can be inspected
# directly, the same approach _test_hotfix_nan_image_offline.py uses for
# st.columns()/st.markdown().
if "streamlit" not in sys.modules:
    _fake_streamlit = types.ModuleType("streamlit")

    def _fake_cache_data(*args, **kwargs):
        def _decorator(func):
            func.clear = lambda: None
            return func
        return _decorator

    _markdown_calls = []

    def _fake_markdown(text, **kwargs):
        _markdown_calls.append(text)

    _fake_streamlit.cache_data = _fake_cache_data
    _fake_streamlit.markdown = _fake_markdown
    _fake_streamlit._markdown_calls = _markdown_calls
    sys.modules["streamlit"] = _fake_streamlit

import pandas as pd

from dashboard_lib import card_view as cv
from dashboard_lib import formatting as fmt
from dashboard_lib import queries as q


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise SystemExit(f"Test failed: {label}")


def last_markdown_call():
    return sys.modules["streamlit"]._markdown_calls[-1]


def fresh_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(PROJECT_ROOT, "schema.sql")) as f:
        conn.executescript(f.read())
    return conn


def main():
    # ------------------------------------------------------------------
    # A. formatting.py — mana-symbol URL builders
    # ------------------------------------------------------------------
    check("mana_symbol_svg_url('W') builds the expected Scryfall SVG URL", fmt.mana_symbol_svg_url("W") == "https://svgs.scryfall.io/card-symbols/W.svg")
    check("mana_symbol_svg_url is case-insensitive", fmt.mana_symbol_svg_url("w") == fmt.mana_symbol_svg_url("W"))
    check("mana_symbol_svg_url('C') (colorless) is valid", fmt.mana_symbol_svg_url("C") == "https://svgs.scryfall.io/card-symbols/C.svg")
    check("mana_symbol_svg_url rejects a non-WUBRG/C letter", fmt.mana_symbol_svg_url("X") is None)
    check("mana_symbol_svg_url(None) returns None, doesn't raise", fmt.mana_symbol_svg_url(None) is None)
    check("mana_symbol_svg_url('') returns None", fmt.mana_symbol_svg_url("") is None)

    check(
        "deck_color_identity_symbol_urls('RB') returns exactly 2 URLs, in WUBRG order (B before R)",
        fmt.deck_color_identity_symbol_urls("RB") == [
            "https://svgs.scryfall.io/card-symbols/B.svg",
            "https://svgs.scryfall.io/card-symbols/R.svg",
        ],
    )
    check(
        "deck_color_identity_symbol_urls('GUW') is reordered to WUBRG (W, U, G)",
        fmt.deck_color_identity_symbol_urls("GUW") == [
            "https://svgs.scryfall.io/card-symbols/W.svg",
            "https://svgs.scryfall.io/card-symbols/U.svg",
            "https://svgs.scryfall.io/card-symbols/G.svg",
        ],
    )
    check(
        "deck_color_identity_symbol_urls('') (colorless deck) still returns one URL, the Colorless symbol",
        fmt.deck_color_identity_symbol_urls("") == ["https://svgs.scryfall.io/card-symbols/C.svg"],
    )
    check(
        "deck_color_identity_symbol_urls(None) also falls back to the Colorless symbol, doesn't raise/return []",
        fmt.deck_color_identity_symbol_urls(None) == ["https://svgs.scryfall.io/card-symbols/C.svg"],
    )

    # ------------------------------------------------------------------
    # B. formatting.py — accent color/gradient builders
    # ------------------------------------------------------------------
    check("deck_accent_hex('RB') picks the WUBRG-first color (B, not R)", fmt.deck_accent_hex("RB") == fmt.MANA_CURVE_COLOR_HEX["B"])
    check("deck_accent_hex('GUW') picks W (first in WUBRG order)", fmt.deck_accent_hex("GUW") == fmt.MANA_CURVE_COLOR_HEX["W"])
    check("deck_accent_hex('') (colorless) uses the Colorless hex", fmt.deck_accent_hex("") == fmt.MANA_CURVE_COLOR_HEX["Colorless"])
    check("deck_accent_hex(None) doesn't raise, uses the Colorless hex", fmt.deck_accent_hex(None) == fmt.MANA_CURVE_COLOR_HEX["Colorless"])

    check(
        "deck_accent_gradient('RB') is a 2-stop gradient of the deck's REAL colors (B, R) — not the flat Multi-color goldenrod",
        fmt.deck_accent_gradient("RB")
        == f"linear-gradient(90deg, {fmt.MANA_CURVE_COLOR_HEX['B']}, {fmt.MANA_CURVE_COLOR_HEX['R']})",
    )
    check(
        "deck_accent_gradient('GUW') has 3 stops, one per actual color, in WUBRG order",
        fmt.deck_accent_gradient("GUW")
        == f"linear-gradient(90deg, {fmt.MANA_CURVE_COLOR_HEX['W']}, {fmt.MANA_CURVE_COLOR_HEX['U']}, {fmt.MANA_CURVE_COLOR_HEX['G']})",
    )
    check(
        "deck_accent_gradient('R') (mono-color) repeats its one hex as 2 stops, not a bare single-color string",
        fmt.deck_accent_gradient("R") == f"linear-gradient(90deg, {fmt.MANA_CURVE_COLOR_HEX['R']}, {fmt.MANA_CURVE_COLOR_HEX['R']})",
    )
    check(
        "deck_accent_gradient('') (colorless) repeats the Colorless hex as 2 stops",
        fmt.deck_accent_gradient("")
        == f"linear-gradient(90deg, {fmt.MANA_CURVE_COLOR_HEX['Colorless']}, {fmt.MANA_CURVE_COLOR_HEX['Colorless']})",
    )

    # ------------------------------------------------------------------
    # C. card_view.py — deck_image_src() rename (was _deck_image_src())
    # ------------------------------------------------------------------
    check("card_view.deck_image_src (public) exists", hasattr(cv, "deck_image_src"))
    check("the old private card_view._deck_image_src is gone", not hasattr(cv, "_deck_image_src"))

    remote_only_row = pd.Series({
        "cover_image_path": None,
        "commander_local_image_path": None,
        "commander_image_uri": "https://example.com/commander.jpg",
    })
    check(
        "deck_image_src() falls back to the remote commander image URL when nothing local resolves",
        cv.deck_image_src(remote_only_row) == "https://example.com/commander.jpg",
    )
    nothing_row = pd.Series({"cover_image_path": None, "commander_local_image_path": None, "commander_image_uri": None})
    check("deck_image_src() returns None when nothing resolves at all", cv.deck_image_src(nothing_row) is None)

    # End-to-end: a real queries.list_decks_with_covers() row (the exact
    # shape both render_deck_landing_grid() and the Decks page's new
    # header now share) feeds cleanly into deck_image_src().
    conn = fresh_conn()
    conn.execute(
        "INSERT INTO decks (name, commander, color_identity) VALUES ('Test Deck', 'Test Commander', 'RB')"
    )
    conn.execute(
        "INSERT INTO cards (scryfall_id, name, set_code, collector_number, image_uri) "
        "VALUES ('sid-1', 'Test Commander', 'tst', '1', 'https://example.com/real-commander-art.jpg')"
    )
    deck_id = conn.execute("SELECT deck_id FROM decks WHERE name = 'Test Deck'").fetchone()[0]
    conn.execute(
        "INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?, 'sid-1', 1)",
        (deck_id,),
    )
    conn.commit()
    covers_df = q.list_decks_with_covers(conn)
    row = covers_df[covers_df["deck_id"] == deck_id].iloc[0]
    check(
        "list_decks_with_covers() row for a deck with no cover image, real commander image_uri -> deck_image_src() resolves the remote commander art",
        cv.deck_image_src(row) == "https://example.com/real-commander-art.jpg",
    )
    conn.close()

    # ------------------------------------------------------------------
    # D. card_view.py — render_deck_header()
    # ------------------------------------------------------------------
    cv.render_deck_header("Raktres, Lord of Discounts", "Big Mana Group Slug", None, "RB")
    html_no_image = last_markdown_call()
    check("render_deck_header renders the deck name", "Raktres, Lord of Discounts" in html_no_image)
    check("render_deck_header renders the tagline", "Big Mana Group Slug" in html_no_image)
    check(
        "render_deck_header renders both color-identity mana symbols (B and R)",
        "card-symbols/B.svg" in html_no_image and "card-symbols/R.svg" in html_no_image,
    )
    # (mana-symbol badges are themselves <img> tags, so this checks
    # specifically for the deck-art thumbnail's own distinguishing style
    # — width:96px — rather than a blanket "no <img> at all" assertion.)
    check("render_deck_header renders no deck-art thumbnail <img> when image_src is None", "width:96px" not in html_no_image)

    cv.render_deck_header("Test Deck", None, "https://example.com/art.jpg", "")
    html_with_image_no_tagline = last_markdown_call()
    check("render_deck_header renders the thumbnail <img> when image_src is given", "https://example.com/art.jpg" in html_with_image_no_tagline)
    check("render_deck_header omits the tagline block entirely when tagline is None (no empty div)", "opacity:0.75" not in html_with_image_no_tagline)
    check("render_deck_header shows the Colorless mana symbol for a colorless deck", "card-symbols/C.svg" in html_with_image_no_tagline)

    # HTML-escaping: a deck name/tagline containing '<' or '&' must not
    # break out of the surrounding markup.
    cv.render_deck_header("A & B <Deck>", "5 < 10 & counting", None, "")
    html_escaped = last_markdown_call()
    check("render_deck_header HTML-escapes the deck name", "&amp;" in html_escaped and "&lt;Deck&gt;" in html_escaped)
    check("render_deck_header HTML-escapes the tagline", "5 &lt; 10 &amp; counting" in html_escaped)

    # ------------------------------------------------------------------
    # E. card_view.py — inject_deck_accent_css()
    # ------------------------------------------------------------------
    gradient = fmt.deck_accent_gradient("RB")
    accent_hex = fmt.deck_accent_hex("RB")
    cv.inject_deck_accent_css(gradient, accent_hex)
    css = last_markdown_call()
    check("inject_deck_accent_css embeds the gradient CSS", gradient in css)
    check("inject_deck_accent_css embeds the accent hex", accent_hex in css)
    check("inject_deck_accent_css targets hr (st.divider())", "hr {" in css)
    check("inject_deck_accent_css targets the expander container", 'data-testid="stExpander"' in css)
    check("inject_deck_accent_css targets the sidebar", 'data-testid="stSidebar"' in css)

    # ------------------------------------------------------------------
    # F. Source-text checks on the actual page file — confirms the
    #    wiring landed, not just that the helper functions work in
    #    isolation.
    # ------------------------------------------------------------------
    page_path = os.path.join(PROJECT_ROOT, "pages", "2_Decks.py")
    with open(page_path, encoding="utf-8") as f:
        page_text = f.read()

    check('the old generic st.title("🃏 Decks") page title is gone', 'st.title("🃏 Decks")' not in page_text)
    check(
        "the old representative-preferring header_name line is gone",
        'meta.get("representative") or meta.get("name")' not in page_text,
    )
    check("the new deck header is wired in via render_deck_header()", "cv.render_deck_header(" in page_text)
    check("the new page-wide accent CSS is wired in via inject_deck_accent_css()", "cv.inject_deck_accent_css(" in page_text)
    check(
        "the old standalone description caption right after the stat grid is gone",
        'st.caption(meta["description"])' not in page_text,
    )
    check(
        "the Game Changers table no longer renders a Status column",
        '["Card", "Your tag", "Status"]' not in page_text and '"status": "Status"' not in page_text,
    )
    check(
        "the Game Changers table still renders Card and Your tag",
        '["Card", "Your tag"]' in page_text,
    )
    check(
        "the old 'mismatches are worth reviewing' Game Changers help text is gone",
        "mismatches are worth reviewing" not in page_text,
    )

    print("\nAll Prompt Pass 10 offline checks passed.")


if __name__ == "__main__":
    main()
