"""
Tag Editor page — writes directly to card_tags / deck_tags instead of
going through the CSV migration.

Card Tags tab: pick a tag type (e.g. 'strategy', or create a new one),
bulk-edit every mainboard card's tags for that type in one table, save.

Deck Tags tab: add/remove deck-level tags (e.g. Rule 0 house rules) with
optional notes.

Migration is one-way (CSV -> DB): re-running scripts/migrate.py wipes and
rebuilds the whole database from the CSVs, including everything tagged
here. Use the CSVs + migrate.py for your initial import, then manage tags
here from then on — see the README for the full rationale.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from dashboard_lib import db, loaders, writes

st.set_page_config(page_title="Tag Editor · MTG Dashboard", page_icon="🏷️", layout="wide")

db.require_db()
conn = db.get_connection()

st.title("🏷️ Tag Editor")
st.caption(
    "Writes straight to the database — no CSV re-migration needed. Edits here survive "
    "re-running migrate.py (it backs up and restores dashboard-made tags automatically)."
)

st.sidebar.header("Deck")
db.refresh_data_button()
include_retired = st.sidebar.toggle("Show retired decks", value=True, key="tageditor_include_retired")

decks_df = loaders.load_decks_df(conn, include_retired=include_retired)
if decks_df.empty:
    st.info("No decks found.")
    st.stop()

deck_labels = [
    row["name"] + ("  (retired)" if not row["is_active"] else "")
    for _, row in decks_df.iterrows()
]
label_to_id = dict(zip(deck_labels, decks_df["deck_id"]))
chosen_label = st.sidebar.selectbox("Choose a deck", deck_labels, key="tageditor_deck_select")
deck_id = int(label_to_id[chosen_label])

meta = loaders.load_deck_meta(conn, deck_id)
st.header(meta.get("representative") or meta.get("name") or "Deck")

tab_cards, tab_deck = st.tabs(["Card Tags", "Deck Tags"])

# ------------------------------------------------------------------
# Card Tags
# ------------------------------------------------------------------
with tab_cards:
    library_df = loaders.load_deck_cards_df(conn, deck_id)

    if library_df.empty:
        st.info("No mainboard cards loaded for this deck yet.")
    else:
        existing_types = loaders.load_tag_types(conn)
        type_options = sorted(set(existing_types) | {"strategy"}) + ["+ Create new tag type..."]
        type_choice = st.selectbox("Tag type to edit", type_options, key=f"tageditor_{deck_id}_cardtype_choice")

        if type_choice == "+ Create new tag type...":
            new_type = st.text_input("New tag type name (e.g. 'rule0')", key=f"tageditor_{deck_id}_cardtype_new")
            tag_type = new_type.strip() or None
        else:
            tag_type = type_choice

        if not tag_type:
            st.info("Enter a tag type name above to start editing.")
        else:
            current_tags = loaders.load_card_tags_by_type(conn, deck_id, tag_type)
            existing_labels = sorted({label for labels in current_tags.values() for label in labels})
            if existing_labels:
                st.caption(f"Existing **{tag_type}** labels in this deck: " + ", ".join(existing_labels))
            else:
                st.caption(f"No **{tag_type}** tags on any card in this deck yet.")

            editor_rows = [
                {
                    "scryfall_id": row["scryfall_id"],
                    "Card": row["name"],
                    "Tags": ", ".join(current_tags.get(row["scryfall_id"], [])),
                }
                for _, row in library_df.iterrows()
            ]
            editor_df = pd.DataFrame(editor_rows)

            edited = st.data_editor(
                editor_df,
                column_config={
                    "Card": st.column_config.TextColumn("Card", disabled=True),
                    "Tags": st.column_config.TextColumn(f"{tag_type} tags (comma-separated)"),
                },
                column_order=["Card", "Tags"],
                hide_index=True,
                use_container_width=True,
                key=f"tageditor_{deck_id}_{tag_type}_editor",
            )

            if st.button("💾 Save tag changes", key=f"tageditor_{deck_id}_{tag_type}_save"):
                edits = {}
                for _, row in edited.iterrows():
                    raw = row["Tags"]
                    labels = [] if pd.isna(raw) else [x.strip() for x in str(raw).split(",")]
                    edits[row["scryfall_id"]] = labels
                changed = writes.bulk_set_card_tags(conn, deck_id, tag_type, edits)
                loaders.invalidate_deck_caches(deck_id)
                st.success(f"Updated {tag_type} tags on {changed} card(s).")
                st.rerun()

# ------------------------------------------------------------------
# Deck Tags
# ------------------------------------------------------------------
with tab_deck:
    current_deck_tags = loaders.load_deck_tags(conn, deck_id)

    if current_deck_tags:
        st.dataframe(
            pd.DataFrame(current_deck_tags)[["tag_type", "label", "notes"]],
            hide_index=True, use_container_width=True,
        )
    else:
        st.caption("No deck-level tags yet.")

    st.markdown("**Remove a tag**")
    if current_deck_tags:
        remove_options = {f"{t['tag_type']}: {t['label']}": t["tag_id"] for t in current_deck_tags}
        remove_choice = st.selectbox(
            "Pick a tag to remove", list(remove_options.keys()), key=f"tageditor_{deck_id}_remove_choice"
        )
        if st.button("🗑️ Remove tag", key=f"tageditor_{deck_id}_remove_btn"):
            writes.remove_deck_tag(conn, deck_id, remove_options[remove_choice])
            loaders.invalidate_deck_caches(deck_id)
            st.success(f"Removed '{remove_choice}'.")
            st.rerun()
    else:
        st.caption("Nothing to remove yet.")

    st.divider()
    st.markdown("**Add a tag**")
    existing_deck_types = loaders.load_tag_types(conn)
    add_type_options = sorted(set(existing_deck_types) | {"rule0"}) + ["+ Create new tag type..."]
    add_type_choice = st.selectbox("Tag type", add_type_options, key=f"tageditor_{deck_id}_add_type_choice")

    if add_type_choice == "+ Create new tag type...":
        new_add_type = st.text_input("New tag type name", key=f"tageditor_{deck_id}_add_newtype")
        add_tag_type = new_add_type.strip() or None
    else:
        add_tag_type = add_type_choice

    add_label = st.text_input("Label", key=f"tageditor_{deck_id}_add_label")
    add_notes = st.text_input("Notes (optional)", key=f"tageditor_{deck_id}_add_notes")

    if st.button("➕ Add tag", key=f"tageditor_{deck_id}_add_btn"):
        if not add_tag_type or not add_label.strip():
            st.warning("Enter both a tag type and a label.")
        else:
            writes.add_deck_tag(conn, deck_id, add_tag_type, add_label.strip(), add_notes.strip() or None)
            loaders.invalidate_deck_caches(deck_id)
            st.success(f"Added '{add_tag_type}: {add_label.strip()}'.")
            st.rerun()
