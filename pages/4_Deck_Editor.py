"""
Deck Editor page — add, rename, and remove deck-level things directly in
the database: deck names/metadata, whole new decks, themes, card tags,
mainboard and maybeboard cards (including brand-new cards, via a live
Scryfall lookup when needed), and planned card swaps. No CSV editing or
re-migration required for any of this.

Card Tags (bulk-editing any tag_type across a deck's mainboard) lives
here too, moved from the earlier standalone Tag Editor page. Deck-level
tags were removed entirely — use description/combos/tutors instead.

Collection/inventory management lives in Collection Editor, and
whole-database reference data (Game Changers, Mana Tags, Card Data
refresh) lives in Card Database — both split out from what used to be a
single combined Editor page.

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

from dashboard_lib import db, loaders, writes, card_resolver, formatting as fmt, queries as q
from dashboard_lib import card_view as cv

DECK_COVER_DIR = os.path.join(db.BASE_DIR, "image_cache", "deck_covers")

cv.setup_page("Deck Editor · MTG Dashboard", "✏️")

db.require_db()
conn = db.get_connection()

st.title("✏️ Deck Editor")
st.caption("Writes straight to the database — no CSV editing or re-migration needed for any of this.")

st.sidebar.header("Deck")
db.refresh_data_button()
decks_df = loaders.load_decks_df(conn)
deck_id = cv.render_deck_picker(
    decks_df, key="editor_deck_select", empty_message="No decks yet — create one below."
)

with st.sidebar.expander("➕ Create a new deck"):
    cn_name = st.text_input("Deck name (required)", key="editor_newdeck_name")
    cn_commander = st.text_input("Commander", key="editor_newdeck_commander")
    cn_partner = st.text_input("Partner", key="editor_newdeck_partner")
    cn_colors = st.multiselect("Color identity", ["W", "U", "B", "R", "G"], key="editor_newdeck_colors")
    if st.button("Create deck", key="editor_newdeck_create"):
        if not cn_name.strip():
            st.warning("Enter a deck name.")
        else:
            new_id, err = writes.create_deck(
                conn, cn_name.strip(),
                commander=cn_commander.strip() or None,
                partner=cn_partner.strip() or None,
                color_identity="".join(c for c in fmt.WUBRG_ORDER if c in cn_colors),
            )
            if err:
                st.error(err)
            else:
                loaders.invalidate_deck_caches()
                st.success(f"Created '{cn_name.strip()}'.")
                st.session_state["editor_deck_select"] = cn_name.strip()
                st.rerun()

(tab_info, tab_themes, tab_cardtags, tab_main, tab_maybe, tab_swap) = st.tabs(
    ["Deck Info", "Themes", "Card Tags", "Mainboard", "Maybeboard", "Swap Manager"]
)

# ------------------------------------------------------------------
# Deck Info
# ------------------------------------------------------------------
with tab_info:
    if deck_id is None:
        st.info("Create a deck in the sidebar to get started.")
    else:
        meta = loaders.load_deck_meta(conn, deck_id)

        st.markdown("#### Rename")
        rn_col1, rn_col2 = st.columns([3, 2])
        new_name = rn_col1.text_input("Deck name", value=meta.get("name", ""), key=f"editor_{deck_id}_name")
        cascade = rn_col2.checkbox(
            "Also update matching collection Locations", value=True,
            key=f"editor_{deck_id}_cascade",
            help="Renames any collection rows whose Location exactly matched the old deck "
                 "name, so \"sleeved in this deck\" tracking stays correct.",
        )
        if st.button("💾 Save name", key=f"editor_{deck_id}_savename"):
            if new_name.strip() and new_name.strip() != meta.get("name"):
                ok, err = writes.rename_deck(conn, deck_id, new_name.strip(), update_collection_locations=cascade)
                if err:
                    st.error(err)
                else:
                    loaders.invalidate_deck_caches()
                    st.success(f"Renamed to '{new_name.strip()}'.")
                    st.rerun()
            else:
                st.info("No change to save.")
        st.divider()
        st.markdown("#### Deck details")
        commander = st.text_input("Commander", value=meta.get("commander") or "", key=f"editor_{deck_id}_commander")
        partner = st.text_input("Partner", value=meta.get("partner") or "", key=f"editor_{deck_id}_partner")
        representative = st.text_input(
            "Representative (display name)", value=meta.get("representative") or "",
            key=f"editor_{deck_id}_representative",
        )
        current_colors = fmt.deck_color_identity_letters(meta.get("color_identity"))
        colors = st.multiselect(
            "Color identity", ["W", "U", "B", "R", "G"], default=current_colors, key=f"editor_{deck_id}_colors"
        )
        bracket_col, interaction_col = st.columns(2)
        bracket = bracket_col.number_input(
            "Bracket", min_value=0, max_value=5, value=int(meta.get("bracket") or 0), step=1,
            key=f"editor_{deck_id}_bracket",
        )
        interaction = interaction_col.number_input(
            "Interaction", min_value=0, max_value=5, value=int(meta.get("interaction") or 0), step=1,
            key=f"editor_{deck_id}_interaction",
        )
        built = st.text_input(
            "Initially built (YYYY-MM-DD, optional)", value=meta.get("initially_built") or "",
            key=f"editor_{deck_id}_built",
        )
        description = st.text_area("Description", value=meta.get("description") or "", key=f"editor_{deck_id}_description")
        combos = st.text_area("Combos", value=meta.get("combos") or "", key=f"editor_{deck_id}_combos")
        tutors = st.text_area("Tutors", value=meta.get("tutors") or "", key=f"editor_{deck_id}_tutors")

        if st.button("💾 Save deck details", key=f"editor_{deck_id}_savemeta"):
            writes.update_deck_meta(
                conn, deck_id,
                commander=commander.strip() or None,
                partner=partner.strip() or None,
                representative=representative.strip() or None,
                color_identity="".join(c for c in fmt.WUBRG_ORDER if c in colors),
                bracket=bracket or None,
                interaction=interaction or None,
                initially_built=built.strip() or None,
                description=description.strip() or None,
                combos=combos.strip() or None,
                tutors=tutors.strip() or None,
            )
            loaders.invalidate_deck_caches()
            st.success("Saved deck details.")
            st.rerun()

        st.divider()
        st.markdown("#### Cover image")
        st.caption("A custom PNG shown as this deck's thumbnail on the Decks page and the home page's deck tiles.")
        current_cover = fmt.resolve_local_image(meta.get("cover_image_path"))
        cov_col1, cov_col2 = st.columns([1, 3])
        if current_cover:
            cov_col1.image(current_cover, width=120)
        else:
            cov_col1.caption("No cover image set.")
        with cov_col2:
            cover_upload = st.file_uploader(
                "Upload a PNG", type=["png"], key=f"editor_{deck_id}_cover_upload"
            )
            up_col, rm_col = st.columns(2)
            if up_col.button("💾 Save cover image", key=f"editor_{deck_id}_cover_save", disabled=cover_upload is None):
                os.makedirs(DECK_COVER_DIR, exist_ok=True)
                dest_path = os.path.join(DECK_COVER_DIR, f"{deck_id}.png")
                with open(dest_path, "wb") as f:
                    f.write(cover_upload.getbuffer())
                rel_path = os.path.relpath(dest_path, db.BASE_DIR)
                writes.set_deck_cover_image(conn, deck_id, rel_path)
                loaders.invalidate_deck_caches()
                st.success("Cover image saved.")
                st.rerun()
            if rm_col.button("🗑️ Remove cover image", key=f"editor_{deck_id}_cover_remove", disabled=not meta.get("cover_image_path")):
                writes.set_deck_cover_image(conn, deck_id, None)
                loaders.invalidate_deck_caches()
                st.success("Cover image removed.")
                st.rerun()

        st.divider()
        st.markdown("#### Win Conditions / Strengths / Weaknesses")
        st.caption("Up to 3 ranked entries each, as \"Label\" + an optional longer description.")

        rank_kinds = [("win_conditions", "Win Conditions"), ("strengths", "Strengths"), ("weaknesses", "Weaknesses")]
        rank_col1, rank_col2, rank_col3 = st.columns(3)
        rank_containers = {"win_conditions": rank_col1, "strengths": rank_col2, "weaknesses": rank_col3}
        rank_inputs = {}

        for kind, title in rank_kinds:
            container = rank_containers[kind]
            container.markdown(f"**{title}**")
            existing = {r["rank"]: r for r in loaders.load_deck_rank_list(conn, deck_id, kind)}
            entries = []
            for rank in (1, 2, 3):
                row = existing.get(rank, {})
                label = container.text_input(
                    f"{title} {rank} — label", value=row.get("label") or "",
                    key=f"editor_{deck_id}_{kind}_{rank}_label",
                )
                desc = container.text_area(
                    f"{title} {rank} — description", value=row.get("description") or "",
                    key=f"editor_{deck_id}_{kind}_{rank}_desc", height=68,
                )
                entries.append({"label": label, "description": desc})
            rank_inputs[kind] = entries

        if st.button("💾 Save Win Conditions / Strengths / Weaknesses", key=f"editor_{deck_id}_ranks_save"):
            for kind, _ in rank_kinds:
                writes.set_deck_rank_list(conn, deck_id, kind, rank_inputs[kind])
            loaders.invalidate_deck_caches()
            st.success("Saved.")
            st.rerun()

        st.divider()
        st.markdown("#### ⚠️ Delete this deck")
        st.caption(
            "Permanently removes the deck, its mainboard, maybeboard, tags, and themes. "
            "Historical games stay in the game log but are detached from this deck (the "
            "same shape an untracked opponent's deck already has). **This cannot be undone.**"
        )
        del_stats = loaders.load_deck_stats(conn, deck_id)
        del_value = loaders.load_deck_value(conn, deck_id)
        del_mb_count = len(loaders.load_maybeboard_df(conn, deck_id))
        st.caption(
            f"This deck currently has **{del_value.get('total_cards') or 0}** mainboard card(s), "
            f"**{del_mb_count}** maybeboard card(s), and **{del_stats['games_played']}** logged game(s)."
        )
        del_clear_loc = st.checkbox(
            f"Also reassign collection Locations that matched this deck's name back to '{writes.DEFAULT_LOCATION}'",
            value=True, key=f"editor_{deck_id}_delete_clearloc",
            help="Those cards still physically exist — they're just coming out of a deck "
                 "that no longer exists, so they land back in general storage instead of "
                 "losing their Location entirely.",
        )
        del_confirm = st.text_input(
            f"Type the deck's name to confirm — **{meta.get('name')}**",
            key=f"editor_{deck_id}_delete_confirm",
        )
        if st.button("🗑️ Permanently delete this deck", key=f"editor_{deck_id}_delete_btn"):
            if del_confirm.strip() != meta.get("name"):
                st.error("Name didn't match — nothing was deleted.")
            else:
                deleted_name, err = writes.delete_deck(conn, deck_id, clear_collection_locations=del_clear_loc)
                if err:
                    st.error(err)
                else:
                    loaders.invalidate_deck_caches()
                    st.success(f"Deleted '{deleted_name}'.")
                    st.rerun()

# ------------------------------------------------------------------
# Themes
# ------------------------------------------------------------------
with tab_themes:
    if deck_id is None:
        st.info("Create a deck in the sidebar to get started.")
    else:
        current_themes = loaders.load_deck_themes(conn, deck_id)
        if current_themes:
            st.dataframe(pd.DataFrame(current_themes), hide_index=True, use_container_width=True)
        else:
            st.caption("No themes yet.")

        st.markdown("**Remove a theme**")
        if current_themes:
            theme_options = {f"{t['theme']} ({t['role']})": t for t in current_themes}
            theme_choice = st.selectbox(
                "Pick a theme to remove", list(theme_options.keys()), key=f"editor_{deck_id}_theme_remove_choice"
            )
            if st.button("🗑️ Remove theme", key=f"editor_{deck_id}_theme_remove_btn"):
                t = theme_options[theme_choice]
                writes.remove_deck_theme(conn, deck_id, t["theme"], t["role"])
                loaders.invalidate_deck_caches()
                st.success(f"Removed '{theme_choice}'.")
                st.rerun()
        else:
            st.caption("Nothing to remove yet.")

        st.divider()
        st.markdown("**Add a theme**")
        st.caption("Pick from the master theme list, or create a new one on the fly.")
        theme_catalog = loaders.load_theme_catalog(conn)
        theme_pick_options = theme_catalog + ["+ Create new theme..."]
        theme_pick = st.selectbox("Theme", theme_pick_options, key=f"editor_{deck_id}_theme_add_pick")
        if theme_pick == "+ Create new theme...":
            theme_name = st.text_input("New theme name", key=f"editor_{deck_id}_theme_add_name_new")
        else:
            theme_name = theme_pick
        theme_role = st.selectbox("Role", ["main", "sub"], key=f"editor_{deck_id}_theme_add_role")
        if st.button("➕ Add theme", key=f"editor_{deck_id}_theme_add_btn"):
            if not theme_name.strip():
                st.warning("Enter a theme name.")
            else:
                writes.add_theme_to_catalog(conn, theme_name.strip())
                writes.add_deck_theme(conn, deck_id, theme_name.strip(), theme_role)
                loaders.invalidate_deck_caches()
                loaders.invalidate_reference_caches()
                st.success(f"Added '{theme_name.strip()}' ({theme_role}).")
                st.rerun()

        with st.expander("⚙️ Manage the master theme list"):
            st.caption(
                "Adding/removing here only changes what's offered above — it never touches "
                "any deck's existing theme assignments."
            )
            new_catalog_theme = st.text_input("New theme name", key=f"editor_{deck_id}_theme_catalog_add")
            if st.button("➕ Add to master list", key=f"editor_{deck_id}_theme_catalog_add_btn"):
                if new_catalog_theme.strip():
                    writes.add_theme_to_catalog(conn, new_catalog_theme.strip())
                    loaders.invalidate_reference_caches()
                    st.success(f"Added '{new_catalog_theme.strip()}' to the master list.")
                    st.rerun()
                else:
                    st.warning("Enter a theme name.")

            if theme_catalog:
                catalog_remove_choice = st.selectbox(
                    "Remove from master list", theme_catalog, key=f"editor_{deck_id}_theme_catalog_remove"
                )
                if st.button("🗑️ Remove from master list", key=f"editor_{deck_id}_theme_catalog_remove_btn"):
                    writes.remove_theme_from_catalog(conn, catalog_remove_choice)
                    loaders.invalidate_reference_caches()
                    st.success(f"Removed '{catalog_remove_choice}' from the master list.")
                    st.rerun()
            else:
                st.caption("Master theme list is empty.")

# ------------------------------------------------------------------
# Card Tags — bulk-edit a whole deck's mainboard tags for one tag_type
# at a time. Moved here from the earlier standalone Tag Editor page;
# deck-level tags (the old "Deck Tags" tab) were removed entirely — use
# the Deck Info tab's description/combos/tutors fields instead.
# ------------------------------------------------------------------
with tab_cardtags:
    if deck_id is None:
        st.info("Create a deck in the sidebar to get started.")
    else:
        cardtags_library_df = loaders.load_deck_cards_df(conn, deck_id)

        if cardtags_library_df.empty:
            st.info("No mainboard cards loaded for this deck yet.")
        else:
            existing_types = loaders.load_tag_types(conn)
            type_options = sorted(set(existing_types) | {"strategy"}) + ["+ Create new tag type..."]
            type_choice = st.selectbox("Tag type to edit", type_options, key=f"editor_{deck_id}_cardtag_type_choice")

            if type_choice == "+ Create new tag type...":
                new_type = st.text_input("New tag type name (e.g. 'rule0')", key=f"editor_{deck_id}_cardtag_type_new")
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

                cardtags_editor_rows = [
                    {
                        "scryfall_id": row["scryfall_id"],
                        "Card": row["name"],
                        "Tags": ", ".join(current_tags.get(row["scryfall_id"], [])),
                    }
                    for _, row in cardtags_library_df.iterrows()
                ]
                cardtags_editor_df = pd.DataFrame(cardtags_editor_rows)

                cardtags_edited = st.data_editor(
                    cardtags_editor_df,
                    column_config={
                        "Card": st.column_config.TextColumn("Card", disabled=True),
                        "Tags": st.column_config.TextColumn(f"{tag_type} tags (comma-separated)"),
                    },
                    column_order=["Card", "Tags"],
                    hide_index=True,
                    use_container_width=True,
                    key=f"editor_{deck_id}_{tag_type}_cardtags_editor",
                )

                if st.button("💾 Save tag changes", key=f"editor_{deck_id}_{tag_type}_cardtags_save"):
                    cardtags_edits = {}
                    for _, row in cardtags_edited.iterrows():
                        raw = row["Tags"]
                        labels = [] if pd.isna(raw) else [x.strip() for x in str(raw).split(",")]
                        cardtags_edits[row["scryfall_id"]] = labels
                    changed = writes.bulk_set_card_tags(conn, deck_id, tag_type, cardtags_edits)
                    loaders.invalidate_deck_caches()
                    st.success(f"Updated {tag_type} tags on {changed} card(s).")
                    st.rerun()

# ------------------------------------------------------------------
# Mainboard
# ------------------------------------------------------------------
with tab_main:
    if deck_id is None:
        st.info("Create a deck in the sidebar to get started.")
    else:
        st.markdown("**Add a card**")
        st.caption("Set code + collector number picks an exact printing; leave them blank to match by name.")
        ac1, ac2, ac3, ac4 = st.columns([3, 1, 1, 1])
        main_add_name = ac1.text_input("Card name", key=f"editor_{deck_id}_main_add_name")
        main_add_set = ac2.text_input("Set", key=f"editor_{deck_id}_main_add_set")
        main_add_num = ac3.text_input("Number", key=f"editor_{deck_id}_main_add_num")
        main_add_qty = ac4.number_input("Qty", min_value=1, value=1, step=1, key=f"editor_{deck_id}_main_add_qty")
        if st.button("➕ Add to mainboard", key=f"editor_{deck_id}_main_add_btn"):
            if not main_add_name.strip() and not (main_add_set.strip() and main_add_num.strip()):
                st.warning("Enter a card name, or a set code + collector number.")
            else:
                sid, was_new, err = card_resolver.resolve_or_fetch_card(
                    conn, name=main_add_name.strip() or None,
                    set_code=main_add_set.strip() or None, collector_number=main_add_num.strip() or None,
                )
                if err:
                    st.error(err)
                else:
                    writes.add_deck_card(conn, deck_id, sid, quantity=int(main_add_qty))
                    # Deck Building Auto-Add (Prompt Pass 6): if this exact
                    # printing has no collection lot at all yet, give it a
                    # starter one (quantity matching what was just added,
                    # Location defaulted to this deck's name) rather than
                    # letting the deck and the collection quietly drift
                    # apart. A no-op if a lot for this printing already
                    # exists. Deliberately NOT done for maybeboard adds
                    # (see writes.auto_add_to_collection's docstring).
                    deck_name = loaders.load_deck_meta(conn, deck_id).get("name")
                    auto_added_id = writes.auto_add_to_collection(
                        conn, sid, quantity=int(main_add_qty), location=deck_name,
                    )
                    loaders.invalidate_deck_caches()
                    if auto_added_id:
                        loaders.invalidate_collection_caches()
                    note = " (fetched fresh from Scryfall)" if was_new else ""
                    auto_note = (
                        " Also added a starter collection lot for it (wasn't tracked yet)."
                        if auto_added_id else ""
                    )
                    st.success(f"Added {main_add_qty}x {main_add_name.strip() or sid}{note}.{auto_note}")
                    st.rerun()

        st.divider()
        st.markdown("**Edit / remove mainboard cards**")
        main_df = loaders.load_deck_cards_df(conn, deck_id)
        if main_df.empty:
            st.caption("No mainboard cards yet.")
        else:
            main_rows = [
                {"scryfall_id": r["scryfall_id"], "Card": r["name"], "Qty": int(r["quantity"]), "Delete": False}
                for _, r in main_df.iterrows()
            ]
            main_editor_df = pd.DataFrame(main_rows)
            main_edited = st.data_editor(
                main_editor_df,
                column_config={
                    "Card": st.column_config.TextColumn("Card", disabled=True),
                    "Qty": st.column_config.NumberColumn("Qty", min_value=0, step=1),
                    "Delete": st.column_config.CheckboxColumn("Delete"),
                },
                column_order=["Card", "Qty", "Delete"],
                hide_index=True, use_container_width=True,
                key=f"editor_{deck_id}_main_editor",
            )
            if st.button("💾 Save mainboard changes", key=f"editor_{deck_id}_main_save"):
                main_updates = {}
                for _, row in main_edited.iterrows():
                    if row["Delete"]:
                        main_updates[row["scryfall_id"]] = 0
                    else:
                        main_updates[row["scryfall_id"]] = int(row["Qty"]) if pd.notna(row["Qty"]) else 0
                changed = writes.bulk_update_deck_cards(conn, deck_id, main_updates)
                loaders.invalidate_deck_caches()
                st.success(f"Updated {changed} card(s).")
                st.rerun()

# ------------------------------------------------------------------
# Maybeboard
# ------------------------------------------------------------------
with tab_maybe:
    if deck_id is None:
        st.info("Create a deck in the sidebar to get started.")
    else:
        st.markdown("**Add a card**")
        st.caption("Set code + collector number picks an exact printing; leave them blank to match by name.")
        mc1, mc2, mc3 = st.columns([3, 1, 1])
        mb_add_name = mc1.text_input("Card name", key=f"editor_{deck_id}_mb_add_name")
        mb_add_set = mc2.text_input("Set", key=f"editor_{deck_id}_mb_add_set")
        mb_add_num = mc3.text_input("Number", key=f"editor_{deck_id}_mb_add_num")
        mb_add_notes = st.text_input("Notes (optional)", key=f"editor_{deck_id}_mb_add_notes")
        if st.button("➕ Add to maybeboard", key=f"editor_{deck_id}_mb_add_btn"):
            if not mb_add_name.strip() and not (mb_add_set.strip() and mb_add_num.strip()):
                st.warning("Enter a card name, or a set code + collector number.")
            else:
                sid, was_new, err = card_resolver.resolve_or_fetch_card(
                    conn, name=mb_add_name.strip() or None,
                    set_code=mb_add_set.strip() or None, collector_number=mb_add_num.strip() or None,
                )
                if err:
                    st.error(err)
                else:
                    writes.add_maybeboard_card(conn, deck_id, sid, notes=mb_add_notes.strip() or None)
                    loaders.invalidate_deck_caches()
                    note = " (fetched fresh from Scryfall)" if was_new else ""
                    st.success(f"Added {mb_add_name.strip() or sid} to the maybeboard{note}.")
                    st.rerun()

        st.divider()
        st.markdown("**Edit / remove maybeboard cards**")
        mb_df = loaders.load_maybeboard_df(conn, deck_id)
        if mb_df.empty:
            st.caption("Maybeboard is empty.")
        else:
            mb_rows = [
                {
                    "scryfall_id": r["scryfall_id"], "Card": r["name"],
                    "Review?": bool(r["review_flag"]),
                    "Replaces": r["replace_target"] if pd.notna(r.get("replace_target")) else "",
                    "Notes": r["notes"] if pd.notna(r.get("notes")) else "",
                    "Delete": False,
                }
                for _, r in mb_df.iterrows()
            ]
            mb_editor_df = pd.DataFrame(mb_rows)
            mb_edited = st.data_editor(
                mb_editor_df,
                column_config={
                    "Card": st.column_config.TextColumn("Card", disabled=True),
                    "Review?": st.column_config.CheckboxColumn("Review?"),
                    "Replaces": st.column_config.TextColumn("Replaces (free text)"),
                    "Notes": st.column_config.TextColumn("Notes"),
                    "Delete": st.column_config.CheckboxColumn("Delete"),
                },
                column_order=["Card", "Review?", "Replaces", "Notes", "Delete"],
                hide_index=True, use_container_width=True,
                key=f"editor_{deck_id}_mb_editor",
            )
            if st.button("💾 Save maybeboard changes", key=f"editor_{deck_id}_mb_save"):
                mb_updates = {}
                for _, row in mb_edited.iterrows():
                    mb_updates[row["scryfall_id"]] = {
                        "_delete": bool(row["Delete"]),
                        "review_flag": bool(row["Review?"]),
                        "replace_card_name": row["Replaces"] or None,
                        "notes": row["Notes"] or None,
                    }
                changed = writes.bulk_update_maybeboard(conn, deck_id, mb_updates)
                loaders.invalidate_deck_caches()
                st.success(f"Updated {changed} card(s).")
                st.rerun()

# ------------------------------------------------------------------
# Swap Manager (Prompt Pass 13 / prompt6.txt) — queue up planned
# mainboard swaps ("Add Card A -> Replace Card B"), see real-time
# collection availability for each candidate before committing anything,
# then apply the whole queue at once. writes.execute_swap() does the
# actual work per swap: updates deck_cards (remove B, add A), moves any
# collection lot for B that's sleeved in THIS deck back to storage, and
# sleeves A here (reusing an available owned lot, or creating a starter
# one if it isn't owned/available). The queue is persisted in the
# deck_swap_queue table (writes.queue_swap/list_swap_queue/etc.) so it
# survives closing the dashboard between sessions — nothing is written
# to deck_cards/collection until "Confirm & apply" is clicked.
# ------------------------------------------------------------------
with tab_swap:
    if deck_id is None:
        st.info("Create a deck in the sidebar to get started.")
    else:
        swap_deck_name = loaders.load_deck_meta(conn, deck_id).get("name")
        swap_main_df = loaders.load_deck_cards_df(conn, deck_id)
        result_key = f"editor_{deck_id}_swap_last_result"

        if st.session_state.get(result_key):
            for line in st.session_state[result_key]:
                (st.success if line.startswith("✅") else st.error)(line)
            del st.session_state[result_key]

        st.markdown("**Swap Builder**")
        st.caption(
            "Build a list of planned swaps below — nothing is written to the database "
            "until you review and confirm it in Execution Confirmation."
        )
        if swap_main_df.empty:
            st.info("This deck has no mainboard cards yet — add some in the Mainboard tab first.")
        else:
            sw1, sw2, sw3 = st.columns([2, 2, 1])
            swap_add_name = sw1.text_input(
                "Add (card name)", key=f"editor_{deck_id}_swap_add_name",
                help="The exact printing is resolved locally, or fetched fresh from "
                     "Scryfall if needed, when you confirm the swap below.",
            )
            swap_remove_options = {
                f"{r['name']} (x{int(r['quantity'])})": r for _, r in swap_main_df.iterrows()
            }
            swap_remove_label = sw2.selectbox(
                "Replace (current mainboard card)", list(swap_remove_options.keys()),
                key=f"editor_{deck_id}_swap_remove_pick",
            )
            swap_remove_row = swap_remove_options[swap_remove_label]
            swap_qty = sw3.number_input(
                "Qty", min_value=1, value=int(swap_remove_row["quantity"]), step=1,
                key=f"editor_{deck_id}_swap_qty",
            )
            if st.button("➕ Queue this swap", key=f"editor_{deck_id}_swap_queue_btn"):
                if not swap_add_name.strip():
                    st.warning("Enter a card name to add.")
                else:
                    writes.queue_swap(
                        conn, deck_id,
                        add_name=swap_add_name.strip(),
                        remove_scryfall_id=swap_remove_row["scryfall_id"],
                        remove_name=swap_remove_row["name"],
                        quantity=int(swap_qty),
                    )
                    st.rerun()

        st.divider()
        st.markdown("**Queued swaps**")
        swap_queue = writes.list_swap_queue(conn, deck_id)
        if not swap_queue:
            st.caption("Nothing queued yet.")
        else:
            # Inventory Checking: live availability for each candidate
            # "Add" card, recomputed on every render (not cached) so it
            # always reflects the current collection state.
            queue_rows = []
            for item in swap_queue:
                status = q.card_inventory_status(conn, item["add_name"], deck_name=swap_deck_name)
                if status["owned_qty"] == 0:
                    availability = "Not owned — a starter lot will be created"
                elif status["available_lots"]:
                    best = status["available_lots"][0]
                    availability = f"Available in storage ({best['location'] or 'no location set'})"
                elif status["in_other_decks"]:
                    others = ", ".join(f"{d} (x{n})" for d, n in status["in_other_decks"].items())
                    availability = f"Owned, but every copy is already sleeved in: {others}"
                else:
                    availability = "Already sleeved in this deck"
                queue_rows.append({
                    "#": item["queue_id"],
                    "Add": item["add_name"],
                    "Owned": status["owned_qty"],
                    "Availability": availability,
                    "Replace": item["remove_name"],
                    "Qty": item["quantity"],
                    "Remove from queue": False,
                })
            queue_edited = st.data_editor(
                pd.DataFrame(queue_rows),
                column_config={
                    "#": st.column_config.NumberColumn("#", disabled=True, width="small"),
                    "Add": st.column_config.TextColumn("Add", disabled=True),
                    "Owned": st.column_config.NumberColumn("Owned", disabled=True, width="small"),
                    "Availability": st.column_config.TextColumn("Availability", disabled=True),
                    "Replace": st.column_config.TextColumn("Replace", disabled=True),
                    "Qty": st.column_config.NumberColumn("Qty", disabled=True, width="small"),
                    "Remove from queue": st.column_config.CheckboxColumn("Remove from queue"),
                },
                column_order=["Add", "Owned", "Availability", "Replace", "Qty", "Remove from queue"],
                hide_index=True, use_container_width=True,
                key=f"editor_{deck_id}_swap_queue_editor",
            )
            qbtn1, qbtn2 = st.columns(2)
            if qbtn1.button("🗑️ Remove checked from queue", key=f"editor_{deck_id}_swap_queue_remove_btn"):
                remove_ids = [row["#"] for _, row in queue_edited.iterrows() if row["Remove from queue"]]
                writes.remove_swap_queue_items(conn, remove_ids)
                st.rerun()
            if qbtn2.button("🧹 Clear entire queue", key=f"editor_{deck_id}_swap_queue_clear_btn"):
                writes.clear_swap_queue(conn, deck_id)
                st.rerun()

            st.divider()
            st.markdown("**Execution Confirmation**")
            st.caption(
                "Applies every queued swap at once: updates this deck's mainboard, moves "
                "the replaced card's collection lot(s) that are sleeved here back to "
                "storage, and sleeves the added card here (an available owned copy if one "
                "exists, otherwise a brand-new starter lot)."
            )
            if st.button("✅ Confirm & apply all queued swaps", key=f"editor_{deck_id}_swap_execute_btn"):
                results = []
                for item in swap_queue:
                    sid, was_new, err = card_resolver.resolve_or_fetch_card(conn, name=item["add_name"])
                    if err:
                        results.append(f"❌ {item['add_name']}: {err}")
                        continue
                    outcome = writes.execute_swap(
                        conn, deck_id, swap_deck_name,
                        remove_scryfall_id=item["remove_scryfall_id"], remove_card_name=item["remove_name"],
                        add_scryfall_id=sid, add_card_name=item["add_name"], quantity=item["quantity"],
                    )
                    fetch_note = " (fetched fresh from Scryfall)" if was_new else ""
                    if outcome["created_lot"]:
                        lot_note = " — created a new starter collection lot"
                    elif outcome["assigned_lot_id"]:
                        lot_note = " — reassigned an existing owned copy"
                    else:
                        lot_note = ""
                    results.append(
                        f"✅ Added {item['add_name']}{fetch_note}, replaced {item['remove_name']}{lot_note}."
                    )
                writes.clear_swap_queue(conn, deck_id)
                st.session_state[result_key] = results
                loaders.invalidate_deck_caches()
                loaders.invalidate_collection_caches()
                st.rerun()
