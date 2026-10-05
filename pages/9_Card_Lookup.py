"""
Card Lookup page — one card, everything about it (Workbench rework,
Phase 3; see IMPLEMENTATION_PLAN.md).

No existing page answers "what's going on with THIS card" in one place.
The Collection page shows lots; the Decks page shows one deck's mainboard
and shortlist; the Workbench shows the cross-deck shortlist queue and
contention — none of them roll a single card up across every printing,
every lot, every deck, and its own change history. This page is that
rollup, built on queries.card_lookup(), which matches by oracle_id the
same way the plan allocator and contention table do, so two printings of
one card (or a card referenced by either face of an MDFC) answer as one.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from dashboard_lib import db, loaders, queries as q
from dashboard_lib import card_view as cv
from dashboard_lib import formatting as fmt

cv.setup_page("Card Lookup · MTG Dashboard", "🔍")

db.require_db()
conn = db.get_connection()

st.title("🔍 Card Lookup")
st.sidebar.header("Card Lookup")
db.refresh_data_button()

search = st.text_input(
    "Card name", key="card_lookup_search",
    placeholder="Start typing a card name…",
)

if not search.strip():
    st.caption("Search for any card already in your database to see its price, every lot "
               "you own, every deck it's mainboarded or shortlisted in, and its recent "
               "applied-change history — all rolled up across printings.")
    st.stop()

matches = q.search_card_names(conn, search.strip())
if not matches:
    st.warning(f"Nothing in your database matches '{search.strip()}'.")
    st.stop()

selected_name = matches[0] if len(matches) == 1 else st.selectbox(
    "Matching cards", matches, key="card_lookup_match_pick",
)

result = q.card_lookup(conn, selected_name)
if result is None:
    st.warning(f"'{selected_name}' isn't in your local database under any printing.")
    st.stop()

card = result["card"]
img_col, info_col = st.columns([1, 3])
with img_col:
    src = cv.card_image_src(card)
    if src:
        st.image(src, width="stretch")
    else:
        st.caption("No cached image.")

with info_col:
    url = fmt.scryfall_card_url(card.get("set_code"), card.get("collector_number"))
    st.markdown(f"### [{card['name']}]({url})")
    st.caption(f"{card.get('type_line') or ''}  ·  {(card.get('rarity') or '').title()}  ·  "
               f"{fmt.format_money(card.get('current_price_usd'))}")

    owned_line = f"Owned: **{result['owned_qty']}** copy(s)" if result["owned_qty"] else "**Not owned**"
    st.markdown(owned_line)
    if result["lots"]:
        for lot in result["lots"]:
            loc = lot["location"] or "— no location —"
            st.caption(f"　x{lot['quantity']}  {loc}")

    st.markdown("**Mainboard in**")
    if result["mainboard_in"]:
        for row in result["mainboard_in"]:
            st.caption(f"　{row['deck_name']} (x{row['quantity']})")
    else:
        st.caption("　Not in any mainboard.")

    st.markdown("**Shortlisted in**")
    if result["shortlisted_in"]:
        for row in result["shortlisted_in"]:
            st.caption(f"　{row['deck_name']} ({row['status']})")
    else:
        st.caption("　Not on any open shortlist.")

    if result["shortfall"] > 0:
        st.warning(
            f"⚠️ Demand {result['demand']} > owned {result['owned_qty']} — short "
            f"{result['shortfall']}. See the Workbench's Contention section."
        )

if result["history"]:
    st.divider()
    st.markdown("**History**")
    for h in result["history"]:
        change = f"+ {h['add_card']}" if h["add_card"] else ""
        change += f"   − {h['remove_card']}" if h["remove_card"] else ""
        when = (h["resolved_at"] or "").split(" ")[0]
        st.caption(f"{when}  {h['deck_name']}  {change}")
