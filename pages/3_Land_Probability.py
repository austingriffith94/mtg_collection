"""
Land Probability page.

For a chosen deck: probability of lands (or any specific card) appearing
in the opening 7, an estimate of hitting your land drop each of the first
5 turns (plus, Prompt Pass 5, the weighted EXPECTED land count by each of
those turns), the probability of having a mana source for each color in
the commander's color identity at the opening hand and each of the first
5 turns, a Phase 3 weighted mana-source-availability graph (turn-by-turn
expected source counts by category), a Phase 3 Commander Cast Probability
engine (turns 1-10), and (Prompt Pass 5) a per-card Cast Probability tool
covering turns 1-8 for any chosen mainboard card.

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
  - Land detection is MDFC-aware (Phase 3): a card with Land on EITHER
    face (e.g. "Instant // Land") counts as a land for every calculation
    on this page, even though it displays under its front face's type
    elsewhere in the dashboard.
  - Color-source counts default to LAND-type cards only, classified by
    the Phase 3 keyword/text rule parser (dashboard_lib.formatting.
    classify_mana_colors) rather than trusting the color_identity field
    alone — this correctly flags "any color" lands like Command Tower,
    City of Brass, and Exotic Orchard even though those cards' own
    color_identity is empty. An opt-in toggle extends the count to all
    card types (mana rocks/dorks) as an approximation.
  - The Commander Cast Probability engine (Section D) compounds the
    land-drop and color-source factors as if independent — see its own
    caption for the caveat this introduces. The per-card tool (Section E,
    Prompt Pass 5) adds a third factor (actually having drawn the card)
    on top of the same two, with the same independence caveat.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from dashboard_lib import db, loaders, formatting as fmt, probability as prob
from dashboard_lib import card_view as cv

st.set_page_config(page_title="Land Probability · MTG Dashboard", page_icon="🎲", layout="wide")
cv.inject_nav_caps_css()

db.require_db()
conn = db.get_connection()

st.title("🎲 Land & Color Probability")

st.sidebar.header("Deck")
db.refresh_data_button()

decks_df = loaders.load_decks_df(conn)
if decks_df.empty:
    st.info("No decks found.")
    st.stop()

deck_labels = [row["name"] for _, row in decks_df.iterrows()]
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
# Phase 3: MDFC-aware — a card with Land on either face counts as a land
# here, via the is_land column card_view.add_derived_columns() computes.
detected_land_count = int(library_df.loc[library_df["is_land"], "quantity"].sum())

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
size_col4.metric("Detected", f"{detected_land_count} lands / {detected_library_size} cards", help="MDFC-aware: a card with Land on either face counts as a land.")

st.divider()

# ------------------------------------------------------------------
# Section A — land drops
# ------------------------------------------------------------------
st.markdown("### Hitting your land drops")
st.caption(
    "Probability of having drawn **at least N lands by turn N** — i.e. enough to have "
    "played a land every turn so far, assuming no mulligans or extra draw. **Expected lands "
    "seen** answers a different question: the probability-weighted EXPECTED total land count "
    "by that point (same linearity-of-expectation math as the weighted mana-source graph "
    "further down) — this can be a fraction, and it's 'how many lands do I expect to have by "
    "now' rather than 'what's the chance I've hit every drop so far'."
)

rows = []
for turn in range(0, 6):
    seen = prob.cards_seen_by_turn(turn, on_the_play)
    needed = 1 if turn == 0 else turn
    label = "Opening hand" if turn == 0 else f"Turn {turn}"
    p = prob.prob_at_least(library_size, land_count, seen, needed)
    expected_lands = prob.expected_count_by_turn(library_size, land_count, turn, on_the_play)
    rows.append({
        "When": label,
        "Cards seen": seen,
        "Lands needed": needed,
        "Probability": f"{p * 100:.1f}%",
        "Expected lands seen": round(expected_lands, 2),
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

source_counts = {}
if not ordered_colors:
    st.info("This commander's color identity is colorless — no colored mana requirements to check.")
else:
    include_nonland = st.toggle(
        "Also count nonland mana rocks/dorks toward color sources",
        value=False,
        key=f"landprob_{deck_id}_nonland_sources",
        help=(
            "Off (default): only Land-type cards count (MDFC lands included). On: also "
            "counts nonland mana rocks/dorks — cards with a verified mana ability of their "
            "own (formatting.is_mana_rock_or_dork), using the same text-based color parser "
            "as lands. This correctly picks up cards like Birds of Paradise ('add one mana "
            "of any color') and Sol Ring ('add {C}{C}'); a same-colored nonland card with no "
            "mana ability of its own (e.g. a plain red creature) never counts just because "
            "it shares a color."
        ),
    )

    source_counts = {
        c: prob.count_color_sources(library_df, c, lands_only=not include_nonland)
        for c in ordered_colors
    }
    st.caption(
        "Detected sources — "
        + ", ".join(f"**{c}**: {source_counts[c]}" for c in ordered_colors)
        + "  (any-color lands like Command Tower count toward every color here)"
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

# Color sources used below in Sections C/D always use the LAND-only,
# text-parser-based counts (the "off" toggle state), regardless of what's
# picked above — keeps the two advanced engines on a consistent, always-
# available basis rather than depending on a UI toggle only shown when
# the commander has a color identity.
land_only_source_counts = {
    c: prob.count_color_sources(library_df, c, lands_only=True) for c in fmt.WUBRG_ORDER
}

st.divider()

# ------------------------------------------------------------------
# Section C — weighted mana-source availability (Phase 3)
# ------------------------------------------------------------------
st.markdown("### Weighted mana-source availability")
st.caption(
    "The probability-weighted **expected number of mana sources** you'll have access to "
    "by each turn, split by category — Mana Rocks/Dorks, Colorless lands, Any-Color lands "
    "(Command Tower, City of Brass, Exotic Orchard, etc.), and each individual color. "
    "This is an expected COUNT (not a 'chance of at least one'), so a dual land contributes "
    "to both of its colors' bars — categories won't sum to the total land count."
)

category_counts = prob.mana_source_category_counts(library_df)
st.caption(
    "Detected totals — "
    + ", ".join(f"**{cat}**: {category_counts[cat]}" for cat in prob.MANA_SOURCE_CATEGORIES if category_counts[cat] > 0)
)

if sum(category_counts.values()) == 0:
    st.info("No mana sources detected in this deck's mainboard yet.")
else:
    weighted_rows = []
    for turn in range(0, 11):
        exp = prob.weighted_mana_expectation(library_size, category_counts, turn, on_the_play)
        row = {"turn": turn}
        row.update(exp)
        weighted_rows.append(row)
    weighted_df = pd.DataFrame(weighted_rows).set_index("turn")
    # Only chart categories that actually have any sources, so an empty
    # color doesn't clutter the stacked bar with a flat zero series.
    nonzero_cols = [c for c in prob.MANA_SOURCE_CATEGORIES if category_counts.get(c, 0) > 0]
    # Prompt Pass 12: explicit per-category colors (fmt.MANA_SOURCE_COLOR_HEX)
    # aligned with the Decks page's WUBRG palette, instead of Streamlit's
    # auto-assigned default categorical colors.
    chart_colors = [fmt.MANA_SOURCE_COLOR_HEX[c] for c in nonzero_cols]
    st.bar_chart(weighted_df[nonzero_cols], color=chart_colors)

    with st.expander("Show as a table"):
        st.dataframe(weighted_df[nonzero_cols].round(2), use_container_width=True)

st.divider()

# ------------------------------------------------------------------
# Section D — Commander Cast Probability engine (Phase 3)
# ------------------------------------------------------------------
st.markdown("### Commander cast probability (Turn 1–10)")
st.caption(
    "Estimated probability of being able to cast the commander **on curve**, assuming "
    "standard 1-land-per-turn drops with no ramp. This compounds two factors: (1) the "
    "cumulative probability of having drawn enough lands for the commander's full mana "
    "value by that turn, and (2) the probability of having drawn enough sources of each "
    "colored mana symbol in its cost by that turn — treated as independent, which is an "
    "optimistic simplification (in reality the same land can't satisfy two color "
    "requirements with one draw). Hybrid/Phyrexian pips are not treated as a hard color "
    "requirement, since they can be paid another way."
)

commander_names_ordered = [n for n in (meta.get("commander"), meta.get("partner")) if n]
if not commander_names_ordered:
    st.info("No commander set for this deck yet (Editor → Deck Info) — nothing to calculate.")
else:
    commander_cards_df = loaders.load_cards_by_name(conn, tuple(commander_names_ordered))
    if commander_cards_df.empty:
        st.info(
            "Couldn't find the commander's own card data in the database yet — try an "
            "Editor → Card Data refresh, or check the commander name matches a card exactly."
        )
    else:
        pick_label = commander_names_ordered[0]
        if len(commander_names_ordered) > 1:
            pick_label = st.radio(
                "Which commander to analyze",
                commander_names_ordered,
                horizontal=True,
                key=f"landprob_{deck_id}_cmdr_pick",
            )
        cmdr_row = commander_cards_df[commander_cards_df["name"].str.lower() == pick_label.lower()]
        if cmdr_row.empty:
            st.info(f"Couldn't find card data for **{pick_label}** — try an Editor → Card Data refresh.")
        else:
            cmdr_row = cmdr_row.iloc[0]
            commander_cmc = cmdr_row.get("cmc") or 0
            colored_pips = prob.parse_colored_pips(cmdr_row.get("mana_cost"))

            pip_desc = ", ".join(f"{c}×{n}" for c, n in colored_pips.items()) or "no colored pips"
            st.caption(
                f"**{pick_label}** — mana value {commander_cmc:.0f}, cost `{cmdr_row.get('mana_cost') or '—'}` ({pip_desc})"
            )

            cast_rows = prob.commander_cast_probability_by_turn(
                library_size=library_size,
                land_count=land_count,
                source_counts=land_only_source_counts,
                commander_cmc=commander_cmc,
                colored_pips=colored_pips,
                on_the_play=on_the_play,
                max_turn=10,
            )
            cast_df = pd.DataFrame(cast_rows)
            chart_df = cast_df.set_index("turn")[["probability"]].rename(columns={"probability": pick_label})
            st.line_chart(chart_df)

            with st.expander("Show factor breakdown"):
                display_df = cast_df.copy()
                display_df["Land factor"] = (display_df["land_factor"] * 100).round(1).astype(str) + "%"
                display_df["Color factor"] = (display_df["color_factor"] * 100).round(1).astype(str) + "%"
                display_df["Combined"] = (display_df["probability"] * 100).round(1).astype(str) + "%"
                display_df = display_df.rename(columns={"turn": "Turn", "cards_seen": "Cards seen"})
                st.dataframe(
                    display_df[["Turn", "Cards seen", "Land factor", "Color factor", "Combined"]].set_index("Turn"),
                    use_container_width=True,
                )

st.divider()

# ------------------------------------------------------------------
# Section E — check a specific card
# ------------------------------------------------------------------
st.markdown("### Check a specific card")
st.caption(
    "Pure lands (basics and other cards whose type line is only Land) are excluded here — "
    "they're already covered by the land-drop math above. MDFC cards with a land face are "
    "still included, since their other, non-land face is still a real card to check for. "
    "This shows the turn-by-turn probability of actually **playing** the chosen card on "
    "curve (drawn it, AND had enough lands, AND had the right colored sources by that turn), "
    "projected through Turn 8."
)
checkable_df = library_df[~library_df["type_line"].fillna("").apply(fmt.is_pure_land)]
card_names = sorted(checkable_df["name"].unique())

if not card_names:
    st.info("No non-land cards to check in this deck's mainboard.")
else:
    chosen_card = st.selectbox(
        "Pick a card from this deck's mainboard", card_names, key=f"landprob_{deck_id}_card_pick"
    )
    card_qty = int(library_df.loc[library_df["name"] == chosen_card, "quantity"].sum())
    card_row = library_df.loc[library_df["name"] == chosen_card].iloc[0]
    card_cmc = card_row.get("cmc") or 0
    card_pips = prob.parse_colored_pips(card_row.get("mana_cost"))
    pip_desc = ", ".join(f"{c}×{n}" for c, n in card_pips.items()) or "no colored pips"
    st.caption(
        f"{card_qty} cop{'y' if card_qty == 1 else 'ies'} of **{chosen_card}** in the "
        f"{library_size}-card library — mana value {card_cmc:.0f}, cost "
        f"`{card_row.get('mana_cost') or '—'}` ({pip_desc})."
    )

    card_cast_rows = prob.card_cast_probability_by_turn(
        library_size=library_size,
        land_count=land_count,
        source_counts=land_only_source_counts,
        card_qty=card_qty,
        card_cmc=card_cmc,
        colored_pips=card_pips,
        on_the_play=on_the_play,
        max_turn=8,
    )
    card_cast_df = pd.DataFrame(card_cast_rows)
    card_cast_df["When"] = card_cast_df["turn"].apply(lambda t: "Opening hand" if t == 0 else f"Turn {t}")

    summary_df = card_cast_df.copy()
    summary_df["P(playable on curve)"] = (summary_df["probability"] * 100).round(1).astype(str) + "%"
    st.dataframe(
        summary_df.set_index("When")[["cards_seen", "P(playable on curve)"]]
        .rename(columns={"cards_seen": "Cards seen"}),
        use_container_width=True,
    )

    with st.expander("Show factor breakdown"):
        breakdown_df = card_cast_df.copy()
        breakdown_df["Drawn"] = (breakdown_df["draw_factor"] * 100).round(1).astype(str) + "%"
        breakdown_df["Land factor"] = (breakdown_df["land_factor"] * 100).round(1).astype(str) + "%"
        breakdown_df["Color factor"] = (breakdown_df["color_factor"] * 100).round(1).astype(str) + "%"
        breakdown_df["Combined"] = (breakdown_df["probability"] * 100).round(1).astype(str) + "%"
        st.dataframe(
            breakdown_df.set_index("When")[["cards_seen", "Drawn", "Land factor", "Color factor", "Combined"]]
            .rename(columns={"cards_seen": "Cards seen"}),
            use_container_width=True,
        )

