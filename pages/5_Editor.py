"""
Editor page — add, rename, and remove things directly in the database:
deck names/metadata, whole new decks, retiring/reactivating a deck,
themes, mainboard and maybeboard cards (including brand-new cards, via a
live Scryfall lookup when needed), and collection lots. No CSV editing or
re-migration required for any of this.

Card tags and deck tags have their own dedicated page (Tag Editor) —
not duplicated here.

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

from dashboard_lib import db, loaders, writes, card_resolver, refresh, formatting as fmt

st.set_page_config(page_title="Editor · MTG Dashboard", page_icon="✏️", layout="wide")

db.require_db()
conn = db.get_connection()

st.title("✏️ Editor")
st.caption(
    "Writes straight to the database — no CSV editing or re-migration needed for any of this. "
    "Tags live on the separate Tag Editor page."
)

st.sidebar.header("Deck")
db.refresh_data_button()
include_retired = st.sidebar.toggle("Show retired decks", value=True, key="editor_include_retired")
decks_df = loaders.load_decks_df(conn, include_retired=include_retired)

deck_id = None
if not decks_df.empty:
    deck_labels = [
        row["name"] + ("  (retired)" if not row["is_active"] else "")
        for _, row in decks_df.iterrows()
    ]
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

tab_info, tab_themes, tab_main, tab_maybe, tab_coll, tab_carddata = st.tabs(
    ["Deck Info", "Themes", "Mainboard", "Maybeboard", "Collection", "Card Data"]
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
        st.caption(
            "This only updates the database, not deck_mapping.csv. If you later re-run the CSV "
            "migration, a rename made here (without a matching CSV update) can cause a duplicate "
            "deck entry."
        )

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
        st.markdown("#### Active / Retired")
        is_active = bool(meta.get("is_active"))
        if is_active:
            st.write("Status: **Active**")
            other_decks = decks_df[decks_df["deck_id"] != deck_id]
            successor_options = ["(none)"] + other_decks["name"].tolist()
            successor_label = st.selectbox(
                "Successor (optional, only used if retiring)", successor_options, key=f"editor_{deck_id}_successor"
            )
            if st.button("🗄️ Retire this deck", key=f"editor_{deck_id}_retire"):
                succ_id = None
                if successor_label != "(none)":
                    succ_id = int(other_decks[other_decks["name"] == successor_label]["deck_id"].iloc[0])
                writes.set_deck_active(conn, deck_id, False, successor_deck_id=succ_id)
                loaders.invalidate_deck_caches()
                st.success("Retired.")
                st.rerun()
        else:
            st.write("Status: **Retired**")
            if st.button("↩️ Reactivate this deck", key=f"editor_{deck_id}_reactivate"):
                writes.set_deck_active(conn, deck_id, True)
                loaders.invalidate_deck_caches()
                st.success("Reactivated.")
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
        theme_name = st.text_input("Theme", key=f"editor_{deck_id}_theme_add_name")
        theme_role = st.selectbox("Role", ["main", "sub"], key=f"editor_{deck_id}_theme_add_role")
        if st.button("➕ Add theme", key=f"editor_{deck_id}_theme_add_btn"):
            if not theme_name.strip():
                st.warning("Enter a theme name.")
            else:
                writes.add_deck_theme(conn, deck_id, theme_name.strip(), theme_role)
                loaders.invalidate_deck_caches()
                st.success(f"Added '{theme_name.strip()}' ({theme_role}).")
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
                    loaders.invalidate_deck_caches()
                    note = " (fetched fresh from Scryfall)" if was_new else ""
                    st.success(f"Added {main_add_qty}x {main_add_name.strip() or sid}{note}.")
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
    st.caption("Set code + collector number picks an exact printing; leave them blank to match by name.")
    cc1, cc2, cc3 = st.columns(3)
    coll_add_name = cc1.text_input("Card name", key="editor_coll_add_name")
    coll_add_set = cc2.text_input("Set code", key="editor_coll_add_set")
    coll_add_num = cc3.text_input("Collector number", key="editor_coll_add_num")

    cc4, cc5, cc6, cc7 = st.columns(4)
    coll_add_qty = cc4.number_input("Quantity", min_value=0, value=1, step=1, key="editor_coll_add_qty")
    coll_add_foil = cc5.checkbox("Foil", key="editor_coll_add_foil")
    coll_add_location = cc6.text_input("Location", key="editor_coll_add_location")
    coll_add_price = cc7.number_input("Price paid", min_value=0.0, value=0.0, step=0.5, key="editor_coll_add_price")

    cc8, cc9 = st.columns(2)
    coll_add_date = cc8.text_input("Date acquired (YYYY-MM-DD, optional)", key="editor_coll_add_date")
    coll_add_source = cc9.text_input("Source (optional)", key="editor_coll_add_source")

    if st.button("➕ Add to collection", key="editor_coll_add_btn"):
        if not coll_add_name.strip() and not (coll_add_set.strip() and coll_add_num.strip()):
            st.warning("Enter a card name, or a set code + collector number.")
        else:
            sid, was_new, err = card_resolver.resolve_or_fetch_card(
                conn, name=coll_add_name.strip() or None,
                set_code=coll_add_set.strip() or None, collector_number=coll_add_num.strip() or None,
            )
            if err:
                st.error(err)
            else:
                writes.add_collection_lot(
                    conn, sid,
                    quantity=int(coll_add_qty) if coll_add_qty else None,
                    foil=coll_add_foil,
                    location=coll_add_location.strip() or None,
                    date_acquired=coll_add_date.strip() or None,
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
                    "Date": r["date_acquired"] if pd.notna(r["date_acquired"]) else "",
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
                    "Date": st.column_config.TextColumn("Acquired (YYYY-MM-DD)"),
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
                    coll_updates[row["collection_id"]] = {
                        "_delete": bool(row["Delete"]),
                        "quantity": int(row["Qty"]) if pd.notna(row["Qty"]) else None,
                        "foil": bool(row["Foil"]),
                        "location": row["Location"] or None,
                        "date_acquired": row["Date"] or None,
                        "price_paid": float(row["Price Paid"]) if pd.notna(row["Price Paid"]) else None,
                        "source": row["Source"] or None,
                    }
                changed = writes.bulk_update_collection(conn, coll_updates)
                loaders.invalidate_collection_caches()
                st.success(f"Updated {changed} lot(s).")
                st.rerun()

# ------------------------------------------------------------------
# Card Data — refresh Reserved List / Game Changer / legality / price
# straight from Scryfall for every card already in the database
# ------------------------------------------------------------------
with tab_carddata:
    st.markdown("**Refresh Scryfall data**")
    st.caption(
        "Re-fetches every card already in your database by its permanent Scryfall ID and "
        "updates its Reserved List flag, Game Changer flag, Commander legality, and current "
        "price. Doesn't touch your collection, decklists, tags, or anything else — safe to "
        "run anytime, as often as you like. Needs network access and the `requests` package."
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

        st.success(f"Checked {summary['checked']} card(s), updated {summary['updated']}.")
        if summary["newly_reserved"]:
            st.info("Newly flagged Reserved List: " + ", ".join(summary["newly_reserved"]))
        if summary["newly_game_changer"]:
            st.info("Newly flagged Game Changer: " + ", ".join(summary["newly_game_changer"]))
        if summary["unresolved"]:
            st.warning(f"Couldn't refresh {len(summary['unresolved'])} card(s): " + ", ".join(summary["unresolved"]))
