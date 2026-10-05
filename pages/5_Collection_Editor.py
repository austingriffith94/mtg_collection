"""
Collection Editor page — manage the physical collection directly in the
database: import a marketplace purchase-order CSV and add a single card lot
by hand (with Scryfall lookup for brand-new printings). Editing/removing
existing lots lives on the Collection page, alongside its filters. No CSV editing or re-migration required.

Not deck-scoped — unlike Deck Editor, there's no "choose a deck" sidebar
here, since every action on this page operates on the collection as a
whole (a Location value can still reference a deck by name, but nothing
here requires picking one first).

Split out from the original combined Editor page — deck-level editing
lives in Deck Editor, and whole-database reference data (Game Changers,
Mana Tags, Card Data refresh) lives in Card Database.

Migration is one-way (CSV -> DB): re-running scripts/migrate.py wipes and
rebuilds the whole database from the CSVs, discarding anything edited
here since the last migration. Use the CSVs + migrate.py for your initial
import, then manage everything here from then on — see the README.
"""
import sys
import os
import datetime
import hashlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from dashboard_lib import db, loaders, writes, card_resolver, formatting as fmt, queries as q
from dashboard_lib import card_view as cv, order_import


cv.setup_page("Collection Editor · MTG Dashboard", "📥")

db.require_db()
conn = db.get_connection()

st.title("📥 Collection Editor")
st.caption("Writes straight to the database — no CSV editing or re-migration needed for any of this.")

db.refresh_data_button()

with st.expander("⚙️ Manage the master Location list"):
    st.caption(
        "Standard, non-deck storage locations (e.g. 'Box', 'Lands Box') offered "
        "alongside every tracked deck's own name in the Location dropdowns below. "
        "Adding/removing here only changes what's offered — it never touches any "
        "collection row's existing Location value."
    )
    loc_catalog = loaders.load_location_catalog(conn)
    new_loc_catalog_entry = st.text_input("New location name", key="editor_loc_catalog_add")
    if st.button("➕ Add to master list", key="editor_loc_catalog_add_btn"):
        if new_loc_catalog_entry.strip():
            writes.add_location_to_catalog(conn, new_loc_catalog_entry.strip())
            loaders.invalidate_reference_caches()
            st.success(f"Added '{new_loc_catalog_entry.strip()}' to the master list.")
            st.rerun()
        else:
            st.warning("Enter a location name.")

    if loc_catalog:
        loc_catalog_remove = st.selectbox(
            "Remove from master list", loc_catalog, key="editor_loc_catalog_remove"
        )
        if st.button("🗑️ Remove from master list", key="editor_loc_catalog_remove_btn"):
            writes.remove_location_from_catalog(conn, loc_catalog_remove)
            loaders.invalidate_reference_caches()
            st.success(f"Removed '{loc_catalog_remove}' from the master list.")
            st.rerun()
    else:
        st.caption("Master location list is empty.")

st.markdown("**Import a purchase order (CSV)**")
st.caption(
    "Upload a marketplace order export (e.g. Mana Pool's "
    "'order_<id>_<date>.csv' download) to add every line item at once. "
    "Each row's Set Code + Collector # is looked up against your "
    "database (falling back to a live Scryfall lookup) to resolve the "
    "exact printing and its real name — the card name printed in the "
    "file itself is never trusted for the match, only used as a "
    "fallback label if a row can't be resolved at all."
)
order_file = st.file_uploader("Order CSV", type=["csv"], key="editor_order_import_file")

if order_file is not None:
    order_rows = []
    try:
        # dtype=str: without it, pandas infers numeric dtypes per column,
        # and a Collector # column that's all-digits in a given file
        # would silently lose a leading zero (e.g. "007" -> 7) — see
        # order_import.py's module docstring.
        order_df = pd.read_csv(order_file, dtype=str)
        order_rows = order_import.parse_order_csv(order_df)
    except Exception as e:
        st.error(f"Couldn't read this file: {e}")

    if order_rows:
        # Cache resolved rows in session_state by file content hash so
        # a Streamlit rerun (e.g. from touching the Location dropdown
        # below) doesn't re-hit Scryfall for every row again.
        file_hash = hashlib.md5(order_file.getvalue()).hexdigest()
        cache_key = f"editor_order_import_resolved_{file_hash}"
        if cache_key not in st.session_state:
            with st.spinner("Resolving printings (checking your database, then Scryfall)..."):
                order_import.resolve_rows(conn, order_rows)
                order_import.mark_existing_lots(conn, order_rows)
            st.session_state[cache_key] = order_rows
        order_rows = st.session_state[cache_key]

        singles = [r for r in order_rows if r["is_single"]]
        non_single = [r for r in order_rows if not r["is_single"]]
        resolved = [r for r in singles if r.get("scryfall_id")]
        unresolved = [r for r in singles if not r.get("scryfall_id")]
        already_owned = [r for r in resolved if r["already_in_collection"]]
        all_already_owned = bool(resolved) and len(already_owned) == len(resolved)
        unrecognized_finish = [r for r in singles if not r["finish_recognized"]]
        price_mismatches = [r for r in singles if r["price_mismatch"]]

        summary = f"Found **{len(singles)}** single card line item(s)"
        summary += f", {len(non_single)} non-single row(s) will be skipped." if non_single else "."
        st.write(summary)
        if unresolved:
            st.warning(f"{len(unresolved)} row(s) couldn't be resolved to a printing — see below.")

        def _status(r):
            if not r.get("scryfall_id"):
                return f"❌ {r['error']}"
            bits = ["🆕 New (from Scryfall)" if r.get("was_new") else "✓ Known"]
            if r["already_in_collection"]:
                bits.append("already owned")
            if not r["finish_recognized"]:
                bits.append(f"⚠️ unrecognized Finish '{r['finish_raw']}'")
            if r["price_mismatch"]:
                bits.append("⚠️ price mismatch")
            return " · ".join(bits)

        st.dataframe(
            pd.DataFrame([
                {
                    "Card": r["resolved_name"], "Set": r["set_code"].upper(), "#": r["collector_number"],
                    "Condition": r["condition"], "Finish": r["finish_raw"], "Qty": r["quantity"],
                    "Unit Price": r["unit_price"], "Total Price": r["total_price"],
                    "Seller": r["seller"], "Status": _status(r),
                }
                for r in singles
            ]),
            hide_index=True, use_container_width=True,
        )

        if unrecognized_finish:
            st.warning(
                f"⚠️ {len(unrecognized_finish)} row(s) have a Finish value other than "
                f"exactly 'Foil'/'Non-Foil' (e.g. a foil-etched or alt-finish printing) — "
                f"they've been treated as **non-foil** by default since that can't be "
                f"inferred automatically. Check the Status column above and fix the Foil "
                f"flag afterward in the Collection page's Edit mode if needed."
            )
        if price_mismatches:
            st.warning(
                f"⚠️ {len(price_mismatches)} row(s) have a Total Price that doesn't match "
                f"Quantity × Unit Price — the Unit Price shown is still what gets stored "
                f"as the price paid, but double-check these rows in case the file's "
                f"columns parsed unexpectedly."
            )

        confirm_dupe_key = f"editor_order_import_confirm_dupe_{file_hash}"
        confirm_dupe = True
        if all_already_owned:
            st.warning(
                "⚠️ Every card below is already in your collection (same printing + "
                "foil already has a lot). This looks like it might be a re-upload of "
                "an order you've already imported — importing now will add brand-new "
                "duplicate lots, not update the existing ones."
            )
            confirm_dupe = st.checkbox(
                "I understand — add these as new lots anyway",
                key=confirm_dupe_key,
            )
        elif already_owned:
            st.caption(
                f"{len(already_owned)} of {len(resolved)} row(s) are already in your "
                f"collection (marked 'already owned' above) — importing will still add "
                f"a new lot for them, same as any other row."
            )
        if any(r["csv_name"] != r["resolved_name"] for r in singles if r.get("scryfall_id")):
            st.caption(
                "Note: some resolved names differ from the file's own Card Name text "
                "(e.g. MDFC naming) — the resolved name above is what gets stored."
            )
        if any(r["language"] and r["language"].lower() != "english" for r in singles):
            st.caption(
                "Note: this file has non-English cards — the collection schema "
                "doesn't track Language, so it'll be dropped on import."
            )
        st.caption(
            "Condition and Language are shown above for reference but aren't stored — "
            "the collection schema has no per-lot Condition/Language column today."
        )

        oc1, oc2, oc3 = st.columns(3)
        LOC_OTHER_IMPORT = "+ Other (type a new location below)..."
        order_loc_pick = oc1.selectbox(
            "Location for all imported cards", [""] + loaders.load_location_options(conn) + [LOC_OTHER_IMPORT],
            key="editor_order_import_location_pick",
        )
        if order_loc_pick == LOC_OTHER_IMPORT:
            order_location = st.text_input("New location", key="editor_order_import_location_new")
        else:
            order_location = order_loc_pick
        order_date = oc2.date_input(
            "Date acquired", value=datetime.date.today(), key="editor_order_import_date"
        )
        order_source = oc3.text_input(
            "Source label", value="Mana Pool", key="editor_order_import_source"
        )
        append_seller = st.checkbox(
            "Append each line's seller name to Source (e.g. 'Mana Pool (Tobemi Games)')",
            value=True, key="editor_order_import_append_seller",
        )

        if st.button(
            f"📥 Import {len(singles)} card(s) into collection", key="editor_order_import_btn",
            disabled=all_already_owned and not confirm_dupe,
        ):
            results = order_import.import_rows(
                conn, order_rows,
                location=order_location.strip() or None,
                date_acquired=order_date.isoformat() if order_date else None,
                source_prefix=order_source.strip() or "Mana Pool",
                append_seller=append_seller,
            )
            if order_loc_pick == LOC_OTHER_IMPORT and order_location.strip():
                writes.add_location_to_catalog(conn, order_location.strip())
            loaders.invalidate_collection_caches()
            loaders.invalidate_reference_caches()

            added = [r for r in results if r["status"] in ("added", "added_new_card")]
            fetched = [r for r in results if r["status"] == "added_new_card"]
            errors = [r for r in results if r["status"] == "error"]
            skipped = [r for r in results if r["status"] == "skipped"]

            note = f" ({len(fetched)} fetched fresh from Scryfall)" if fetched else ""
            st.success(f"Imported {len(added)} card(s){note}.")
            if skipped:
                st.info(f"Skipped {len(skipped)} non-single row(s).")
            if errors:
                st.error(f"{len(errors)} row(s) failed:")
                for r in errors:
                    row = r["row"]
                    st.write(f"- {row['resolved_name']} ({row['set_code']}/{row['collector_number']}): {r['message']}")
            del st.session_state[cache_key]
            st.rerun()

st.divider()
st.markdown("**Add a card to your collection**")
st.caption(
    "Start typing a card name — matches already in your database appear as "
    "suggestions below (refreshes once you finish typing or press Enter, since "
    "that's as close to live autocomplete as plain Streamlit widgets get). Pick "
    "one to choose the exact printing you own, or leave it on \"use exactly what "
    "I typed\" / fill in Set + Collector Number directly for a brand-new card "
    "(resolved via a live Scryfall lookup when you click Add)."
)
# After a successful add, clear the per-card fields (name, printing, set/number,
# qty, foil, price) but keep location/date/source — those usually stay the same
# when entering a batch of cards from one purchase or box.
if st.session_state.pop("editor_coll_add_reset", False):
    for _k in ("editor_coll_add_name", "editor_coll_add_match_pick", "editor_coll_add_set",
               "editor_coll_add_num", "editor_coll_add_qty", "editor_coll_add_foil",
               "editor_coll_add_price"):
        st.session_state.pop(_k, None)
    for _k in [k for k in st.session_state if k.startswith("editor_coll_add_printing_pick_")]:
        del st.session_state[_k]
_flash = st.session_state.pop("editor_coll_add_flash", None)  # shown once, gone on the next interaction
if _flash:
    st.success(_flash)

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
LOC_OTHER = "+ Other (type a new location below)..."
loc_pick = cc6.selectbox(
    "Location", [""] + loaders.load_location_options(conn) + [LOC_OTHER],
    key="editor_coll_add_location_pick",
)
coll_add_price = cc7.number_input("Price paid", min_value=0.0, value=0.0, step=0.5, key="editor_coll_add_price")
if loc_pick == LOC_OTHER:
    coll_add_location = st.text_input("New location", key="editor_coll_add_location_new")
else:
    coll_add_location = loc_pick

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
        if loc_pick == LOC_OTHER and coll_add_location.strip():
            writes.add_location_to_catalog(conn, coll_add_location.strip())
        loaders.invalidate_collection_caches()
        loaders.invalidate_reference_caches()
        # st.rerun() would wipe any message shown here, so stash it for the
        # next run, and flag the per-card fields to be cleared (widget keys
        # can only be reset before their widgets are instantiated, i.e. at
        # the top of the form on the next run).
        what = selected_card_name or f"{coll_add_set.strip().upper()} #{coll_add_num.strip()}"
        if selected_printing:
            what += f" ({selected_printing['set_code'].upper()} #{selected_printing['collector_number']})"
        qty_txt = f"{int(coll_add_qty)}× " if coll_add_qty else ""
        foil_txt = " foil" if coll_add_foil else ""
        loc_txt = f" → {coll_add_location.strip()}" if coll_add_location.strip() else ""
        fetched_txt = " (fetched fresh from Scryfall)" if was_new else ""
        st.session_state["editor_coll_add_flash"] = f"Added {qty_txt}{what}{foil_txt}{loc_txt}{fetched_txt}."
        st.session_state["editor_coll_add_reset"] = True
        st.toast(st.session_state["editor_coll_add_flash"], icon="✅")
        st.rerun()

st.divider()
st.markdown("**Edit / remove collection lots**")
st.info(
    "Editing and removing existing lots now lives on the **Collection** page — turn on "
    "'✏️ Edit mode' there to change quantity, foil, location, date, price, or source "
    "directly on whatever the sidebar filters are showing."
)
