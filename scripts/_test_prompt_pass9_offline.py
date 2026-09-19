"""
Offline test for Prompt Pass 9 changes: no Streamlit, no live network.

Prompt Pass 9 ("prompt2.txt" — conditional win-rate formatting across
Game Logs, plus a Dashboard layout tweak):
  1. New pure color-math helpers in formatting.py: win_rate_background_color()
     and win_rate_cell_style(), implementing a diverging red/white/blue
     scale anchored at a 25% "fair" 4-player-pod win rate.
  2. A new card_view.style_win_rate_percentages() that builds a pandas
     Styler applying that scale (plus whole-percent formatting) over one
     or more columns of a DataFrame.
  3. pages/5_Commander_Game_Tracking.py's "By deck" / "By player" summary
     tables and the Head-to-Head matrix now render through that Styler
     instead of a plain DataFrame of pre-formatted strings.
  4. dashboard.py's sidebar-nav explainer text (instruction #2 of
     prompt2.txt) moved from below the deck tile grid to directly above
     it — this part of the prompt was missed when items 1-3 first landed
     and was added in a follow-up pass.

Covers:
  A. win_rate_background_color() — anchor/extremes/blend math, clamping,
     None/NaN/non-numeric handling, a custom baseline.
  B. win_rate_cell_style() — CSS string vs. "" (Styler no-op) output.
  C. card_view.style_win_rate_percentages() — end-to-end Styler output
     (color hex codes and formatted percentages actually present in the
     rendered HTML; NaN cells get no background-color; columns=None
     styles every column for the head-to-head-matrix use case).
  D. pages/5_Commander_Game_Tracking.py source-text checks — the old
     manual string-formatting lines are gone, the new styling helper is
     wired in for all three tables, and the ELO table (out of scope per
     the prompt — it has no Win % column) is left untouched.
  E. dashboard.py source-text check — the sidebar-nav explainer markdown
     block now appears before the deck-grid render call, not after.

Run from anywhere:
    python scripts/_test_prompt_pass9_offline.py
"""
import math
import os
import sys
import types

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# dashboard_lib.card_view has a real `import streamlit` (for st.dataframe/
# st.markdown/etc. inside its functions); streamlit isn't installed in this
# offline sandbox (see PROJECT_STATE.md's testing caveat), so register a
# minimal stub before importing anything that pulls it in — same trick
# prior _test_*_offline.py files use. style_win_rate_percentages() itself
# never calls any st.* function (it only builds a pandas Styler), so the
# stub doesn't need real cache_data behavior, just to exist.
if "streamlit" not in sys.modules:
    _fake_streamlit = types.ModuleType("streamlit")

    def _fake_cache_data(*args, **kwargs):
        def _decorator(func):
            func.clear = lambda: None
            return func
        return _decorator

    _fake_streamlit.cache_data = _fake_cache_data
    sys.modules["streamlit"] = _fake_streamlit

import pandas as pd

from dashboard_lib import card_view as cv
from dashboard_lib import formatting as fmt


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise SystemExit(f"Test failed: {label}")


def main():
    # ------------------------------------------------------------------
    # A. win_rate_background_color()
    # ------------------------------------------------------------------
    check(
        "exactly the 25% anchor -> neutral white",
        fmt.win_rate_background_color(0.25) == fmt.WIN_RATE_NEUTRAL_HEX,
    )
    check(
        "0% (full underperformance) -> full red",
        fmt.win_rate_background_color(0.0) == fmt.WIN_RATE_RED_HEX,
    )
    check(
        "100% (full overperformance) -> full blue",
        fmt.win_rate_background_color(1.0) == fmt.WIN_RATE_BLUE_HEX,
    )

    # Halfway between white and red (0.125 is halfway from 0.25 down to 0.0).
    half_red = fmt.win_rate_background_color(0.125)
    expected_half_red = fmt._blend_hex(fmt.WIN_RATE_NEUTRAL_HEX, fmt.WIN_RATE_RED_HEX, 0.5)
    check("below-anchor blend is proportional to distance from 25%", half_red == expected_half_red)

    # Halfway between white and blue (0.625 is halfway from 0.25 up to 1.0).
    half_blue = fmt.win_rate_background_color(0.625)
    expected_half_blue = fmt._blend_hex(fmt.WIN_RATE_NEUTRAL_HEX, fmt.WIN_RATE_BLUE_HEX, 0.5)
    check("above-anchor blend is proportional to distance from 25%", half_blue == expected_half_blue)

    check(
        "a value just below the anchor is closer to white than full red",
        fmt.win_rate_background_color(0.24) != fmt.WIN_RATE_RED_HEX
        and fmt.win_rate_background_color(0.24) != fmt.WIN_RATE_NEUTRAL_HEX,
    )

    check(
        "an out-of-range value above 1.0 clamps to full blue, doesn't error",
        fmt.win_rate_background_color(1.5) == fmt.WIN_RATE_BLUE_HEX,
    )
    check(
        "an out-of-range value below 0.0 clamps to full red, doesn't error",
        fmt.win_rate_background_color(-0.5) == fmt.WIN_RATE_RED_HEX,
    )

    check("None returns None (unstyled)", fmt.win_rate_background_color(None) is None)
    check("NaN returns None (unstyled)", fmt.win_rate_background_color(float("nan")) is None)
    check("a non-numeric string returns None (unstyled)", fmt.win_rate_background_color("n/a") is None)

    # A custom baseline (e.g. a straight 1v1 50% anchor) is honored, not
    # hardcoded to 25% — confirms the function is genuinely parametrized
    # even though every current caller uses the 25% default.
    check(
        "a custom baseline is respected",
        fmt.win_rate_background_color(0.5, baseline=0.5) == fmt.WIN_RATE_NEUTRAL_HEX,
    )
    check(
        "a custom baseline changes which side of the scale a value falls on",
        fmt.win_rate_background_color(0.4, baseline=0.5) != fmt.win_rate_background_color(0.4, baseline=0.25),
    )

    # ------------------------------------------------------------------
    # B. win_rate_cell_style()
    # ------------------------------------------------------------------
    style_0 = fmt.win_rate_cell_style(0.0)
    check("cell style for 0% is a background-color declaration", style_0 == f"background-color: {fmt.WIN_RATE_RED_HEX}")
    check("NaN cell style is '' (Styler no-op), not None", fmt.win_rate_cell_style(float("nan")) == "")
    check("None cell style is '' (Styler no-op)", fmt.win_rate_cell_style(None) == "")

    # ------------------------------------------------------------------
    # C. card_view.style_win_rate_percentages()
    # ------------------------------------------------------------------
    deck_like = pd.DataFrame({
        "Deck": ["Alpha", "Bravo", "Charlie"],
        "Played": [4, 6, 2],
        "Win %": [0.0, 0.25, 1.0],
    })
    styler = cv.style_win_rate_percentages(deck_like, columns=["Win %"])
    html = styler.to_html()
    check("styled HTML contains the full-red hex for the 0% row", fmt.WIN_RATE_RED_HEX in html)
    check("styled HTML contains the full-blue hex for the 100% row", fmt.WIN_RATE_BLUE_HEX in html)
    check("styled HTML renders whole-percent text ('100%')", "100%" in html)
    check(
        "the 'Played' column (not in `columns`) isn't percent-formatted",
        "4</td>" in html or ">4<" in html,  # the raw int 4 survives untouched
    )

    # columns=None (the head-to-head-matrix case): every column styled,
    # NaN cells (the diagonal) left uncolored, still rendered as the dash.
    h2h_like = pd.DataFrame(
        {"Alice": [float("nan"), 1.0], "Bob": [0.0, float("nan")]},
        index=["Alice", "Bob"],
    )
    h2h_styler = cv.style_win_rate_percentages(h2h_like)
    h2h_html = h2h_styler.to_html()
    check("head-to-head styling covers every column with columns=None", fmt.WIN_RATE_RED_HEX in h2h_html and fmt.WIN_RATE_BLUE_HEX in h2h_html)
    check("head-to-head NaN diagonal cells show the em-dash, not a stray '%'", h2h_html.count("\u2014") == 2)
    check(
        "head-to-head NaN diagonal cells get no background-color (only the 2 real cells do)",
        h2h_html.count("background-color") == 2,
    )

    # A custom na_rep is honored.
    custom_na = cv.style_win_rate_percentages(
        pd.DataFrame({"Win %": [float("nan")]}), columns=["Win %"], na_rep="N/A"
    ).to_html()
    check("a custom na_rep is used instead of the default dash", "N/A" in custom_na)

    # ------------------------------------------------------------------
    # D. Source-text checks on the actual page file — confirms the wiring
    #    landed (not just that the helper functions work in isolation),
    #    and that the ELO table (out of scope — no Win % column) wasn't
    #    touched.
    # ------------------------------------------------------------------
    page_path = os.path.join(PROJECT_ROOT, "pages", "5_Commander_Game_Tracking.py")
    with open(page_path, encoding="utf-8") as f:
        page_text = f.read()

    check(
        "By-deck/By-player tables now route through style_win_rate_percentages()",
        page_text.count("cv.style_win_rate_percentages(") >= 3,  # deck table + player table + h2h table
    )
    check(
        "the old manual '{v * 100:.0f}%' string-formatting lines are gone",
        "v * 100:.0f" not in page_text,
    )
    check(
        "a 25% baseline is explained to the user somewhere on the page",
        "25%" in page_text,
    )
    check(
        "the ELO ratings table (Rating/Games, no Win % column) is untouched by the styling helper",
        "elo_df.rename(columns={\"player_name\": \"Player\", \"rating\": \"Rating\", \"games_played\": \"Games\"})"
        in page_text
        and "style_win_rate_percentages(elo_df" not in page_text,
    )

    # ------------------------------------------------------------------
    # E. dashboard.py source-text check — the sidebar-nav explainer text
    #    (prompt2.txt instruction #2) sits above the deck tile grid now,
    #    not below it.
    # ------------------------------------------------------------------
    dashboard_path = os.path.join(PROJECT_ROOT, "dashboard.py")
    with open(dashboard_path, encoding="utf-8") as f:
        dashboard_text = f.read()

    markdown_pos = dashboard_text.index("Use the sidebar to jump to:")
    grid_call_pos = dashboard_text.index("cv.render_deck_landing_grid(")
    check(
        "dashboard.py's sidebar-nav explainer text now precedes the deck tile grid render call",
        markdown_pos < grid_call_pos,
    )

    print("\nAll Prompt Pass 9 offline checks passed.")


if __name__ == "__main__":
    main()
