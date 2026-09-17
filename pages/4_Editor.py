"""
Editor page — add, rename, and remove things directly in the database:
deck names/metadata, whole new decks, themes, card tags, mainboard and
maybeboard cards (including brand-new cards, via a live Scryfall lookup
when needed), and collection lots. No CSV editing or re-migration
required for any of this.

Card Tags (bulk-editing any tag_type across a deck's mainboard) lives
here now too, moved from the earlier standalone Tag Editor page. Deck-
level tags were removed entirely — use description/combos/tutors instead.

Migration is one-way (CSV -> DB): re-running scripts/migrate.py wipes and
rebuilds the whole database from the CSVs, discarding anything edited
here since the last migration. Use the CSVs + migrate.py for your initial
import, then manage everything here from then on — see the README.
"""
import sys
import os
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from dashboard_lib import db, loaders, writes, card_resolver, refresh, formatting as fmt, queries as q

DECK_COVER_DIR = os.path.join(db.BASE_DIR, "image_cache", "deck_covers")


def _parse_iso_date(value):
    """'YYYY-MM-DD' (or NaN/None/'') -> datetime.date, else None — for
    seeding st.date_input widgets from whatever's already in the DB."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        return None

st.set_page_config(page_title="Editor · MTG Dashboard", page_icon="✏️", layout="wide")

db.require_db()
conn = db.get_connection()

st.title("✏️ Editor")
st.caption("Writes straight to the database — no CSV editing or re-migration needed for any of this.")

st.sidebar.header("Deck")
db.refresh_data_button()
decks_df = loaders.load_decks_df(conn)

deck_id = None
if not decks_df.empty:
    deck_labels = [row["name"] for _, row in decks_df.iterrows()]
    label_to_id = dict(zip(deck_labels, decks_df["deck_id"]))
    chosen_label = st.sidebar.selectbox("Choose a deck", deck_labels, key="editor_deck_select")
    deck_id = int(label_to_id[chosen_label])
else:
    st.sidebar.info("No decks yet — create one below.")

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

tab_info, tab_themes, tab_cardtags, tab_main, tab_maybe, tab_coll, tab_gc, tab_carddata = st.tabs(
    ["Deck Info", "Themes", "Card Tags", "Mainboard", "Maybeboard", "Collection", "Game Changers", "Card Data"]
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
            "Interaction", min_value=0, max_value=10, value=int(meta.get("interaction") or 0), step=1,
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
            "Also clear collection Locations that matched this deck's name",
            value=True, key=f"editor_{deck_id}_delete_clearloc",
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
# Collection
# ------------------------------------------------------------------
with tab_coll:
    st.markdown("**Add a card to your collection**")
    st.caption(
        "Start typing a card name — matches already in your database appear as "
        "suggestions below (refreshes once you finish typing or press Enter, since "
        "that's as close to live autocomplete as plain Streamlit widgets get). Pick "
        "one to choose the exact printing you own, or leave it on \"use exactly what "
        "I typed\" / fill in Set + Collector Number directly for a brand-new card "
        "(resolved via a live Scryfall lookup when you click Add)."
    )
    coll_add_name = st.text_input("Card name", key="editor_coll_add_name")

    # Prompt Pass 6 — auto-complete dropdown: search the local `cards`
    # table (the dashboard's own "master card registry") for name
    # matches as the user types, then a second selector for the exact
    # printing once a card name is settled on.
    NO_NAME_MATCH = "— Use exactly what I typed above —"
    selected_card_name = coll_add_name.strip() or None
    if coll_add_name.strip():
        name_matches = q.search_card_names(conn, coll_add_name.strip())
        if name_matches:
            match_choice = st.selectbox(
                "Matching cards in your database", name_matches + [NO_NAME_MATCH],
                key="editor_coll_add_match_pick",
            )
            if match_choice != NO_NAME_MATCH:
                selected_card_name = match_choice

    selected_printing = None
    if selected_card_name:
        printings = q.card_printings_by_name(conn, selected_card_name)
        if printings:
            def _printing_label(p):
                return (
                    f"{(p['set_code'] or '?').upper()} #{p['collector_number']} "
                    f"— {fmt.format_money(p['current_price_usd'])}"
                )
            printing_labels = [_printing_label(p) for p in printings]
            printing_choice = st.selectbox(
                "Printing", printing_labels,
                key=f"editor_coll_add_printing_pick_{selected_card_name}",
            )
            selected_printing = printings[printing_labels.index(printing_choice)]
        else:
            st.caption(f"'{selected_card_name}' isn't in your local database under any printing yet.")

        if card_resolver.scryfall_available():
            if st.button("🔍 Check Scryfall for other printings", key="editor_coll_add_more_printings_btn"):
                added, err = card_resolver.fetch_additional_printings(conn, selected_card_name)
                if err:
                    st.error(err)
                elif added:
                    st.success(f"Found {added} new printing(s) for '{selected_card_name}'.")
                    st.rerun()
                else:
                    st.info("No additional printings found beyond what's already in your database.")

    if selected_printing:
        st.caption(
            f"Selected printing: **{selected_printing['set_code'].upper()} "
            f"#{selected_printing['collector_number']}**"
        )
        coll_add_set = ""
        coll_add_num = ""
    else:
        set_col, num_col = st.columns(2)
        coll_add_set = set_col.text_input("Set code (optional — pins an exact printing)", key="editor_coll_add_set")
        coll_add_num = num_col.text_input("Collector number", key="editor_coll_add_num")

    cc4, cc5, cc6, cc7 = st.columns(4)
    coll_add_qty = cc4.number_input("Quantity", min_value=0, value=1, step=1, key="editor_coll_add_qty")
    coll_add_foil = cc5.checkbox("Foil", key="editor_coll_add_foil")
    coll_add_location = cc6.text_input("Location", key="editor_coll_add_location")
    coll_add_price = cc7.number_input("Price paid", min_value=0.0, value=0.0, step=0.5, key="editor_coll_add_price")

    cc8, cc9 = st.columns(2)
    coll_add_date = cc8.date_input(
        "Date acquired", value=datetime.date.today(), key="editor_coll_add_date",
        help="Defaults to today — change it if you're logging a past acquisition.",
    )
    coll_add_source = cc9.text_input("Source (optional)", key="editor_coll_add_source")

    if st.button("➕ Add to collection", key="editor_coll_add_btn"):
        # Prompt Pass 6: a printing chosen from the autocomplete/printing
        # selectors above is used directly (already a known scryfall_id,
        # no need to re-resolve it) — otherwise fall back to the
        # existing name/set/number resolution (local match, else a live
        # Scryfall lookup) exactly as before.
        if selected_printing:
            sid, was_new, err = selected_printing["scryfall_id"], False, None
        elif not selected_card_name and not (coll_add_set.strip() and coll_add_num.strip()):
            st.warning("Enter a card name, or a set code + collector number.")
            sid, was_new, err = None, False, None
        else:
            sid, was_new, err = card_resolver.resolve_or_fetch_card(
                conn, name=selected_card_name,
                set_code=coll_add_set.strip() or None, collector_number=coll_add_num.strip() or None,
            )

        if err:
            st.error(err)
        elif sid:
            writes.add_collection_lot(
                conn, sid,
                quantity=int(coll_add_qty) if coll_add_qty else None,
                foil=coll_add_foil,
                location=coll_add_location.strip() or None,
                date_acquired=coll_add_date.isoformat() if coll_add_date else None,
                price_paid=float(coll_add_price) if coll_add_price else None,
                source=coll_add_source.strip() or None,
            )
            loaders.invalidate_collection_caches()
            note = " (fetched fresh from Scryfall)" if was_new else ""
            st.success(f"Added to collection{note}.")
            st.rerun()

    st.divider()
    st.markdown("**Edit / remove collection lots**")
    st.caption("Search first — editing the whole collection in one table isn't practical at real-world scale.")
    coll_search = st.text_input("Search by card name", key="editor_coll_search")

    if not coll_search:
        st.caption("Type a search above to load matching lots for editing.")
    else:
        coll_df = loaders.load_collection_df(conn)
        scoped = coll_df[coll_df["name"].str.contains(coll_search, case=False, na=False, regex=False)]
        if scoped.empty:
            st.info("No matches.")
        else:
            coll_rows = [
                {
                    "collection_id": r["collection_id"], "Card": r["name"], "Set": r["set_code"],
                    "Qty": int(r["quantity"]) if pd.notna(r["quantity"]) else None,
                    "Foil": bool(r["foil"]),
                    "Location": r["location"] if pd.notna(r["location"]) else "",
                    "Date": _parse_iso_date(r["date_acquired"]),
                    "Price Paid": float(r["price_paid"]) if pd.notna(r["price_paid"]) else None,
                    "Source": r["source"] if pd.notna(r["source"]) else "",
                    "Delete": False,
                }
                for _, r in scoped.iterrows()
            ]
            coll_editor_df = pd.DataFrame(coll_rows)
            coll_edited = st.data_editor(
                coll_editor_df,
                column_config={
                    "Card": st.column_config.TextColumn("Card", disabled=True),
                    "Set": st.column_config.TextColumn("Set", disabled=True),
                    "Qty": st.column_config.NumberColumn("Qty", min_value=0, step=1),
                    "Foil": st.column_config.CheckboxColumn("Foil"),
                    "Location": st.column_config.TextColumn("Location"),
                    "Date": st.column_config.DateColumn("Acquired", format="YYYY-MM-DD"),
                    "Price Paid": st.column_config.NumberColumn("Price Paid", format="$%.2f"),
                    "Source": st.column_config.TextColumn("Source"),
                    "Delete": st.column_config.CheckboxColumn("Delete"),
                },
                column_order=["Card", "Set", "Qty", "Foil", "Location", "Date", "Price Paid", "Source", "Delete"],
                hide_index=True, use_container_width=True,
                key=f"editor_coll_editor_{coll_search}",
            )
            if st.button("💾 Save collection changes", key="editor_coll_save"):
                coll_updates = {}
                for _, row in coll_edited.iterrows():
                    row_date = row["Date"]
                    coll_updates[row["collection_id"]] = {
                        "_delete": bool(row["Delete"]),
                        "quantity": int(row["Qty"]) if pd.notna(row["Qty"]) else None,
                        "foil": bool(row["Foil"]),
                        "location": row["Location"] or None,
                        "date_acquired": row_date.isoformat() if pd.notna(row_date) and row_date else None,
                        "price_paid": float(row["Price Paid"]) if pd.notna(row["Price Paid"]) else None,
                        "source": row["Source"] or None,
                    }
                changed = writes.bulk_update_collection(conn, coll_updates)
                loaders.invalidate_collection_caches()
                st.success(f"Updated {changed} lot(s).")
                st.rerun()

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
