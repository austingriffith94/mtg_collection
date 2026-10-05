"""
MTG Collection Dashboard — main entry point.

Launch via run.py ("2) Launch dashboard"), or directly with:
    streamlit run dashboard.py

This file is the Streamlit "home" page — a landing page of image-backed
deck tiles (Prompt Pass 4). Clicking a tile navigates straight to that
deck's page on the Decks page (pages/2_Decks.py), which reads the
`deck_id` query param the tile's link carries to pre-select it.

The sidebar-nav explainer text sits directly above the deck tile grid
(Prompt Pass 9 / prompt2.txt) — it used to run at the very bottom of the
page, below the tiles.

The Collection, Decks, Land & Color Probability, Editor, and Commander
Game Tracking pages live under pages/ and appear automatically in the
sidebar nav (Streamlit's native multipage-app convention).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import streamlit as st

from dashboard_lib import db, loaders
from dashboard_lib import card_view as cv

cv.setup_page("MTG Collection Dashboard", "🃏")

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
- **🃏 Decks** — pick a deck (or click its tile below) to see its "Turn 0"
  summary (colors, bracket, tutors), win conditions/strengths/
  weaknesses, optimized mana, Game Changers, and Reserved List right
  underneath, then themes, mana curve, type breakdown, strategy tags, a
  Commander Spellbook Combos panel (real combo detection — what's fully
  assembled, and what's one card away), a Top 10 Most Expensive cards
  panel, and an optional custom cover image, before browsing its
  mainboard and/or shortlist the same way as the Collection page.
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
  mainboard and shortlist cards (including brand-new cards via a live
  Scryfall lookup when needed), collection lots (with a real date picker
  for Date Acquired), Game Changer categories with art and owned/in-deck
  counters, and a "Card Data" refresh that also prunes stale collection
  rows. No CSV editing required for any of it.
- **🏆 Commander Game Tracking** — log, edit, or delete games (tracked
  decks via dropdown, or a known/free-text opponent deck and player
  name with autofill from past entries), across a Log tab (rendered
  game history included) and a Stats tab (win rate by deck/by player,
  a multiplayer ELO rating, and a head-to-head win-rate matrix).
"""
)

st.divider()

st.subheader("Your decks")
st.caption("Click a deck to jump straight to its page.")

comparison = loaders.load_deck_comparison(conn)

# Workbench summary above the grid (Phase 5): pending work visible from the
# landing page rather than three clicks deep. Counts come from the same
# comparison rows as the tile strips, so the two can't disagree.
planned_total = int(comparison["planned"].sum())
decks_with_plans = int((comparison["planned"] > 0).sum())
unlocated = loaders.load_unlocated_lot_summary(conn)
workbench_bits = []
if planned_total:
    workbench_bits.append(f"**{planned_total}** planned change(s) across {decks_with_plans} deck(s)")
if summary["idea_rows"]:
    workbench_bits.append(f"**{summary['idea_rows']}** shortlist idea(s)")
if unlocated["lots"]:
    workbench_bits.append(f"**{unlocated['lots']}** lot(s) with no location")
if workbench_bits:
    st.markdown("🔧 " + " · ".join(workbench_bits) + " — open **Workbench** in the sidebar.")

decks_with_covers = loaders.load_decks_with_covers(conn)
cv.render_deck_landing_grid(decks_with_covers, status_df=comparison)
