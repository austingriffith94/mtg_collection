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
  populated by the Editor's Card Data refresh / any new card fetch.
- Deck-status toggle (Prompt Pass 4): All / In a deck / Not in a deck /
  Not in a deck OR another location — see the comment above that control
  below for exactly what each option means.
- "Group / breakout by" lets you layer multiple groupings (e.g. Type, then
  Rarity within each type) — each top-level group is a collapsible
  section.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from dashboard_lib import db, loaders, formatting as fmt, moxfield_export
from dashboard_lib import card_view as cv

st.set_page_config(page_title="Collection · MTG Dashboard", page_icon="📦", layout="wide")

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
