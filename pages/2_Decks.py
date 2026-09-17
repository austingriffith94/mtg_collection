"""
Decks page (renamed from "Decks & Maybeboard" in Prompt Pass 4 — the
maybeboard is still browsable here via the Board toggle near the bottom,
this is just the section's display name/URL now).

Pick a deck (or land here already-selected, via a `deck_id` query param
set by the home page's deck-tile links) to see its "Turn 0" summary —
commander/partner, colors, bracket, interaction, combos, tutors,
description, when it was built, win/loss record, deck value, and how many
of its cards are physically sleeved in it right now — followed by win
conditions / strengths / weaknesses, then (Prompt Pass 4 reorder)
optimized mana, Game Changers, and the Reserved List right underneath
that, then themes, mana curve, type breakdown, and strategy tag
breakdown, then Top 10 Most Expensive / Top 10 Saltiest.

Below that: the same table/grid card browser as the Collection page, for
the Mainboard, Maybeboard, or both at once.

Proxy flags are intentionally NOT shown here yet — per the project state
doc, the Proxy convention in Location hasn't been confirmed with the user.

Phase 2 additions: an optional custom cover-image thumbnail next to the
deck header (set via the Editor's Deck Info tab), and Top 10 Most
Expensive / Top 10 Saltiest card lists alongside the existing Game
Changers and Reserved List panels. Saltiest is hand-maintained EDHREC
data (Editor -> Card Data -> Salt Scores), not a Scryfall field.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from dashboard_lib import db, loaders, formatting as fmt, moxfield_export
from dashboard_lib import card_view as cv

st.set_page_config(page_title="Decks · MTG Dashboard", page_icon="🃏", layout="wide")

db.require_db()
conn = db.get_connection()

MOXFIELD_EXPORT_DIR = os.path.join(db.BASE_DIR, "moxfield_exports")

st.title("🃏 Decks")

st.sidebar.header("Deck")
db.refresh_data_button()

decks_df = loaders.load_decks_df(conn)
if decks_df.empty:
    st.info("No decks found.")
    st.stop()

deck_labels = [f"{row['name']}" for _, row in decks_df.iterrows()]
label_to_id = dict(zip(deck_labels, decks_df["deck_id"]))

# Honor a `?deck_id=<id>` query param (set by the home page's deck-tile
# links, see dashboard_lib.card_view.render_deck_landing_grid) by
# pre-selecting that deck in the sidebar picker below — but only by
# pre-seeding session_state BEFORE the selectbox is created, since that's
# the only point Streamlit allows setting a widget's value. The param is
# consumed (deleted) immediately after reading so it doesn't keep
# re-forcing this deck back into the picker on a later rerun after the
# user manually picks something else (e.g. by toggling a sidebar filter).
query_deck_id = st.query_params.get("deck_id")
if query_deck_id is not None:
    try:
        target_deck_id = int(query_deck_id)
    except (TypeError, ValueError):
        target_deck_id = None
    if target_deck_id is not None:
        id_to_label = {v: k for k, v in label_to_id.items()}
        target_label = id_to_label.get(target_deck_id)
        if target_label:
            st.session_state["deckpage_deck_select"] = target_label
    del st.query_params["deck_id"]

chosen_label = st.sidebar.selectbox("Choose a deck", deck_labels, key="deckpage_deck_select")
deck_id = int(label_to_id[chosen_label])

meta = loaders.load_deck_meta(conn, deck_id)
stats = loaders.load_deck_stats(conn, deck_id)
value = loaders.load_deck_value(conn, deck_id)
sleeved = loaders.load_in_deck_sleeved_count(conn, meta.get("name"))

# ------------------------------------------------------------------
# Turn 0 panel
# ------------------------------------------------------------------
header_name = meta.get("representative") or meta.get("name") or "Deck"

cover_path = fmt.resolve_local_image(meta.get("cover_image_path"))
if cover_path:
    hcol1, hcol2 = st.columns([1, 5])
    hcol1.image(cover_path, width=120)
    hcol2.header(header_name)
else:
    st.header(header_name)

meta_col1, meta_col2, meta_col3, meta_col4 = st.columns(4)
meta_col1.markdown(f"**Commander**\n\n{meta.get('commander') or '—'}")
meta_col2.markdown(f"**Partner**\n\n{meta.get('partner') or '—'}")
meta_col3.markdown(f"**Colors**\n\n{fmt.deck_color_identity_display(meta.get('color_identity'))}")
meta_col4.markdown(f"**Bracket**\n\n{meta.get('bracket') if meta.get('bracket') is not None else '—'}")

meta_col5, meta_col6, meta_col7, meta_col8 = st.columns(4)
meta_col5.markdown(f"**Interaction**\n\n{meta.get('interaction') if meta.get('interaction') is not None else '—'}")
meta_col6.markdown(f"**Combos**\n\n{meta.get('combos') or '—'}")
meta_col7.markdown(f"**Tutors**\n\n{meta.get('tutors') or '—'}")
meta_col8.markdown(f"**Built**\n\n{meta.get('initially_built') or '—'}")

if meta.get("description"):
    st.caption(meta["description"])

st.divider()

stat_col1, stat_col2, stat_col3, stat_col4, stat_col5 = st.columns(5)
stat_col1.metric("Games played", stats["games_played"])
stat_col2.metric("Wins", stats["wins"])
stat_col3.metric("Losses", stats["losses"])
stat_col4.metric("Win rate", f"{stats['win_rate']*100:.0f}%" if stats["win_rate"] is not None else "—")
deck_size = value.get("total_cards") or 0
sleeved_label = f"{int(sleeved)}/{deck_size}" if sleeved is not None else f"—/{deck_size}"
stat_col5.metric("Sleeved in deck", sleeved_label, help="Tracked collection rows whose Location matches this deck's name, vs. mainboard size.")

st.caption(f"Estimated deck value: {fmt.format_money(value.get('total_value'))}")

with st.expander("📋 Export to Moxfield"):
    export_include_mb = st.checkbox(
        "Include maybeboard (as a separate '// Maybeboard' section)",
        key=f"deck_{deck_id}_moxfield_include_mb",
    )
    moxfield_text = moxfield_export.export_deck_text(conn, deck_id, include_maybeboard=export_include_mb)
    if not moxfield_text:
        st.caption("No mainboard cards to export yet.")
    else:
        st.code(moxfield_text, language=None)
        st.caption("Hover the box above and click the copy icon in the corner to copy it to your clipboard.")

        if st.button("💾 Save to file", key=f"deck_{deck_id}_moxfield_save"):
            os.makedirs(MOXFIELD_EXPORT_DIR, exist_ok=True)
            base_name = fmt.safe_filename(meta.get("name") or "deck")
            suffix = " (with maybeboard)" if export_include_mb else ""
            out_path = os.path.join(MOXFIELD_EXPORT_DIR, f"{base_name}{suffix}.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(moxfield_text + "\n")
            st.success(f"Saved to `{os.path.relpath(out_path, db.BASE_DIR)}`")

st.divider()

# ------------------------------------------------------------------
# Win conditions / strengths / weaknesses
# ------------------------------------------------------------------
wc, st_, wk = st.columns(3)


def _render_rank_list(container, title_text, rows):
    container.markdown(f"**{title_text}**")
    if not rows:
        container.caption("None recorded.")
        return
    for r in rows:
        desc = f" — {r['description']}" if r.get("description") else ""
        container.markdown(f"{r['rank']}. **{r['label']}**{desc}")


_render_rank_list(wc, "Win Conditions", loaders.load_deck_rank_list(conn, deck_id, "win_conditions"))
_render_rank_list(st_, "Strengths", loaders.load_deck_rank_list(conn, deck_id, "strengths"))
_render_rank_list(wk, "Weaknesses", loaders.load_deck_rank_list(conn, deck_id, "weaknesses"))

st.divider()

# ------------------------------------------------------------------
# Optimized mana / Game Changers / Reserved List (Prompt Pass 4: moved to
# sit directly under Win Conditions/Strengths/Weaknesses, ahead of
# Themes/Mana Curve below)
# ------------------------------------------------------------------
main_df_full = loaders.load_deck_cards_df(conn, deck_id)

mana_tag_col, gc_col, reserved_col = st.columns(3)

with mana_tag_col:
    st.markdown("**Optimized mana**")
    mana_summary = loaders.load_deck_mana_tag_summary(conn, deck_id)
    if mana_summary:
        st.dataframe(pd.DataFrame(mana_summary), hide_index=True, use_container_width=True)
    else:
        st.caption("No optimized-mana tags matched for this deck.")

with gc_col:
    st.markdown("**Game Changers**", help="Scryfall's live game_changer flag alongside your own category tag — mismatches are worth reviewing.")
    gc_df = loaders.load_deck_game_changers(conn, deck_id)
    if not gc_df.empty:
        st.dataframe(
            # scryfall_flag deliberately omitted (Prompt Pass 4): every
            # row here is already Game-Changer-flagged or custom-tagged,
            # so the raw Scryfall boolean only ever showed a static "1".
            gc_df.rename(columns={"card_name": "Card", "custom_tag": "Your tag", "status": "Status"})[
                ["Card", "Your tag", "Status"]
            ],
            hide_index=True, use_container_width=True,
        )
    else:
        st.caption("No Game Changers in this deck.")

with reserved_col:
    st.markdown("**Reserved List**", help="Pulled live from Scryfall's `reserved` field.")
    reserved_df = loaders.load_deck_reserved_list_cards(conn, deck_id)
    if not reserved_df.empty:
        # Prompt Pass 4: shows current price instead of quantity run.
        st.dataframe(
            reserved_df.rename(columns={"card_name": "Card", "price": "Price"}),
            column_config={"Price": st.column_config.NumberColumn("Price", format="$%.2f")},
            hide_index=True, use_container_width=True,
        )
    else:
        st.caption("No Reserved List cards in this deck.")

st.divider()

# ------------------------------------------------------------------
# Themes / mana curve / type breakdown / strategy tags
# ------------------------------------------------------------------
theme_col, curve_col = st.columns(2)

with theme_col:
    st.markdown("**Themes**")
    themes = loaders.load_deck_themes(conn, deck_id)
    mains = [t["theme"] for t in themes if t["role"] == "main"]
    subs = [t["theme"] for t in themes if t["role"] == "sub"]
    st.markdown(f"- Main: {', '.join(mains) if mains else '—'}")
    st.markdown(f"- Sub: {', '.join(subs) if subs else '—'}")

    st.markdown("**Type breakdown**")
    if not main_df_full.empty:
        type_counts = main_df_full.groupby("card_type")["quantity"].sum().reindex(fmt.ALL_CARD_TYPES).fillna(0)
        st.bar_chart(type_counts)
    else:
        st.caption("No mainboard cards loaded yet.")

with curve_col:
    st.markdown("**Mana curve** (non-land, MDFC-aware)")
    st.caption(
        "Cards with a land on either face (e.g. an MDFC land) are excluded here, same as "
        "the rest of this dashboard's mana-curve and land-ratio math (Land Probability page)."
    )
    if not main_df_full.empty:
        nonland_df = main_df_full[~main_df_full["is_land"]]
    else:
        nonland_df = main_df_full
    if nonland_df.empty:
        st.caption("No non-land cards to chart yet.")
    else:
        breakdown_by = st.radio(
            "Stack by", ["Color", "Type"], horizontal=True, key=f"curve_stackby_{deck_id}"
        )
        chart_colors = None
        if breakdown_by == "Color":
            # Prompt Pass 4: fixed W/U/B/R/G/Multi-color/Colorless
            # grouping+order with custom mana-colored bars, instead of an
            # alphabetical-with-Colorless-last fallback order.
            bucketed = nonland_df.assign(
                mana_color_bucket=nonland_df["color_display"].apply(fmt.mana_curve_color_bucket)
            )
            group_col = "mana_color_bucket"
            pivot = (
                bucketed.groupby(["cmc", group_col])["quantity"]
                .sum()
                .unstack(group_col)
                .fillna(0)
                .sort_index()
            )
            ordered_cols = [c for c in fmt.MANA_CURVE_COLOR_ORDER if c in pivot.columns]
            pivot = pivot[ordered_cols]
            chart_colors = [fmt.MANA_CURVE_COLOR_HEX[c] for c in ordered_cols]
        else:
            group_col = "card_type"
            pivot = (
                nonland_df.groupby(["cmc", group_col])["quantity"]
                .sum()
                .unstack(group_col)
                .fillna(0)
                .sort_index()
            )
            ordered_cols = [c for c in fmt.ALL_CARD_TYPES if c in pivot.columns]
            ordered_cols += [c for c in pivot.columns if c not in ordered_cols]
            pivot = pivot[ordered_cols]
        if chart_colors:
            st.bar_chart(pivot, color=chart_colors)
        else:
            st.bar_chart(pivot)

    st.markdown("**Strategy tag breakdown**")
    tag_counts = loaders.load_deck_strategy_tag_counts(conn, deck_id)
    if not tag_counts.empty:
        st.dataframe(tag_counts, hide_index=True, use_container_width=True)
    else:
        st.caption("No strategy tags recorded for this deck.")

st.divider()

# ------------------------------------------------------------------
# Top 10 Most Expensive
# (The Top 10 Saltiest panel that used to sit alongside this — hand-
# maintained EDHREC salt scores — was removed dashboard-wide in Prompt
# Pass 5; see PROJECT_STATE.md for why.)
# ------------------------------------------------------------------
st.markdown("**Top 10 most expensive cards**", help="By cards.current_price_usd, refreshed via Editor -> Card Data. Single-card (unit) pricing — quantity isn't shown here.")
price_df = loaders.load_deck_price_top10(conn, deck_id)
if not price_df.empty:
    # Prompt Pass 4: quantity column hidden (unit pricing only); a
    # "purchase price / cost paid" column added, sourced from
    # collection.price_paid for the lot(s) sleeved in this deck.
    st.dataframe(
        price_df.rename(columns={
            "card_name": "Card", "price": "Price", "purchase_price": "Purchase price / Cost paid",
        }),
        column_config={
            "Price": st.column_config.NumberColumn("Price", format="$%.2f"),
            "Purchase price / Cost paid": st.column_config.NumberColumn(
                "Purchase price / Cost paid", format="$%.2f",
            ),
        },
        hide_index=True, use_container_width=True,
    )
else:
    st.caption("No priced cards in this deck yet.")

st.divider()

# ------------------------------------------------------------------
# Card browser: Mainboard / Maybeboard / Both
# ------------------------------------------------------------------
st.subheader("Cards")

board_choice = st.radio("Board", ["Mainboard", "Maybeboard", "Both"], horizontal=True, key=f"deck_{deck_id}_board_choice")

maybeboard_df = loaders.load_maybeboard_df(conn, deck_id)

if board_choice == "Mainboard":
    working_df = main_df_full
elif board_choice == "Maybeboard":
    working_df = maybeboard_df
else:
    m = main_df_full.copy()
    m["board"] = "Mainboard"
    b = maybeboard_df.copy()
    b["board"] = "Maybeboard"
    for col in ("review_flag", "replace_target", "notes"):
        if col not in m.columns:
            m[col] = None
    for col in ("quantity", "strategy_tags"):
        if col not in b.columns:
            b[col] = None
    working_df = pd.concat([m, b], ignore_index=True, sort=False)

# Widget keys are namespaced by deck_id + board_choice so switching decks
# or boards never tries to restore a filter/group selection that no longer
# matches the available options for the new dataframe.
scope_key = f"deck_{deck_id}_{board_choice}"

if working_df.empty:
    st.info(f"No cards in the {board_choice.lower()} for this deck.")
else:
    multi_facets = [("color_identity", "Color Identity", "Colorless")]
    if "strategy_tags" in working_df.columns:
        multi_facets.append(("strategy_tags", "Strategy Tag", None))

    single_facets = [("card_type", "Card Type"), ("rarity", "Rarity"), ("set_code", "Set")]
    if "board" in working_df.columns:
        single_facets.append(("board", "Board"))

    bool_toggles = [("is_game_changer", "Game Changer")]
    if "review_flag" in working_df.columns:
        bool_toggles.append(("review_flag", "Flagged for review"))

    filtered = cv.render_filter_panel(
        working_df,
        key_prefix=scope_key,
        container=st.sidebar,
        multi_facets=multi_facets,
        single_facets=single_facets,
        bool_toggles=bool_toggles,
    )

    column_config = cv.base_column_config()
    extra_cols = []
    if "quantity" in working_df.columns:
        column_config["quantity"] = st.column_config.NumberColumn("Qty", width="small")
        extra_cols.append("quantity")
    if "strategy_tags" in working_df.columns:
        column_config["strategy_tags"] = st.column_config.TextColumn("Strategy Tag", width="small")
        extra_cols.append("strategy_tags")
    if "board" in working_df.columns:
        column_config["board"] = st.column_config.TextColumn("Board", width="small")
        extra_cols.append("board")
    if "review_flag" in working_df.columns:
        column_config["review_flag"] = st.column_config.CheckboxColumn("Review?", width="small")
        extra_cols.append("review_flag")
    if "replace_target" in working_df.columns:
        column_config["replace_target"] = st.column_config.TextColumn("Replaces", width="small")
        extra_cols.append("replace_target")
    if "notes" in working_df.columns:
        column_config["notes"] = st.column_config.TextColumn("Notes", width="large")
        extra_cols.append("notes")

    group_options = [("card_type", "Type"), ("color_display", "Color Identity"), ("rarity", "Rarity")]
    if "strategy_tags" in working_df.columns:
        group_options.append(("strategy_tags", "Strategy Tag"))
    if "board" in working_df.columns:
        group_options.append(("board", "Board"))

    cv.render_browser(
        filtered,
        key_prefix=scope_key,
        group_options=group_options,
        column_config=column_config,
        extra_table_cols=extra_cols,
    )
