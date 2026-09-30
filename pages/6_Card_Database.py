"""
Card Database page — whole-database, card-name-keyed reference data that
isn't scoped to any one deck: Game Changer categories, optimized-mana
tags, and a Scryfall data refresh across every card already tracked.

Split out from the original combined Editor page — deck-level editing
lives in Deck Editor, and collection/inventory management lives in
Collection Editor.

Migration is one-way (CSV -> DB): re-running scripts/migrate.py wipes and
rebuilds the whole database from the CSVs, discarding anything edited
here since the last migration. Use the CSVs + migrate.py for your initial
import, then manage everything here from then on — see the README.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from dashboard_lib import db, loaders, writes, refresh, card_resolver, formatting as fmt, queries as q
from dashboard_lib import card_view as cv

cv.setup_page("Card Database · MTG Dashboard", "🗄️")

db.require_db()
conn = db.get_connection()

st.title("🗄️ Card Database")
st.caption("Writes straight to the database — no CSV editing or re-migration needed for any of this.")

db.refresh_data_button()

tab_gc, tab_manatags, tab_carddata = st.tabs(["Game Changers", "Mana Tags", "Card Data"])

# ------------------------------------------------------------------
# Game Changers — card_name-keyed (NOT deck-scoped), across every card
# in the database that's either Scryfall-flagged or already carries a
# custom category. Categories come from a master catalog (add/remove
# below), same pattern as the Themes catalog above.
# ------------------------------------------------------------------
with tab_gc:
    st.caption(
        "Every card that's either flagged by Scryfall's live Game Changer field, or already "
        "carries one of your custom categories below — across your whole database, not just "
        "one deck. 'Owned' sums collection quantity across all printings of the card; "
        "'In decks' counts how many of your decks currently run it."
    )

    with st.expander("⚙️ Manage the master category list"):
        st.caption(
            "Adding/removing here only changes what's offered below — it never touches any "
            "card's existing category assignments."
        )
        gc_catalog = loaders.load_game_changer_categories(conn)
        new_gc_category = st.text_input("New category name", key="editor_gc_catalog_add")
        if st.button("➕ Add to master list", key="editor_gc_catalog_add_btn"):
            if new_gc_category.strip():
                writes.add_game_changer_category(conn, new_gc_category.strip())
                loaders.invalidate_reference_caches()
                st.success(f"Added '{new_gc_category.strip()}' to the master list.")
                st.rerun()
            else:
                st.warning("Enter a category name.")

        if gc_catalog:
            gc_catalog_remove = st.selectbox(
                "Remove from master list", gc_catalog, key="editor_gc_catalog_remove"
            )
            if st.button("🗑️ Remove from master list", key="editor_gc_catalog_remove_btn"):
                writes.remove_game_changer_category(conn, gc_catalog_remove)
                loaders.invalidate_reference_caches()
                st.success(f"Removed '{gc_catalog_remove}' from the master list.")
                st.rerun()
        else:
            st.caption("Master category list is empty.")

    st.divider()

    gc_df = loaders.load_game_changers_overview(conn)
    if gc_df.empty:
        st.info(
            "No Game Changers found yet — cards only show up here once they're already in "
            "your database (owned, or run in a deck) and either Scryfall-flagged or carrying "
            "one of your custom categories."
        )
    else:
        gc_search = st.text_input("Search by card name", key="editor_gc_search")
        gc_scoped = gc_df[gc_df["name"].str.contains(gc_search, case=False, na=False, regex=False)] if gc_search else gc_df

        gc_rows = [
            {
                "scryfall_id": r["scryfall_id"],
                "image_uri": fmt.resolve_local_image(r["local_image_path"]) or r["image_uri"],
                "Card": r["name"],
                "Scryfall flag": bool(r["scryfall_flag"]),
                "Categories": r["categories"] if pd.notna(r["categories"]) else "",
                "Owned": int(r["owned_qty"]),
                "In decks": int(r["deck_count"]),
            }
            for _, r in gc_scoped.iterrows()
        ]
        gc_editor_df = pd.DataFrame(gc_rows)
        gc_edited = st.data_editor(
            gc_editor_df,
            column_config={
                "image_uri": st.column_config.ImageColumn("Art", width="small"),
                "Card": st.column_config.TextColumn("Card", disabled=True),
                "Scryfall flag": st.column_config.CheckboxColumn("Scryfall flag", disabled=True),
                "Categories": st.column_config.TextColumn("Categories (comma-separated)"),
                "Owned": st.column_config.NumberColumn("Owned", disabled=True),
                "In decks": st.column_config.NumberColumn("In decks", disabled=True),
            },
            column_order=["image_uri", "Card", "Scryfall flag", "Categories", "Owned", "In decks"],
            hide_index=True, use_container_width=True,
            # Keyed by the search text (not a static key) so changing the
            # search gives a genuinely fresh widget instead of Streamlit
            # reapplying this editor's in-progress edits against a
            # different set of rows by row position.
            key=f"editor_gc_editor_{gc_search}",
        )
        if st.button("💾 Save category changes", key="editor_gc_save"):
            gc_edits = {}
            for _, row in gc_edited.iterrows():
                raw = row["Categories"]
                labels = [] if pd.isna(raw) or not str(raw).strip() else [x.strip() for x in str(raw).split(",")]
                gc_edits[row["Card"]] = labels
            changed = writes.bulk_set_game_changer_tags(conn, gc_edits)
            loaders.invalidate_reference_caches()
            st.success(f"Updated categories on {changed} card(s).")
            st.rerun()

# ------------------------------------------------------------------
# Mana Tags (Prompt Pass 12 / prompt5.txt) — card_name-keyed (NOT
# deck-scoped), across every card in the database, same pattern as Game
# Changers above. Unlike Game Changers, mana_tags has no Scryfall-derived
# flag to seed candidates from (it's 100% hand-curated — Fast, Dual,
# Shockland, Fetch, Ritual, Mana Doubler, Medallion, Moxen, etc.), so a
# dedicated "tag a card" search comes first, letting you assign a tag to
# ANY card in the database (not just ones that already have one); the
# overview table below it only ever shows cards that already carry at
# least one tag, exactly like Game Changers' overview does for its own
# candidate set.
# ------------------------------------------------------------------
with tab_manatags:
    st.caption(
        "Optimized-mana categorization (Fast, Dual, Shockland, Fetch, Ritual, Mana Doubler, "
        "Medallion, Moxen, etc.) — reconstructs the old 'Optimized Mana' footnote, shown live "
        "per deck on the Decks page's 'Optimized mana' panel. Card-name-keyed across your "
        "whole database, not deck-scoped, same as Game Changers."
    )

    with st.expander("⚙️ Manage the master tag list"):
        st.caption(
            "Adding/removing here only changes what's offered below — it never touches any "
            "card's existing tag assignments."
        )
        mt_catalog = loaders.load_mana_tag_catalog(conn)
        new_mt_tag = st.text_input("New tag name", key="editor_mt_catalog_add")
        if st.button("➕ Add to master list", key="editor_mt_catalog_add_btn"):
            if new_mt_tag.strip():
                writes.add_mana_tag_to_catalog(conn, new_mt_tag.strip())
                loaders.invalidate_reference_caches()
                st.success(f"Added '{new_mt_tag.strip()}' to the master list.")
                st.rerun()
            else:
                st.warning("Enter a tag name.")

        if mt_catalog:
            mt_catalog_remove = st.selectbox(
                "Remove from master list", mt_catalog, key="editor_mt_catalog_remove"
            )
            if st.button("🗑️ Remove from master list", key="editor_mt_catalog_remove_btn"):
                writes.remove_mana_tag_from_catalog(conn, mt_catalog_remove)
                loaders.invalidate_reference_caches()
                st.success(f"Removed '{mt_catalog_remove}' from the master list.")
                st.rerun()
        else:
            st.caption("Master tag list is empty.")

    st.divider()

    st.markdown("**Tag a card**")
    st.caption(
        "Search any card in your database and assign its optimized-mana tags — this is the "
        "only way to give a card its FIRST tag, since (unlike Game Changers) there's no "
        "Scryfall flag to seed the list below from."
    )
    mt_search = st.text_input("Search for a card by name", key="editor_mt_search_add")
    mt_matches = q.search_card_names(conn, mt_search) if mt_search.strip() else []
    if mt_search.strip() and not mt_matches:
        st.caption("No cards found matching that name.")
    elif mt_matches:
        mt_pick = st.selectbox("Card", mt_matches, key="editor_mt_pick")
        mt_current_tags = writes.get_mana_tags_map(conn).get(mt_pick, [])
        mt_tags_input = st.text_input(
            "Tags (comma-separated)",
            value=", ".join(mt_current_tags),
            # Keyed by the picked card (not a static key) so switching
            # cards resets this box to THAT card's own saved tags —
            # Streamlit ignores `value=` on rerender for a key it has
            # already instantiated, so a static key would carry over
            # whatever text was typed for the previous card.
            key=f"editor_mt_tags_input_{mt_pick}",
        )
        if st.button("💾 Save tags for this card", key="editor_mt_save_one"):
            labels = [x.strip() for x in mt_tags_input.split(",") if x.strip()]
            writes.set_mana_tags(conn, mt_pick, labels)
            loaders.invalidate_reference_caches()
            st.success(f"Updated mana tags on '{mt_pick}'.")
            st.rerun()

    st.divider()

    mt_df = loaders.load_mana_tags_overview(conn)
    if mt_df.empty:
        st.info(
            "No cards carry an optimized-mana tag yet — use 'Tag a card' above to assign the "
            "first one."
        )
    else:
        mt_bulk_search = st.text_input("Search by card name", key="editor_mt_bulk_search")
        mt_scoped = (
            mt_df[mt_df["name"].str.contains(mt_bulk_search, case=False, na=False, regex=False)]
            if mt_bulk_search else mt_df
        )

        mt_rows = [
            {
                "scryfall_id": r["scryfall_id"],
                "image_uri": fmt.resolve_local_image(r["local_image_path"]) or r["image_uri"],
                "Card": r["name"],
                "Tags": r["tags"] if pd.notna(r["tags"]) else "",
                "Owned": int(r["owned_qty"]),
                "In decks": int(r["deck_count"]),
            }
            for _, r in mt_scoped.iterrows()
        ]
        mt_editor_df = pd.DataFrame(mt_rows)
        mt_edited = st.data_editor(
            mt_editor_df,
            column_config={
                "image_uri": st.column_config.ImageColumn("Art", width="small"),
                "Card": st.column_config.TextColumn("Card", disabled=True),
                "Tags": st.column_config.TextColumn("Tags (comma-separated)"),
                "Owned": st.column_config.NumberColumn("Owned", disabled=True),
                "In decks": st.column_config.NumberColumn("In decks", disabled=True),
            },
            column_order=["image_uri", "Card", "Tags", "Owned", "In decks"],
            hide_index=True, use_container_width=True,
            key=f"editor_mt_editor_{mt_bulk_search}",
        )
        if st.button("💾 Save tag changes", key="editor_mt_save_bulk"):
            mt_edits = {}
            for _, row in mt_edited.iterrows():
                raw = row["Tags"]
                labels = [] if pd.isna(raw) or not str(raw).strip() else [x.strip() for x in str(raw).split(",")]
                mt_edits[row["Card"]] = labels
            changed = writes.bulk_set_mana_tags(conn, mt_edits)
            loaders.invalidate_reference_caches()
            st.success(f"Updated mana tags on {changed} card(s).")
            st.rerun()

# ------------------------------------------------------------------
# Card Data — refresh Reserved List / Game Changer / legality / price
# straight from Scryfall for every card already in the database
# ------------------------------------------------------------------
with tab_carddata:
    st.markdown("**Refresh Scryfall data**")
    st.caption(
        "Re-fetches every card already in your database by its permanent Scryfall ID and "
        "updates its Reserved List flag, Game Changer flag, Showcase/Borderless flags, "
        "Commander legality, and current price. Doesn't touch your decklists or tags — safe "
        "to run anytime, as often as you like. Needs network access and the `requests` "
        "package. Afterward, it also automatically prunes your collection: any lot with no "
        "assigned location, a quantity of 0/none, and not present in any deck's mainboard or "
        "maybeboard is deleted."
    )
    total_cards = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    st.caption(f"{total_cards} card(s) in your database.")

    if not card_resolver.scryfall_available():
        st.warning("The `requests` package isn't installed — run `pip install requests` to enable this.")
    elif st.button("🔄 Refresh now", key="editor_refresh_cards_btn"):
        progress_bar = st.progress(0.0)
        status = st.empty()

        def _progress(done, total, name):
            progress_bar.progress(done / total if total else 1.0)
            status.caption(f"{done}/{total} — {name}")

        summary = refresh.refresh_all_cards(conn, progress_callback=_progress)
        loaders.invalidate_deck_caches()
        loaders.invalidate_collection_caches()
        loaders.invalidate_reference_caches()

        st.success(f"Checked {summary['checked']} card(s), updated {summary['updated']}.")
        if summary["newly_reserved"]:
            st.info("Newly flagged Reserved List: " + ", ".join(summary["newly_reserved"]))
        if summary["newly_game_changer"]:
            st.info("Newly flagged Game Changer: " + ", ".join(summary["newly_game_changer"]))
        if summary["unresolved"]:
            st.warning(f"Couldn't refresh {len(summary['unresolved'])} card(s): " + ", ".join(summary["unresolved"]))
        if summary["pruned_count"]:
            st.info(
                f"Pruned {summary['pruned_count']} collection lot(s) with no location, no "
                f"quantity, and not in any deck's mainboard/maybeboard: "
                + ", ".join(summary["pruned_cards"])
            )
        else:
            st.caption("Nothing to prune from the collection this time.")

    # The "Salt Scores (EDHREC)" sub-editor that used to live here was
    # removed dashboard-wide in Prompt Pass 5: no lightweight public API
    # exposes EDHREC salt scores, and the only alternative data source
    # (MTGJSON's edhrecSaltiness field) only ships inside its full
    # AllPrintings-scale exports — far too heavy an ongoing dependency
    # for one hand-sized field in a personal-scale tool. See
    # PROJECT_STATE.md for the full writeup. cards.edhrec_salt itself is
    # left in the schema untouched (additive policy), so any values
    # entered before this pass aren't lost, there's just no editor for it
    # anymore.
