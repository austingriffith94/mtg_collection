"""
Collection page — browse the full physical collection.

- Table view: sortable dataframe with a thumbnail column and a clickable
  "Scryfall" link column (st.column_config.LinkColumn).
- Image Grid view: paginated card art (local cache first, remote Scryfall
  URL fallback) where each card image itself links out to its Scryfall
  page.
- Sidebar filters (search, color identity, type, rarity, set, location,
  foil, basic land, game changer, Showcase, Borderless, price, mana
  value) all combine (AND across facets, "any of" within a facet).
  Showcase/Borderless are Scryfall frame-treatment flags (Phase 2),
  populated by the Card Database's Card Data refresh / any new card fetch.
- Deck-status toggle (Prompt Pass 4): All / In a deck / Not in a deck /
  Not in a deck OR another location — see the comment above that control
  below for exactly what each option means.
- Edit mode: swaps the browser for an editable table of exactly the rows
  the filters currently show (quantity, foil, location, date, price,
  source, delete), so a filtered set can be changed in place without
  hopping to another page. Lot-adding/importing stays on Collection Editor.
- "Group / breakout by" lets you layer multiple groupings (e.g. Type, then
  Rarity within each type) — each top-level group is a collapsible
  section.
"""
import sys
import os
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from dashboard_lib import db, loaders, writes, formatting as fmt, moxfield_export
from dashboard_lib import card_view as cv



def _parse_iso_date(value):
    """'YYYY-MM-DD' (or NaN/None/'') -> datetime.date, else None — for
    seeding the editor's date column from whatever's in the DB."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        return None


def render_edit_table(df):
    """Editable table over the already-filtered rows; saves only what changed."""
    if df.empty:
        st.info("No cards match the current filters.")
        return
    df = df.sort_values(by=["name", "set_code"], kind="stable")
    rows = [
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
        for _, r in df.iterrows()
    ]
    st.caption(
        f"{len(rows)} lot(s) shown — edit cells, then save. Changing a sidebar filter "
        "before saving discards unsaved edits."
    )
    # Options: catalog + deck names, plus any Location text these rows already
    # carry, so editing can never blank out a value the dropdown doesn't know.
    loc_options = sorted(
        set(loaders.load_location_options(conn)) | {r["Location"] for r in rows} | {""}
    )
    # Keyed by the visible lot ids so a different filter result gets a fresh
    # widget instead of replaying old edits against different rows by position.
    ids_hash = hash(tuple(r["collection_id"] for r in rows))
    edited = st.data_editor(
        pd.DataFrame(rows),
        column_config={
            "Card": st.column_config.TextColumn("Card", disabled=True),
            "Set": st.column_config.TextColumn("Set", disabled=True),
            "Qty": st.column_config.NumberColumn("Qty", min_value=0, step=1),
            "Foil": st.column_config.CheckboxColumn("Foil"),
            "Location": st.column_config.SelectboxColumn("Location", options=loc_options),
            "Date": st.column_config.DateColumn("Acquired", format="YYYY-MM-DD"),
            "Price Paid": st.column_config.NumberColumn("Price Paid", format="$%.2f"),
            "Source": st.column_config.TextColumn("Source"),
            "Delete": st.column_config.CheckboxColumn("Delete"),
        },
        column_order=["Card", "Set", "Qty", "Foil", "Location", "Date", "Price Paid", "Source", "Delete"],
        hide_index=True, use_container_width=True,
        key=f"collection_edit_table_{ids_hash}",
    )
    if st.button("💾 Save collection changes", key="collection_edit_save"):
        updates = {}
        for _, row in edited.iterrows():
            row_date = row["Date"]
            updates[row["collection_id"]] = {
                "_delete": bool(row["Delete"]),
                "quantity": int(row["Qty"]) if pd.notna(row["Qty"]) else None,
                "foil": bool(row["Foil"]),
                "location": row["Location"] or None,
                "date_acquired": row_date.isoformat() if pd.notna(row_date) and row_date else None,
                "price_paid": float(row["Price Paid"]) if pd.notna(row["Price Paid"]) else None,
                "source": row["Source"] or None,
            }
        changed = writes.bulk_update_collection(conn, updates)
        loaders.invalidate_collection_caches()
        st.success(f"Updated {changed} lot(s).")
        st.rerun()


cv.setup_page("Collection · MTG Dashboard", "📦")

db.require_db()
conn = db.get_connection()

MOXFIELD_EXPORT_DIR = os.path.join(db.BASE_DIR, "moxfield_exports")

st.title("📦 Collection")

st.sidebar.header("Collection filters")
db.refresh_data_button()

full_df = loaders.load_collection_df(conn)
coll_summary = loaders.load_collection_summary(conn)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Collection lots", coll_summary.get("lots") or 0)
m2.metric("Unique printings", coll_summary.get("unique_printings") or 0)
m3.metric("Tracked quantity", coll_summary.get("tracked_quantity") or 0)
m4.metric("Tracked value", fmt.format_money(coll_summary.get("tracked_value")))
st.caption(
    "Tracked figures exclude untracked bulk lots (e.g. basics with no counted "
    "quantity) — those are in the collection but not summed above."
)

with st.expander("📋 Export to Moxfield (Collection CSV)"):
    st.caption(
        "Moxfield-compatible collection CSV — one row per printing + foil combination "
        "(quantities summed across any collection lots sharing that exact printing). "
        "'Owned NOT in a deck' only includes printings with zero copies used in any "
        "deck's mainboard; Tradelist Count is 0 for anything run in a deck either way."
    )
    export_scope = st.radio(
        "Which cards?", ["Full Collection", "Owned NOT in a deck"],
        key="collection_moxfield_scope", horizontal=True,
    )
    export_not_in_deck = export_scope == "Owned NOT in a deck"
    export_rows = moxfield_export.collection_export_rows(conn, only_not_in_deck=export_not_in_deck)
    if not export_rows:
        st.caption("Nothing to export for this scope yet.")
    else:
        st.caption(f"{len(export_rows)} printing/foil row(s) ready to export.")
        export_csv_text = moxfield_export.export_collection_csv_text(conn, only_not_in_deck=export_not_in_deck)
        scope_suffix = "not_in_deck" if export_not_in_deck else "full"
        dl_col, save_col = st.columns(2)
        dl_col.download_button(
            "⬇️ Download CSV", data=export_csv_text,
            file_name=f"moxfield_collection_{scope_suffix}.csv", mime="text/csv",
            key=f"collection_moxfield_dl_{scope_suffix}",
        )
        if save_col.button("💾 Save to file", key=f"collection_moxfield_save_{scope_suffix}"):
            os.makedirs(MOXFIELD_EXPORT_DIR, exist_ok=True)
            out_path = os.path.join(MOXFIELD_EXPORT_DIR, f"moxfield_collection_{scope_suffix}.csv")
            with open(out_path, "w", encoding="utf-8", newline="") as f:
                f.write(export_csv_text)
            st.success(f"Saved to `{os.path.relpath(out_path, db.BASE_DIR)}`")

filtered = cv.render_filter_panel(
    full_df,
    key_prefix="collection",
    container=st.sidebar,
    multi_facets=[("color_identity", "Color Identity", "Colorless")],
    single_facets=[
        ("card_type", "Card Type"),
        ("rarity", "Rarity"),
        ("set_code", "Set"),
        ("location", "Location"),
    ],
    bool_toggles=[
        ("is_game_changer", "Game Changer"),
        ("is_reserved", "Reserved List"),
        ("is_basic_land", "Basic Land"),
        ("foil", "Foil"),
        ("is_showcase", "Showcase"),
        ("is_borderless", "Borderless"),
    ],
)

# Deck-status toggle (Prompt Pass 4). "in_deck" comes straight from
# queries.collection_dataframe() — 1 if this printing's scryfall_id is
# used in ANY deck's mainboard (deck_cards), the same "spoken for by a
# deck" test writes.prune_collection() uses. "Not in a deck OR another
# location" narrows further to cards that are unaccounted for on BOTH
# fronts at once — not run in any deck AND no location recorded either
# (blank/NULL) — rather than the broader "not in a deck, or has some
# other location" reading, since that's the bucket most useful for
# spotting cards that genuinely have no home yet.
deck_status_key = "collection_deck_status"
cv.register_filter_key(deck_status_key)
deck_status = st.sidebar.selectbox(
    "Deck status",
    ["All", "In a deck", "Not in a deck", "Not in a deck OR another location"],
    key=deck_status_key,
    help=(
        "In a deck: this printing is used in at least one deck's mainboard. "
        "Not in a deck OR another location: not run in any deck AND has no "
        "Location recorded either — i.e. truly unassigned."
    ),
)
if deck_status == "In a deck":
    filtered = filtered[filtered["in_deck"] == 1]
elif deck_status == "Not in a deck":
    filtered = filtered[filtered["in_deck"] == 0]
elif deck_status == "Not in a deck OR another location":
    filtered = filtered[
        (filtered["in_deck"] == 0)
        & (filtered["location"].isna() | (filtered["location"].astype(str).str.strip() == ""))
    ]

column_config = cv.base_column_config()
column_config.update(
    {
        "quantity": st.column_config.NumberColumn("Qty", width="small"),
        "foil": st.column_config.CheckboxColumn("Foil", width="small"),
        "is_reserved": st.column_config.CheckboxColumn("Reserved", width="small"),
        "is_showcase": st.column_config.CheckboxColumn("Showcase", width="small"),
        "is_borderless": st.column_config.CheckboxColumn("Borderless", width="small"),
        "location": st.column_config.TextColumn("Location", width="small"),
        "date_acquired": st.column_config.TextColumn("Acquired", width="small"),
        "price_paid": st.column_config.NumberColumn("Paid", format="$%.2f", width="small"),
    }
)

if st.toggle(
    "✏️ Edit mode", key="collection_edit_mode",
    help="Edit quantity / foil / location / date / price / source (or delete) for "
         "exactly the lots the filters above currently show.",
):
    render_edit_table(filtered)
    st.stop()

cv.render_browser(
    filtered,
    key_prefix="collection",
    group_options=[
        ("card_type", "Type"),
        ("color_display", "Color Identity"),
        ("rarity", "Rarity"),
        ("set_code", "Set"),
        ("location", "Location"),
    ],
    column_config=column_config,
    extra_table_cols=[
        "quantity", "foil", "is_reserved", "is_showcase", "is_borderless",
        "location", "date_acquired", "price_paid",
    ],
)
