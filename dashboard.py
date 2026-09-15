"""
MTG Collection Dashboard — main entry point.

Launch via run.py ("2) Launch dashboard"), or directly with:
    streamlit run dashboard.py

This file is the Streamlit "home" page. The Collection and Decks &
Maybeboard pages live under pages/ and appear automatically in the
sidebar nav (Streamlit's native multipage-app convention).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import streamlit as st

from dashboard_lib import db, loaders

st.set_page_config(page_title="MTG Collection Dashboard", page_icon="🃏", layout="wide")

st.title("🃏 MTG Collection Dashboard")

db.refresh_data_button()
db.require_db()

conn = db.get_connection()
summary = loaders.load_dashboard_summary(conn)

st.caption(f"Reading from `{os.path.basename(db.DB_PATH)}`.")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Decks", summary["total_decks"])
c2.metric("Unique printings tracked", summary["unique_printings"])
c3.metric("Collection lots", summary["collection_lots"])
c4.metric("Games logged", summary["games_logged"])

st.divider()

st.markdown(
    """
Use the sidebar to jump to:

- **📦 Collection** — browse/search your full physical collection, table or
  image-grid view (48 cards per page by default), with layered filters and
  breakouts (color, type, rarity, set, location, Showcase/Borderless, and
  more), plus a one-click link out to each card's Scryfall page.
- **🃏 Decks & Maybeboard** — pick a deck to see its "Turn 0" summary (colors,
  bracket, combos/tutors, win conditions/strengths/weaknesses, themes,
  mana curve, optimized mana, Game Changers, Top 10 Most Expensive / Top 10
  Saltiest cards, and an optional custom cover image), then browse its
  mainboard and/or maybeboard the same way as the Collection page.
- **🎲 Land & Color Probability** — for any deck: probability of drawing
  lands (or any specific card) in your opening 7, an estimate of hitting
  your land drop each of the first 5 turns, and the probability of having
  a mana source for each color in the commander's color identity at your
  opening hand and each of the first 5 turns.
- **✏️ Editor** — add, rename, and remove things directly: deck names and
  metadata (including a cover image and Win Conditions/Strengths/
  Weaknesses), whole new decks, themes (from a master dropdown you manage),
  card tags (bulk-edit any tag_type across a whole deck's mainboard in one
  table — create new tag types on the fly, e.g. Rule 0 categories),
  mainboard and maybeboard cards (including brand-new cards via a live
  Scryfall lookup when needed), collection lots (with a real date picker
  for Date Acquired), Game Changer categories with art and owned/in-deck
  counters, hand-maintained EDHREC salt scores, and a "Card Data" refresh
  that also prunes stale collection rows. No CSV editing required for any
  of it.
- **🏆 Commander Game Tracking** — log games (existing tracked decks via
  dropdown, or free-text for opponents' decks), see an actual rendered
  game history, and compare win rates by deck and by player.
"""
)

if summary["maybeboard_rows"]:
    st.caption(f"{summary['maybeboard_rows']} maybeboard rows across all decks.")
