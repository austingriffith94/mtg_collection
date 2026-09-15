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
- "Group / breakout by" lets you layer multiple groupings (e.g. Type, then
  Rarity within each type) — each top-level group is a collapsible
  section.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from dashboard_lib import db, loaders, formatting as fmt
from dashboard_lib import card_view as cv

st.set_page_config(page_title="Collection · MTG Dashboard", page_icon="📦", layout="wide")

db.require_db()
conn = db.get_connection()

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
