"""
Land Probability page.

For a chosen deck: probability of lands (or any specific card) appearing
in the opening 7, an estimate of hitting your land drop each of the first
5 turns, and the probability of having a mana source for each color in
the commander's color identity at the opening hand and each of the first
5 turns.

All of this is hypergeometric draw math (dashboard_lib/probability.py)
applied to the deck's actual mainboard, MINUS the commander/partner (they
sit in the command zone, never in the shuffled library) — see
queries.deck_library_dataframe().

Simplifying assumptions, stated here once rather than repeated in every
caption:
  - No mulligans, scrys, fetches, extra draw spells, etc. — this is the
    baseline "just drawing for turn" probability.
  - "Hitting your land drop by turn N" means having drawn AT LEAST N
    lands by that point (i.e. enough to have played one every turn),
    not that turn's draw specifically being a land.
  - Color-source counts default to LAND-type cards only, since Scryfall's
    color_identity field reliably reflects a land's produced colors but
    does NOT reliably reflect a nonland mana source's colors when the
    card's own text describes color-fixing in words rather than colored
    mana symbols (Sol Ring, Arcane Signet, Birds of Paradise, etc.). An
    opt-in toggle extends the count to all card types as an approximation.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from dashboard_lib import db, loaders, formatting as fmt, probability as prob

st.set_page_config(page_title="Land Probability · MTG Dashboard", page_icon="🎲", layout="wide")

db.require_db()
conn = db.get_connection()

st.title("🎲 Land & Color Probability")

st.sidebar.header("Deck")
db.refresh_data_button()
include_retired = st.sidebar.toggle("Show retired decks", value=True, key="landprob_include_retired")

decks_df = loaders.load_decks_df(conn, include_retired=include_retired)
if decks_df.empty:
    st.info("No decks found.")
    st.stop()

deck_labels = [
    row["name"] + ("  (retired)" if not row["is_active"] else "")
    for _, row in decks_df.iterrows()
]
label_to_id = dict(zip(deck_labels, decks_df["deck_id"]))
chosen_label = st.sidebar.selectbox("Choose a deck", deck_labels, key="landprob_deck_select")
deck_id = int(label_to_id[chosen_label])

meta = loaders.load_deck_meta(conn, deck_id)
library_df = loaders.load_deck_library_df(conn, deck_id)

header_name = meta.get("representative") or meta.get("name") or "Deck"
st.header(header_name)

if library_df.empty:
    st.warning("No mainboard cards loaded for this deck yet — nothing to calculate.")
    st.stop()

detected_library_size = int(library_df["quantity"].sum())
detected_land_count = int(library_df.loc[library_df["card_type"] == "Land", "quantity"].sum())

commander_names = {n for n in (meta.get("commander"), meta.get("partner")) if n}
found_names = set(loaders.load_deck_cards_df(conn, deck_id)["name"]) & commander_names
if commander_names and found_names != commander_names:
    st.caption(
        "⚠️ Couldn't match the commander/partner name(s) exactly against this deck's "
        "mainboard rows, so the library size below may still include them. "
        "Double check the numbers if this deck uses an unusual naming."
    )

st.markdown("#### Deck size & land count")
size_col1, size_col2, size_col3, size_col4 = st.columns(4)
library_size = size_col1.number_input(
    "Library size (shuffled deck, excl. commander)",
    min_value=1, max_value=500, value=detected_library_size, step=1,
    key=f"landprob_{deck_id}_libsize",
)
land_count = size_col2.number_input(
    "Lands in library", min_value=0, max_value=library_size, value=min(detected_land_count, library_size), step=1,
    key=f"landprob_{deck_id}_landcount",
)
on_the_play = size_col3.radio(
    "Draw step assumption", ["On the play (skip turn 1 draw)", "On the draw / not first"],
    key=f"landprob_{deck_id}_onplay",
) == "On the play (skip turn 1 draw)"
size_col4.metric("Detected", f"{detected_land_count} lands / {detected_library_size} cards")

st.divider()

# ------------------------------------------------------------------
# Section A — land drops
# ------------------------------------------------------------------
st.markdown("### Hitting your land drops")
st.caption(
    "Probability of having drawn **at least N lands by turn N** — i.e. enough to have "
    "played a land every turn so far, assuming no mulligans or extra draw."
)

rows = []
for turn in range(0, 6):
    seen = prob.cards_seen_by_turn(turn, on_the_play)
    needed = 1 if turn == 0 else turn
    label = "Opening hand" if turn == 0 else f"Turn {turn}"
    p = prob.prob_at_least(library_size, land_count, seen, needed)
    rows.append({
        "When": label,
        "Cards seen": seen,
        "Lands needed": needed,
        "Probability": f"{p * 100:.1f}%",
    })
land_drop_table = pd.DataFrame(rows).set_index("When")
st.dataframe(land_drop_table, use_container_width=True)

st.markdown("**Distribution of lands in your opening 7**")
dist = prob.distribution(library_size, land_count, prob.OPENING_HAND_SIZE)
dist_series = pd.Series(dist, name="Probability").rename_axis("Lands in opening 7")
st.bar_chart(dist_series)

st.divider()

# ------------------------------------------------------------------
# Section B — color availability
# ------------------------------------------------------------------
st.markdown("### Color availability")

ordered_colors = fmt.deck_color_identity_letters(meta.get("color_identity"))

if not ordered_colors:
    st.info("This commander's color identity is colorless — no colored mana requirements to check.")
else:
    include_nonland = st.toggle(
        "Also count nonland mana rocks/dorks toward color sources (approximate)",
        value=False,
        key=f"landprob_{deck_id}_nonland_sources",
        help=(
            "Off (default): only Land-type cards count, which matches Scryfall's color data "
            "reliably (basics, duals, triomes, filter lands, etc.). On: also counts nonland "
            "cards whose color identity includes the color — but this misses rocks/dorks "
            "worded in plain text rather than colored mana symbols (Sol Ring, Arcane Signet, "
            "Birds of Paradise), so treat it as a lower bound, not exact."
        ),
    )

    source_counts = {
        c: prob.count_color_sources(library_df, c, lands_only=not include_nonland)
        for c in ordered_colors
    }
    st.caption(
        "Detected sources — "
        + ", ".join(f"**{c}**: {source_counts[c]}" for c in ordered_colors)
    )

    chart_rows, table_rows = [], []
    for turn in range(0, 6):
        seen = prob.cards_seen_by_turn(turn, on_the_play)
        label = "Hand" if turn == 0 else f"T{turn}"
        chart_row = {"turn": turn}
        table_row = {"When": "Opening hand" if turn == 0 else f"Turn {turn}", "Cards seen": seen}
        for c in ordered_colors:
            p = prob.prob_at_least(library_size, source_counts[c], seen, 1)
            chart_row[c] = p
            table_row[c] = f"{p * 100:.1f}%"
        chart_rows.append(chart_row)
        table_rows.append(table_row)

    color_table = pd.DataFrame(table_rows).set_index("When")
    st.dataframe(color_table, use_container_width=True)

    color_chart = pd.DataFrame(chart_rows).set_index("turn")
    st.line_chart(color_chart)

st.divider()

# ------------------------------------------------------------------
# Section C — check a specific card
# ------------------------------------------------------------------
st.markdown("### Check a specific card")
card_names = sorted(library_df["name"].unique())
chosen_card = st.selectbox(
    "Pick a card from this deck's mainboard", card_names, key=f"landprob_{deck_id}_card_pick"
)
card_qty = int(library_df.loc[library_df["name"] == chosen_card, "quantity"].sum())
st.caption(f"{card_qty} cop{'y' if card_qty == 1 else 'ies'} of **{chosen_card}** in the {library_size}-card library.")

card_rows = []
for turn in range(0, 6):
    seen = prob.cards_seen_by_turn(turn, on_the_play)
    p = prob.prob_at_least(library_size, card_qty, seen, 1)
    card_rows.append({
        "When": "Opening hand" if turn == 0 else f"Turn {turn}",
        "Cards seen": seen,
        f"P(≥1 copy)": f"{p * 100:.1f}%",
    })
st.dataframe(pd.DataFrame(card_rows).set_index("When"), use_container_width=True)
