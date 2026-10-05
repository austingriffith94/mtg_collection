"""
Deck Editor page — add, rename, and remove deck-level things directly in
the database: deck names/metadata, whole new decks, themes, card tags,
mainboard cards (including brand-new cards, via a live Scryfall lookup
when needed), and the Shortlist — ideas for the deck, the swaps staged from
them, and the history of what was applied. No CSV editing or re-migration
required for any of this.

Card Tags (bulk-editing any tag_type across a deck's mainboard) lives
here too, moved from the earlier standalone Tag Editor page. Deck-level
tags were removed entirely — use description/tutors instead.

The old hand-typed "Combos" field (and its text_area here) was removed
outright — it only ever held a manual guess at what combos existed.
Real combo detection now lives on the Decks page's Commander Spellbook
panel instead (see dashboard_lib/spellbook.py). decks.combos itself is
left in the schema, untouched, per this project's non-destructive column
policy (same treatment as cards.edhrec_salt) — any value entered before
this change isn't lost, there's just no editor for it here anymore.

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

(tab_info, tab_themes, tab_cardtags, tab_main, tab_shortlist) = st.tabs(
    ["Deck Info", "Themes", "Card Tags", "Mainboard", "Shortlist"]
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
        # ------------------------------------------------------------------
        # Dismantle (deck lifecycle, Phase 6)
        #
        # The other path out of a deck, next to Delete below: dismantling
        # keeps the list and the history and feeds the cards somewhere
        # useful, where deleting destroys the record and dumps every lot
        # into "Box". It sits here, beside Delete, because the two are the
        # same decision made differently — and because the Workbench is
        # framed as the global pending queue, not a per-deck operation.
        #
        # Nothing here writes to the mainboard or the collection: it stages
        # planned deck_changes rows, which then preview and apply through
        # the Workbench like every other change.
        # ------------------------------------------------------------------
        st.markdown("#### 🔧 Dismantle this deck")
        st.caption(
            "Takes the deck apart and (optionally) feeds another deck with it. Cards on "
            "both lists move **card-to-card**, skipping the box entirely; the rest goes "
            "back to storage. The deck's list and change history are kept, so you can see "
            "what it was and rebuild it later — unlike Delete below, which destroys the "
            "record. Everything is staged as **planned** changes and applied from the "
            "Workbench, so nothing moves until you confirm it."
        )

        _dis_targets = {
            r["name"]: int(r["deck_id"])
            for _, r in decks_df.iterrows()
            if int(r["deck_id"]) != deck_id
        }
        _dis_target_name = st.selectbox(
            "Feed into", ["— nothing, return everything to storage —", *_dis_targets],
            key=f"editor_{deck_id}_dismantle_target",
            help="A deck to hand this one's cards to. Only cards the target's list "
                 "actually runs transfer; the rest goes back to the box.",
        )
        _dis_target_id = _dis_targets.get(_dis_target_name)
        _dis_dest = st.selectbox(
            "Everything else goes to", loaders.load_location_options(conn),
            index=(
                loaders.load_location_options(conn).index(writes.DEFAULT_LOCATION)
                if writes.DEFAULT_LOCATION in loaders.load_location_options(conn) else 0
            ),
            key=f"editor_{deck_id}_dismantle_dest",
        )

        _dis = q.deck_dismantle_plan(conn, deck_id, _dis_target_id)
        if _dis is None or not (_dis["direct_transfer"] or _dis["to_box"] or _dis["strays"]):
            st.caption(
                "Nothing is physically sleeved in this deck, so there's nothing to "
                "dismantle. (Set the cards' Location to this deck's name first — the "
                "Workbench's Reconcile tab does it in bulk.)"
            )
        else:
            st.caption(
                f"**{_dis['sleeved_copies']}** card(s) sleeved here · "
                f"{fmt.format_money(_dis['value'])}"
            )

            if _dis["direct_transfer"]:
                with st.expander(
                    f"↔️ Direct transfer to {_dis['target_deck_name']} "
                    f"({len(_dis['direct_transfer'])}) — in both lists",
                    expanded=True,
                ):
                    st.caption("These move straight into the target's sleeves — no stop at the box.")
                    st.dataframe(
                        pd.DataFrame([
                            {"Card": c["card"], "Copies": c["copies"], "Type": c["card_type"]}
                            for c in _dis["direct_transfer"]
                        ]),
                        hide_index=True, use_container_width=True,
                    )

            if _dis["to_box"]:
                with st.expander(
                    f"📦 Back to {_dis_dest} ({len(_dis['to_box'])}) — on this deck's list only"
                ):
                    st.dataframe(
                        pd.DataFrame([
                            {"Card": c["card"], "Copies": c["copies"], "Type": c["card_type"]}
                            for c in sorted(_dis["to_box"], key=lambda r: (r["card_type"], r["card"]))
                        ]),
                        hide_index=True, use_container_width=True,
                    )

            if _dis["strays"]:
                with st.expander(
                    f"🧹 Sleeved here but not on the list ({len(_dis['strays'])})"
                ):
                    st.caption(
                        "Leftovers of past swaps — real cards in the sleeves with no list "
                        "entry behind them. They aren't list changes, so they're moved "
                        "directly rather than staged as planned changes."
                    )
                    st.dataframe(
                        pd.DataFrame([
                            {"Card": c["card"], "Copies": c["copies"]} for c in _dis["strays"]
                        ]),
                        hide_index=True, use_container_width=True,
                    )

            if _dis["target_needs"]:
                with st.expander(
                    f"🔎 {_dis['target_deck_name']} would still need "
                    f"({len(_dis['target_needs'])})"
                ):
                    st.caption(
                        "Not covered by this dismantle — resolve from the box, another "
                        "deck, or the buy list."
                    )
                    st.dataframe(
                        pd.DataFrame([
                            {"Card": n["card"], "Short": n["short"]}
                            for n in _dis["target_needs"]
                        ]),
                        hide_index=True, use_container_width=True,
                    )

            _dis_move_strays = st.checkbox(
                f"Also move the {len(_dis['strays'])} stray lot(s) to {_dis_dest}",
                value=True, key=f"editor_{deck_id}_dismantle_strays",
                disabled=not _dis["strays"],
            )
            _dis_mark = st.checkbox(
                "Mark this deck as dismantled once the changes are applied",
                value=True, key=f"editor_{deck_id}_dismantle_mark",
                help="Keeps the list and history but labels the deck as taken apart. "
                     "Clear it later from the deck's Build Plan.",
            )
            if st.button(
                "📋 Stage as planned changes", key=f"editor_{deck_id}_dismantle_stage",
                type="primary",
            ):
                staged = writes.stage_dismantle(
                    conn, deck_id, target_deck_id=_dis_target_id, dest_location=_dis_dest,
                )
                if _dis_move_strays and _dis["strays"]:
                    moved = writes.move_dismantle_strays(conn, deck_id, dest_location=_dis_dest)
                    st.info(f"Moved {moved} stray lot(s) to {_dis_dest}.")
                if _dis_mark:
                    writes.set_build_state(conn, deck_id, "dismantled")
                loaders.invalidate_deck_caches()
                loaders.invalidate_collection_caches()
                if staged["removed"] or staged["added"]:
                    st.success(
                        f"Staged {len(staged['removed'])} removal(s) and "
                        f"{len(staged['added'])} addition(s) as planned changes. "
                        "Review and apply them on the **Workbench**."
                    )
                else:
                    st.warning("Nothing was staged.")
                for card, reason in staged["skipped"]:
                    st.caption(f"Skipped **{card}** — {reason}.")

        if meta.get("build_state") == "dismantled":
            st.info(
                "This deck is marked as **dismantled**. Its list and history are intact.",
                icon="🔧",
            )
            if st.button("↩️ No longer dismantled", key=f"editor_{deck_id}_undismantle"):
                writes.set_build_state(conn, deck_id, None)
                loaders.invalidate_deck_caches(deck_id)
                st.rerun()

        st.divider()
        st.markdown("#### ⚠️ Delete this deck")
        st.caption(
            "Permanently removes the deck, its mainboard, shortlist and change history, tags, and themes. "
            "Historical games stay in the game log but are detached from this deck (the "
            "same shape an untracked opponent's deck already has). **This cannot be undone.**"
        )
        del_stats = loaders.load_deck_stats(conn, deck_id)
        del_value = loaders.load_deck_value(conn, deck_id)
        del_sl_count = len(loaders.load_deck_changes_df(conn, deck_id, ("idea", "planned")))
        st.caption(
            f"This deck currently has **{del_value.get('total_cards') or 0}** mainboard card(s), "
            f"**{del_sl_count}** shortlist row(s) (ideas and planned changes), and "
            f"**{del_stats['games_played']}** logged game(s)."
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
                    # exists. Deliberately NOT done for shortlist adds
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
# Shortlist (Workbench rework, Phase 1) — replaces the separate Maybeboard
# and Swap Manager tabs, which were two halves of one workflow: a card
# you're considering, and the swap that would bring it in. Backed by the
# deck_changes table (schema.sql), one row moving through
#
#   idea  ->  planned  ->  applied          (or dropped, from idea/planned)
#
# so promoting an idea to a staged swap is one click instead of retyping
# the card name into a second tab, and applying it leaves a history row
# instead of vanishing. Nothing touches deck_cards/collection until a
# planned row is applied (writes.apply_change -> execute_swap).
# ------------------------------------------------------------------
NO_TARGET = "— none yet —"


def _clean_text(value):
    """data_editor hands back None/NaN for an emptied text cell."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _availability_text(availability, change_id):
    """The Owned / Availability column: queries.plan_availability()'s
    verdict for one row, prefixed with an icon so the state reads at a
    glance.

    This replaced a per-row card_inventory_status() call (Phase 2). The
    difference matters within one deck too: two ideas here both wanting
    your single Blood Crypt used to be told "in storage" twice. The
    allocation is over THIS deck's open rows only — cross-deck collisions
    are the Workbench page's view, over the same function."""
    verdict = availability.get(change_id, {})
    icon = {
        "available": "✅", "claimed_by": "⚠️", "in_other_deck": "📦",
        "not_owned": "🛒", "already_here": "↩️", "no_add": "—",
    }.get(verdict.get("verdict"), "")
    return f"{icon} {verdict.get('text', '')}".strip()


def _replace_options(main_df):
    """The 'Replaces' dropdown: every mainboard card, as
    {label: (scryfall_id, name)} plus reverse lookups. Labels carry the
    quantity, and the printing too when two rows share a name (basic lands
    in several printings), so every label is unique and maps to one row."""
    by_label, by_sid, by_name = {}, {}, {}
    if main_df.empty:
        return by_label, by_sid, by_name
    counts = main_df["name"].str.lower().value_counts()
    for _, r in main_df.sort_values("name", key=lambda c: c.str.lower()).iterrows():
        label = f"{r['name']} (x{int(r['quantity'])})"
        if counts[r["name"].lower()] > 1:
            label = f"{r['name']} [{str(r['set_code']).upper()} {r['collector_number']}] (x{int(r['quantity'])})"
        by_label[label] = (r["scryfall_id"], r["name"])
        by_sid[r["scryfall_id"]] = label
        by_name.setdefault(r["name"].lower(), label)
    return by_label, by_sid, by_name


def _initial_replace_label(row, by_sid, by_name):
    """What an idea's Replaces cell should show: its linked card if that card
    is still in the mainboard, else a match on the stored text, else none."""
    sid = row["remove_scryfall_id"]
    if pd.notna(sid) and sid in by_sid:
        return by_sid[sid]
    name = row["remove_card"]
    if isinstance(name, str) and name.lower() in by_name:
        return by_name[name.lower()]
    return NO_TARGET


def _apply_flash(results):
    """apply_changes() results -> the ✅/❌ lines shown after the rerun."""
    lines = []
    for r in results:
        if r["ok"]:
            outcome = r["outcome"] or {}
            fetch = " (fetched fresh from Scryfall)" if r["was_new"] else ""
            if outcome.get("created_lot"):
                lot = " — created a new starter collection lot"
            elif outcome.get("assigned_lot_id"):
                lot = " — reassigned an existing owned copy"
            else:
                lot = ""
            lines.append(f"✅ Added {r['add_name']}{fetch}, replaced {r['remove_name']}{lot}.")
        else:
            lines.append(f"❌ {r['add_name']}: {r['error']}")
    return lines


with tab_shortlist:
    if deck_id is None:
        st.info("Create a deck in the sidebar to get started.")
    else:
        deck_name = loaders.load_deck_meta(conn, deck_id).get("name")
        main_df = loaders.load_deck_cards_df(conn, deck_id)
        # One allocation pass over the deck's ideas AND planned rows
        # together, so the Ideas and Planned tables below can't each claim
        # the same physical copy. Not cached: it must reflect collection
        # writes made earlier in this same page run.
        deck_open_df = q.open_changes_dataframe(conn, ("planned", "idea"), deck_id=deck_id)
        availability = q.plan_availability(conn, deck_open_df)
        replace_map, label_by_sid, label_by_name = _replace_options(main_df)
        replace_choices = [NO_TARGET] + list(replace_map)

        # Part of every data_editor key below and bumped after each write.
        # A data_editor keeps its pending edits by ROW POSITION under a fixed
        # key, so once a promoted/dropped row leaves the table, the next
        # render would replay those edits onto whichever row slid into its
        # slot. A fresh key per write starts it clean.
        ver_key = f"editor_{deck_id}_shortlist_ver"
        ver = st.session_state.get(ver_key, 0)
        result_key = f"editor_{deck_id}_shortlist_result"

        def _refresh(collection=False):
            st.session_state[ver_key] = ver + 1
            loaders.invalidate_deck_caches()
            if collection:
                loaders.invalidate_collection_caches()
            st.rerun()

        if st.session_state.get(result_key):
            for line in st.session_state[result_key]:
                (st.success if line.startswith("✅") else st.error)(line)
            del st.session_state[result_key]

        st.caption(
            "A card moves **idea → planned → applied**, or gets **dropped**. Ideas can be cards "
            "you own or don't, and needn't name what they'd replace yet. Nothing touches the "
            "deck or your collection until you apply a planned change."
        )

        # ---------------- Add an idea ----------------
        st.markdown("**Add an idea**")
        st.caption("Set code + collector number picks an exact printing; leave them blank to match by name.")
        with st.form(f"editor_{deck_id}_shortlist_add_form", clear_on_submit=True):
            f1, f2, f3 = st.columns([3, 1, 1])
            sl_name = f1.text_input("Card name", key=f"editor_{deck_id}_sl_add_name")
            sl_set = f2.text_input("Set", key=f"editor_{deck_id}_sl_add_set")
            sl_num = f3.text_input("Number", key=f"editor_{deck_id}_sl_add_num")
            f4, f5 = st.columns([2, 3])
            sl_replace = f4.selectbox("Replaces (optional)", replace_choices, key=f"editor_{deck_id}_sl_add_replace")
            sl_notes = f5.text_input("Notes (optional)", key=f"editor_{deck_id}_sl_add_notes")
            sl_submit = st.form_submit_button("➕ Add to shortlist")
        if sl_submit:
            if not sl_name.strip() and not (sl_set.strip() and sl_num.strip()):
                st.warning("Enter a card name, or a set code + collector number.")
            else:
                sid, was_new, err = card_resolver.resolve_or_fetch_card(
                    conn, name=sl_name.strip() or None,
                    set_code=sl_set.strip() or None, collector_number=sl_num.strip() or None,
                )
                if err:
                    st.error(err)
                else:
                    resolved_name = conn.execute(
                        "SELECT name FROM cards WHERE scryfall_id=?", (sid,)
                    ).fetchone()[0]
                    target = replace_map.get(sl_replace)
                    try:
                        writes.add_change(
                            conn, deck_id, add_scryfall_id=sid, add_name=resolved_name,
                            remove_scryfall_id=target[0] if target else None,
                            remove_name=target[1] if target else None,
                            notes=_clean_text(sl_notes),
                        )
                    except ValueError as exc:
                        st.warning(str(exc))
                    else:
                        fetched = " (fetched fresh from Scryfall)" if was_new else ""
                        st.session_state[result_key] = [f"✅ Added {resolved_name} to the shortlist{fetched}."]
                        _refresh()

        # ---------------- Ideas ----------------
        st.divider()
        ideas_df = loaders.load_deck_changes_df(conn, deck_id, ("idea",))
        st.markdown(f"**Ideas ({len(ideas_df)})**")
        if ideas_df.empty:
            st.caption("No ideas yet.")
        else:
            idea_rows, initial_labels = [], {}
            for _, r in ideas_df.iterrows():
                card = r["add_card"] if isinstance(r["add_card"], str) else None
                label = _initial_replace_label(r, label_by_sid, label_by_name)
                initial_labels[int(r["change_id"])] = label
                idea_rows.append({
                    "change_id": int(r["change_id"]),
                    "Card": card or "(removal only)",
                    "Owned": _availability_text(availability, int(r["change_id"])),
                    "Price": float(r["price"]) if pd.notna(r["price"]) else None,
                    "Replaces": label,
                    "Review?": bool(r["review_flag"]),
                    "Notes": r["notes"] if isinstance(r["notes"], str) else "",
                    "→ Plan": False,
                    "Drop": False,
                })
            ideas_edited = st.data_editor(
                pd.DataFrame(idea_rows),
                column_config={
                    "Card": st.column_config.TextColumn("Card", disabled=True),
                    "Owned": st.column_config.TextColumn("Owned?", disabled=True),
                    "Price": st.column_config.NumberColumn("Price", format="$%.2f", disabled=True, width="small"),
                    "Replaces": st.column_config.SelectboxColumn("Replaces", options=replace_choices, required=True),
                    "Review?": st.column_config.CheckboxColumn("Review?", width="small"),
                    "Notes": st.column_config.TextColumn("Notes"),
                    "→ Plan": st.column_config.CheckboxColumn(
                        "→ Plan", width="small",
                        help="Stage this as a swap. Needs a card in 'Replaces'."),
                    "Drop": st.column_config.CheckboxColumn(
                        "Drop", width="small",
                        help="Reject it. Kept in History rather than deleted."),
                },
                column_order=["Card", "Owned", "Price", "Replaces", "Review?", "Notes", "→ Plan", "Drop"],
                hide_index=True, width="stretch",
                key=f"editor_{deck_id}_sl_ideas_{ver}",
            )
            if st.button("💾 Save changes / run checked actions", key=f"editor_{deck_id}_sl_ideas_save_{ver}"):
                updates, to_plan, to_drop, flash = {}, [], [], []
                for _, row in ideas_edited.iterrows():
                    cid = int(row["change_id"])
                    edit = {"review_flag": bool(row["Review?"]), "notes": _clean_text(row["Notes"])}
                    # Only touch the pairing if the dropdown actually moved. A row
                    # whose stored target isn't in the mainboard any more shows
                    # "none yet", and saving must not wipe that out as a side effect.
                    if row["Replaces"] != initial_labels[cid]:
                        target = replace_map.get(row["Replaces"])
                        edit["remove_scryfall_id"], edit["remove_name"] = target if target else (None, None)
                    updates[cid] = edit
                    if row["Drop"]:
                        to_drop.append((cid, row))
                    elif row["→ Plan"]:
                        to_plan.append((cid, row))
                edited_n = writes.bulk_update_changes(conn, deck_id, updates)
                planned_n = dropped_n = 0
                for cid, row in to_plan:
                    target = replace_map.get(row["Replaces"])
                    if not target:
                        flash.append(f"❌ {row['Card']}: pick a card for it to replace before planning it.")
                        continue
                    try:
                        writes.promote_change(conn, cid, remove_scryfall_id=target[0], remove_name=target[1])
                        planned_n += 1
                    except ValueError as exc:
                        flash.append(f"❌ {row['Card']}: {exc}")
                for cid, row in to_drop:
                    try:
                        writes.drop_change(conn, cid)
                        dropped_n += 1
                    except ValueError as exc:
                        flash.append(f"❌ {row['Card']}: {exc}")
                flash.insert(0, f"✅ Saved {edited_n} edit(s), planned {planned_n}, dropped {dropped_n}.")
                st.session_state[result_key] = flash
                _refresh()

        # ---------------- Planned ----------------
        st.divider()
        planned_df = loaders.load_deck_changes_df(conn, deck_id, ("planned",))
        if not planned_df.empty:
            planned_df = planned_df.sort_values(["planned_at", "change_id"])
        st.markdown(f"**Planned ({len(planned_df)})**")
        if planned_df.empty:
            st.caption("Nothing planned. Tick **→ Plan** on an idea above.")
        else:
            main_qty = dict(zip(main_df["scryfall_id"], main_df["quantity"]))
            planned_rows = []
            net_cards = 0
            for _, r in planned_df.iterrows():
                card = r["add_card"] if isinstance(r["add_card"], str) else None
                cut_qty = main_qty.get(r["remove_scryfall_id"])
                net_cards += int(r["quantity"]) - int(cut_qty or 0)
                planned_rows.append({
                    "change_id": int(r["change_id"]),
                    "Add": card or "",
                    "Availability": _availability_text(availability, int(r["change_id"])),
                    "Replaces": r["remove_card"] if isinstance(r["remove_card"], str) else "",
                    "Qty": int(r["quantity"]),
                    "Check": "" if cut_qty is not None else "⚠ cut card isn't in the mainboard any more",
                    "Apply": False,
                    "← Idea": False,
                    "Drop": False,
                })
            if net_cards:
                st.warning(
                    f"Applying everything planned changes this deck's card count by {net_cards:+d}. "
                    "A swap replaces the cut card's whole row, so cutting a basic that's in the deck "
                    "×5 for one card removes all five — set Qty to match."
                )
            planned_edited = st.data_editor(
                pd.DataFrame(planned_rows),
                column_config={
                    "Add": st.column_config.TextColumn("Add", disabled=True),
                    "Availability": st.column_config.TextColumn("Availability", disabled=True),
                    "Replaces": st.column_config.TextColumn("Replaces", disabled=True),
                    "Qty": st.column_config.NumberColumn("Qty", min_value=1, step=1, width="small"),
                    "Check": st.column_config.TextColumn("Check", disabled=True),
                    "Apply": st.column_config.CheckboxColumn("Apply", width="small"),
                    "← Idea": st.column_config.CheckboxColumn(
                        "← Idea", width="small", help="Un-stage it, keeping what it replaces."),
                    "Drop": st.column_config.CheckboxColumn("Drop", width="small"),
                },
                column_order=["Add", "Availability", "Replaces", "Qty", "Check", "Apply", "← Idea", "Drop"],
                hide_index=True, width="stretch",
                key=f"editor_{deck_id}_sl_planned_{ver}",
            )

            def _save_quantities():
                """Qty edits made in the table, written before any action runs
                so Apply never uses a stale quantity."""
                qty_updates = {
                    int(row["change_id"]): {"quantity": max(1, int(row["Qty"])) if pd.notna(row["Qty"]) else 1}
                    for _, row in planned_edited.iterrows()
                }
                return writes.bulk_update_changes(conn, deck_id, qty_updates)

            def _apply(ids):
                results = writes.apply_changes(
                    conn, ids,
                    resolve_add=lambda name: card_resolver.resolve_or_fetch_card(conn, name=name),
                )
                st.session_state[result_key] = _apply_flash(results)
                _refresh(collection=True)

            pb1, pb2, pb3 = st.columns(3)
            if pb1.button("💾 Save Qty / run checked actions", key=f"editor_{deck_id}_sl_planned_save_{ver}"):
                qty_n = _save_quantities()
                flash, demoted_n, dropped_n = [], 0, 0
                for _, row in planned_edited.iterrows():
                    cid = int(row["change_id"])
                    try:
                        if row["Drop"]:
                            writes.drop_change(conn, cid)
                            dropped_n += 1
                        elif row["← Idea"]:
                            writes.demote_change(conn, cid)
                            demoted_n += 1
                    except ValueError as exc:
                        flash.append(f"❌ {row['Add']}: {exc}")
                flash.insert(0, f"✅ Saved {qty_n} quantity edit(s), moved {demoted_n} back to ideas, dropped {dropped_n}.")
                st.session_state[result_key] = flash
                _refresh()
            if pb2.button("✅ Apply checked", key=f"editor_{deck_id}_sl_planned_apply_checked_{ver}"):
                _save_quantities()
                checked = [int(row["change_id"]) for _, row in planned_edited.iterrows() if row["Apply"]]
                if checked:
                    _apply(checked)
                else:
                    st.warning("Tick **Apply** on at least one row first.")
            if pb3.button("✅ Apply all planned", key=f"editor_{deck_id}_sl_planned_apply_all_{ver}"):
                _save_quantities()
                _apply([int(row["change_id"]) for _, row in planned_edited.iterrows()])

        # ---------------- History ----------------
        history_df = loaders.load_deck_changes_df(conn, deck_id, ("applied", "dropped"))
        with st.expander(f"History ({len(history_df)})"):
            if history_df.empty:
                st.caption("Nothing applied or dropped yet.")
            else:
                st.caption(
                    "Swaps you applied here, cards added or removed on the Mainboard tab, and ideas "
                    "you dropped. Times are UTC."
                )
                hist = history_df.sort_values(["resolved_at", "change_id"], ascending=False)
                st.dataframe(
                    pd.DataFrame({
                        "When (UTC)": hist["resolved_at"],
                        "What": hist["status"].map({"applied": "Applied", "dropped": "Dropped"}),
                        "Added": hist["add_card"],
                        "Removed": hist["remove_card"],
                        "Qty": hist["quantity"],
                        "Notes": hist["notes"],
                    }),
                    hide_index=True, width="stretch",
                )
                dropped_df = hist[hist["status"] == "dropped"]
                if not dropped_df.empty:
                    restore_options = {
                        f"{r['add_card'] or r['remove_card']} (#{int(r['change_id'])})": int(r["change_id"])
                        for _, r in dropped_df.iterrows()
                    }
                    restore_pick = st.selectbox(
                        "Restore a dropped idea", list(restore_options),
                        key=f"editor_{deck_id}_sl_restore_pick_{ver}",
                    )
                    if st.button("↩️ Restore as idea", key=f"editor_{deck_id}_sl_restore_btn_{ver}"):
                        try:
                            writes.reopen_change(conn, restore_options[restore_pick])
                        except ValueError as exc:
                            st.warning(str(exc))
                        else:
                            st.session_state[result_key] = [f"✅ Restored {restore_pick.rsplit(' (#', 1)[0]} as an idea."]
                            _refresh()
